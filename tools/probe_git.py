#!/usr/bin/env python3
"""Disposable, bounded Git clone/pinned-fetch checks. Never installs or executes repo files."""
import argparse
import datetime
import hashlib
import json
import os
import resource
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_LIMIT = 16 * 1024 * 1024
TOTAL_LIMIT = 64 * 1024 * 1024
FILE_LIMIT = 8 * 1024 * 1024


def inventory():
    sys.path.insert(0, str(ROOT / 'src'))
    from xdebx.configuration import SNAPSHOT_FILES
    from xdebx.resources import safe_extract
    items = []
    for entry in json.loads((ROOT/'data/pkgs/manifest.json').read_text())['packages']:
        if not entry.get('git_url') or entry.get('download_enabled') is False:
            continue
        item = {'id':entry['id'], 'url':entry['git_url'], 'version':entry['version'], 'provider':'GitHub'}
        member = SNAPSHOT_FILES.get(entry['id'])
        archive = ROOT/'data/pkgs'/entry['filename']
        if member and archive.is_file():
            if hashlib.sha256(archive.read_bytes()).hexdigest() != entry['sha256']:
                raise ValueError('Local snapshot changed')
            with tempfile.TemporaryDirectory() as tmp:
                safe_extract(archive,tmp)
                roots=list(Path(tmp).iterdir())
                file=roots[0]/member
                item.update(member=member, sha256=hashlib.sha256(file.read_bytes()).hexdigest())
        items.append(item)
    return items


def size(path):
    total=0
    for file in Path(path).rglob('*'):
        try:
            if file.is_file() and not file.is_symlink():total+=file.stat().st_size
        except FileNotFoundError:
            pass  # Git atomically renames pack/index files while being monitored.
    return total


def terminate(proc):
    if proc is None:
        return
    try:os.killpg(proc.pid,signal.SIGTERM)
    except ProcessLookupError:pass
    try:proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        proc.wait(timeout=2)


def run(args, home, timeout=45, remaining=TOTAL_LIMIT):
    env=dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home/'config'), XDG_CACHE_HOME=str(home/'cache'),
             GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/bin/false', GCM_INTERACTIVE='never')
    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE,(FILE_LIMIT,FILE_LIMIT))
    start=time.monotonic();proc=None;sel=selectors.DefaultSelector();buffers={'out':bytearray(),'err':bytearray()};peak=0;error=None
    try:
        proc=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,start_new_session=True,preexec_fn=limits)
        for pipe,label in ((proc.stdout,'out'),(proc.stderr,'err')):sel.register(pipe,selectors.EVENT_READ,label)
        while sel.get_map() or proc.poll() is None:
            peak=max(peak,size(home))
            if peak>min(REPO_LIMIT,remaining):error='disk-budget';break
            if time.monotonic()-start>timeout:error='timeout';break
            for key,_ in sel.select(.1):
                data=os.read(key.fileobj.fileno(),65536)
                if not data:sel.unregister(key.fileobj);continue
                buffer=buffers[key.data];buffer.extend(data)
                if len(buffer)>1024*1024:error='output-budget';break
            if error:break
        if error:terminate(proc)
        else:proc.wait(timeout=2)
        return {'exit_code':proc.returncode,'error':error,'elapsed_seconds':round(time.monotonic()-start,3),
                'peak_bytes':peak,'stderr':buffers['err'][-8192:].decode('utf-8','replace')},bytes(buffers['out'])
    finally:
        if proc is not None:
            if proc.poll() is None:terminate(proc)
            proc.stdout.close();proc.stderr.close()
        sel.close()


