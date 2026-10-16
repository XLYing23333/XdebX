#!/usr/bin/env python3
"""Bounded, memory-only URL checks; never installs or writes on the SSH host."""
import argparse
import datetime
import json
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAP = 64 * 1024
BUDGET = 8 * 1024 * 1024


def inventory():
    items = []
    def add(label, url, mode, kind='binary', effective=True):
        if url and url.startswith('https://'):
            items.append(dict(label=label, url=url, mode=mode, kind=kind, effective=effective))
    for distro, suites in [('ubuntu', ['focal', 'focal-updates', 'focal-security', 'jammy', 'jammy-updates', 'jammy-security', 'noble', 'noble-updates', 'noble-security']),
                           ('debian', ['bookworm', 'bookworm-updates', 'trixie', 'trixie-updates']),
                           ('debian-security', ['bookworm-security', 'trixie-security'])]:
        for suite in suites:
            host = ('security.ubuntu.com' if suite.endswith('security') else 'archive.ubuntu.com') if distro == 'ubuntu' else 'deb.debian.org'
            add('APT ' + suite, 'https://' + host + '/' + distro + '/dists/' + suite + '/InRelease', 'Raw', 'metadata')
            add('APT ' + suite, 'https://mirrors.tuna.tsinghua.edu.cn/' + distro + '/dists/' + suite + '/InRelease', 'CN', 'metadata')
    for distro, suites in [('ubuntu', ['jammy', 'noble']), ('debian', ['bookworm', 'trixie'])]:
        for suite in suites:
            for mode, base in [('Raw', 'https://download.docker.com/linux/'), ('CN', 'https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/')]:
                add('Docker ' + suite, base + distro + '/dists/' + suite + '/InRelease', mode, 'metadata')
    for distro in ['ubuntu', 'debian']:
        add('Docker key ' + distro, 'https://download.docker.com/linux/' + distro + '/gpg', 'Raw', 'metadata')
        add('Docker key ' + distro, 'https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/' + distro + '/gpg', 'CN', 'metadata', False)
    for mode, host in [('Raw', 'registry.npmjs.org'), ('CN', 'registry.npmmirror.com')]:
        for package in ['@anthropic-ai/claude-code', 'opencode-ai', 'openclaw']:
            add('npm ' + package, 'https://' + host + '/' + package, mode, 'json')
    for mode, host in [('Raw', 'pypi.org'), ('CN', 'pypi.tuna.tsinghua.edu.cn')]:
        add('PyPI uv', 'https://' + host + '/simple/uv/', mode, 'index')
    for mode, base in [('Raw', 'https://nodejs.org/dist'), ('CN', 'https://npmmirror.com/mirrors/node')]:
        add('Node index', base + '/index.json', mode, 'json', mode == 'Raw')
    manifest = json.loads((ROOT / 'data/pkgs/manifest.json').read_text())
    online = json.loads((ROOT / 'data/pkgs/online-sources.json').read_text())['sources']
    for entry in manifest['packages'] + online:
        if entry.get('download_enabled') is False:
            continue
        url = entry.get('url')
        if url and url.endswith('.git'):
            add(entry['id'] + ' git refs', url + '/info/refs?service=git-upload-pack', 'Raw', 'git')
        else:
            add(entry['id'] + ' ' + entry['version'], url, 'Raw')
            if url:
                sys.path.insert(0, str(ROOT / 'src'))
                from xdebx.mirrors import cn_url
                mirrored = cn_url(url)
                if mirrored != url:
                    add(entry['id'] + ' ' + entry['version'], mirrored, 'CN')
        add(entry['id'] + ' git snapshot', entry.get('git_archive_url'), 'Raw')
    seen = set()
    return [item for item in items if not (item['url'] in seen or seen.add(item['url']))]


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith('https://'):
            raise ValueError('Non-HTTPS redirect refused')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def inspect_body(body, kind, content_type):
    head = body[:1024].lstrip().lower()
    html = 'text/html' in content_type or head.startswith((b'<!doctype html', b'<html'))
    if kind in ('binary', 'metadata', 'json', 'git') and html:
        raise ValueError('HTML error/login page instead of expected resource')
    if kind == 'git' and 'application/x-git-upload-pack-advertisement' not in content_type:
        raise ValueError('Expected Git smart-HTTP advertisement')
    if kind == 'binary' and 'application/json' in content_type:
        raise ValueError('JSON error/metadata response instead of package')
    if kind == 'json' and not head.startswith((b'{', b'[')):
        raise ValueError('Expected JSON metadata')
    if kind == 'metadata' and not head.startswith((b'-----begin pgp', b'origin:', b'suite:', b'label:')):
        raise ValueError('Expected signed APT metadata or key')
    if not body:
        raise ValueError('Empty response')


