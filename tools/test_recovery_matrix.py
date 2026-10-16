#!/usr/bin/env python3
"""Five-system tests in named, disposable local containers; never touches SSH hosts."""
import argparse
import json
import subprocess
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
IMAGES = {'ubuntu20': 'ubuntu:20.04', 'ubuntu22': 'ubuntu:22.04', 'ubuntu24': 'ubuntu:24.04',
          'debian12': 'debian:12-slim', 'debian13': 'debian:13-slim'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'test-results/recovery')
    parser.add_argument('--targets', nargs='+', choices=list(IMAGES), default=list(IMAGES))
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    summary=[];run_id=str(time.time_ns())
    for target in args.targets:
        image=IMAGES[target]
        folder=args.output/target;folder.mkdir(exist_ok=True)
        if (folder/'outcome.json').exists():
            (folder/'outcome.json').unlink()
        name='xdebx-recovery-'+run_id+'-'+target
        cmd=['sudo','-n','docker','run','--rm','--name',name,'--label','xdebx.recovery-test='+run_id,
             '--memory','512m','--pids-limit','256','--ulimit','core=0:0',
             '--mount','type=bind,src='+str(ROOT)+',dst=/project,readonly',
             '--mount','type=bind,src='+str(folder.resolve())+',dst=/results',
             '--workdir','/project',image,'/bin/sh','-c',
             'set -e\nexport DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1\n'
             'apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=15 update\n'
             'apt-get -y --no-install-recommends -o Acquire::Retries=1 -o Acquire::http::Timeout=15 install python3 ca-certificates\n'
             'useradd -m -s /bin/bash xdebx\n'
             'python3 -B -m unittest discover -s tests -q\n'
             'python3 -B tools/recovery_case.py /results/outcome.json\n']
        print('Starting '+target,flush=True);start=time.monotonic()
        try:
            with (folder/'container.log').open('w') as log:
                process=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,timeout=600)
            result=json.loads((folder/'outcome.json').read_text()) if (folder/'outcome.json').exists() else {'target':target,'status':'failed','reason':'See container.log'}
            if process.returncode:result.update(status='failed',exit_code=process.returncode)
        except subprocess.TimeoutExpired:
            subprocess.run(['sudo','-n','docker','rm','-f',name],capture_output=True)
            result={'target':target,'status':'timeout'}
        result['elapsed_seconds']=round(time.monotonic()-start,2)
        summary.append(result)
        (args.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
        print(target+' '+result['status'],flush=True)
    raise SystemExit(0 if all(r['status']=='passed' for r in summary) else 1)


if __name__ == '__main__':
    main()