def probe(item, home, remaining):
    git=['git','-c','core.hooksPath=/dev/null','-c','credential.helper=',
         '-c','protocol.file.allow=never','-c','protocol.ext.allow=never',
         '-c','http.lowSpeedLimit=1024','-c','http.lowSpeedTime=10']
    if item.get('http_version'):
        git += ['-c', 'http.version='+item['http_version']]
    repo=home/'repo';record=dict(item,status='failed',steps=[],peak_bytes=0)
    commands=[('clone',git+['clone','--depth','1','--no-checkout','--filter=blob:none',item['url'],str(repo)]),
              ('pinned-fetch',git+['-C',str(repo),'fetch','--depth','1','--filter=blob:none','origin',item['version']]),
              ('verify-commit',git+['-C',str(repo),'rev-parse',item['version']+'^{commit}'])]
    if item.get('member'):commands.append(('verify-file',git+['-C',str(repo),'show',item['version']+':'+item['member']]))
    if item.get('checkout'):
        commands.append(('checkout',git+['-C',str(repo),'checkout','--detach',item['version']]))
    for name,args in commands:
        if name=='pinned-fetch':
            check,head=run(git+['-C',str(repo),'rev-parse','HEAD'],home,timeout=10,remaining=remaining)
            if check['exit_code']==0 and not check['error'] and head.decode().strip()==item['version']:
                record['commit_already_present']=True
                continue
        result,output=run(args,home,remaining=remaining)
        result['step']=name;record['steps'].append(result);record['peak_bytes']=max(record['peak_bytes'],result['peak_bytes'])
        if result['exit_code']!=0 or result['error']:break
        if name=='clone':record['clone_reachable']=True
        if name=='verify-commit':
            record['actual_commit']=output.decode().strip()
            if record['actual_commit']!=item['version']:record['reason']='Pinned commit differs';break
            record['pinned_commit_verified']=True
        if name=='verify-file':
            record['actual_sha256']=hashlib.sha256(output).hexdigest()
            if record['actual_sha256']!=item['sha256']:record['reason']='Snapshot file differs';break
            record['file_verified']=True
        if name=='checkout':
            record['checkout_verified']=True
            if item.get('member') and hashlib.sha256((repo/item['member']).read_bytes()).hexdigest()!=item['sha256']:
                record['reason']='Checked out file differs';record['checkout_verified']=False;break
    else:record['status']='passed'
    return record


def worker(items):
    if not shutil.which('git'):
        print(json.dumps({'summary':True,'error':'Git unavailable; no installation attempted','cleanup_verified':True}),flush=True)
        return
    directory=Path(tempfile.mkdtemp(prefix='xdebx-git-probe-',dir='/tmp'));spent=0;results=[]
    def stop(*_):raise InterruptedError('Probe interrupted')
    for sig in (signal.SIGTERM,signal.SIGHUP,signal.SIGINT):signal.signal(sig,stop)
    try:
        for index,item in enumerate(items):
            if spent>=TOTAL_LIMIT:
                result=dict(item,status='not-tested-budget',steps=[],peak_bytes=0)
            else:
                home=directory/str(index);home.mkdir()
                try:result=probe(item,home,TOTAL_LIMIT-spent)
                finally:shutil.rmtree(str(home))
                spent+=result['peak_bytes']
            results.append(result);print(json.dumps(result,ensure_ascii=False),flush=True)
    finally:
        shutil.rmtree(str(directory))
        print(json.dumps({'summary':True,'directory':str(directory),'cleanup_verified':not directory.exists(),
                          'aggregate_peak_bytes':spent,'disk_budget':TOTAL_LIMIT,'installed':False}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='SSH host or alias authorized for this network probe')
    parser.add_argument('--output',type=Path,default=ROOT/'docs/beijing-git-probe.json')
    parser.add_argument('--after',type=Path,help='Wait for a preceding probe cleanup report to keep SSH tests serial')
    parser.add_argument('--candidates',type=Path,help='JSON array of explicit mirror URL/pinned version/member/hash records')
    args=parser.parse_args()
    if args.after:
        deadline=time.monotonic()+600
        while True:
            try:previous=json.loads(args.after.read_text())
            except (OSError,ValueError):previous={}
            if previous.get('summary',{}).get('cleanup_verified'):break
            if time.monotonic()>deadline:raise SystemExit('Prior probe did not confirm cleanup; next probe not started')
            time.sleep(1)
    items=json.loads(args.candidates.read_text()) if args.candidates else inventory()
    source="__file__ = '/tmp/xdebx-git-probe-memory.py'\n"+Path(__file__).read_text().rsplit("\nif __name__ == '__main__':",1)[0]
    source+='\nworker('+repr(items)+')\n'
    report={'host':args.host,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'results':[],
            'limits':{'per_repository_bytes':REPO_LIMIT,'aggregate_peak_bytes':TOTAL_LIMIT,'per_file_bytes':FILE_LIMIT,'command_seconds':45}}
    proc=subprocess.Popen(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',args.host,'python3 -B -'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    proc.stdin.write(source);proc.stdin.close()
    for line in proc.stdout:
        result=json.loads(line)
        if result.get('summary'):report['summary']=result
        else:
            report['results'].append(result)
            print(result['id']+' ['+result['provider']+'] '+result['status'],flush=True)
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    report['ssh_exit_code']=proc.wait();report['ssh_stderr']=proc.stderr.read()
    report['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    if proc.returncode:raise SystemExit(report['ssh_stderr'])


if __name__ == '__main__':
    main()