def probe(item, opener, remaining):
    record = dict(item, attempts=[], bytes_read=0, status='failed')
    for attempt in range(2):
        start = time.monotonic()
        detail = {'attempt': attempt + 1}
        def deadline(*_):
            raise TimeoutError('15-second request deadline')
        previous = signal.signal(signal.SIGALRM, deadline)
        signal.setitimer(signal.ITIMER_REAL, 15)
        try:
            headers = {'User-Agent': 'XdebX-URL-Probe/1.0', 'Accept-Encoding': 'identity'}
            try:
                with opener.open(urllib.request.Request(item['url'], headers=headers, method='HEAD'), timeout=5) as response:
                    detail.update(head_status=response.status, final_url=response.url)
            except urllib.error.HTTPError as exc:
                detail['head_status'] = exc.code
                if exc.fp is not None:
                    exc.close()
            # HEAD is insufficient to identify a 200 HTML error page.
            limit = min(CAP, remaining - record['bytes_read'])
            if limit <= 0:
                record['status'] = 'budget-exhausted'
                break
            headers['Range'] = 'bytes=0-' + str(limit - 1)
            with opener.open(urllib.request.Request(item['url'], headers=headers), timeout=5) as response:
                detail.update(http_status=response.status, final_url=response.url,
                              content_type=response.headers.get('Content-Type', ''),
                              declared_size=response.headers.get('Content-Length'),
                              range_honored=response.status == 206)
                # read1 avoids waiting to fill 64KiB on a stalled peer; alarm bounds the loop.
                parts = []
                while detail.get('bytes_read', 0) < limit:
                    block = response.read1(min(8192, limit - detail.get('bytes_read', 0)))
                    if not block:
                        break
                    parts.append(block)
                    detail['bytes_read'] = detail.get('bytes_read', 0) + len(block)
                    record['bytes_read'] += len(block)
                inspect_body(b''.join(parts), item['kind'], detail['content_type'])
            record['status'] = 'reachable-sample-only'
        except Exception as exc:
            detail['error'] = str(exc)
            if isinstance(exc, urllib.error.HTTPError):
                detail['http_status'] = exc.code
                if exc.fp is not None:
                    exc.close()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            detail['elapsed_seconds'] = round(time.monotonic() - start, 3)
            record['attempts'].append(detail)
        if record['status'] == 'reachable-sample-only':
            break
    return record


def worker(items):
    opener = urllib.request.build_opener(HTTPSOnly())
    spent = 0
    for item in items:
        if spent >= BUDGET:
            result = dict(item, status='not-tested-budget', bytes_read=0, attempts=[])
        else:
            result = probe(item, opener, BUDGET - spent)
        spent += result['bytes_read']
        print(json.dumps(result, ensure_ascii=False), flush=True)
    print(json.dumps({'summary': True, 'bytes_read': spent, 'budget': BUDGET,
                      'remote_files_created': 0, 'remote_cache_created': False,
                      'cleanup': 'No files or package caches created; responses held only in memory'}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='SSH host or alias authorized for this network probe')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/beijing-url-probe.json')
    parser.add_argument('--inventory', action='store_true')
    parser.add_argument('--cn-artifacts', action='store_true', help='Probe mirrored artifacts and corresponding originals only')
    args = parser.parse_args()
    items = inventory()
    if args.cn_artifacts:
        labels = {i['label'] for i in items if i['mode'] == 'CN' and i['kind'] == 'binary'}
        items = [i for i in items if i['label'] in labels or i['kind'] == 'git']
    if args.inventory:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    # Send code and public URLs through stdin; no upload, remote script file or cache.
    source = "__file__ = '/tmp/xdebx-url-probe-memory.py'\n" + Path(__file__).read_text().rsplit("\nif __name__ == '__main__':", 1)[0]
    source += '\nworker(' + repr(items) + ')\n'
    report = {'host': args.host, 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'limits': {'response_bytes': CAP, 'total_bytes': BUDGET, 'connection_seconds': 5, 'request_seconds': 15, 'retries': 1},
              'results': [], 'cleanup_verified': False}
    proc = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', args.host,
                             'python3 -B -'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc.stdin.write(source)
    proc.stdin.close()
    for line in proc.stdout:
        result = json.loads(line)
        if result.get('summary'):
            report['summary'] = result
            report['cleanup_verified'] = result['remote_files_created'] == 0
        else:
            report['results'].append(result)
            print(result['status'] + ' ' + result['label'] + ' [' + result['mode'] + ']', flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    report['ssh_exit_code'] = proc.wait()
    report['ssh_stderr'] = proc.stderr.read()
    report['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    if proc.returncode:
        raise SystemExit(report['ssh_stderr'])


if __name__ == '__main__':
    main()
