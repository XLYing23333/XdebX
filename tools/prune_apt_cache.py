#!/usr/bin/env python3
"""Retire native distro DEB backups only after that distro's real APT smoke passes."""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from xdebx.runtime import atomic_json, sha256

HOSTS = {'git-man': 'git', 'zsh-common': 'zsh-omz', 'zsh': 'zsh-omz',
         'vim-runtime': 'vim9', 'vim': 'vim9', 'tmux': 'tmux-tmuxp', 'tmuxp': 'tmux-tmuxp'}


def candidates(manifest, directory, target):
    proofs = {}
    for folder in directory.glob(target + '-*'):
        case_file, result_file = folder / 'case.json', folder / 'result.json'
        if not case_file.exists() or not result_file.exists():
            continue
        case, result = json.loads(case_file.read_text()), json.loads(result_file.read_text())
        if case.get('target') != target or case.get('kind') != 'strategy' or case.get('method') != 'official' or result.get('status') != 'passed':
            continue
        proofs[case['software']] = (folder, result)
    selected = []
    for entry in manifest['packages']:
        # An upstream portable DEB is not proof of a working native APT package.
        if entry.get('download_enabled') is False or entry.get('kind') != 'deb' or not entry.get('source_package'):
            continue
        if entry.get('targets') != [target] or not entry['filename'].startswith(target + '/'):
            continue
        software = HOSTS.get(entry['id'], entry['id'])
        if software not in proofs:
            continue
        folder, result = proofs[software]
        native = entry.get('deb_control', {}).get('Package', entry['id'])
        version = entry['version']
        recorded = result.get('package_versions', {}).get(native)
        log = folder / 'container.log'
        installed = log.exists() and re.search(r'Setting up ' + re.escape(native) + r'(?::amd64)? \(' + re.escape(version) + r'\)', log.read_text(errors='replace'))
        if recorded != version and not installed:
            continue
        asset = ROOT / 'data/pkgs' / entry['filename']
        if '..' in Path(entry['filename']).parts:
            raise ValueError('Unsafe backup path')
        try:
            asset.resolve().relative_to((ROOT / 'data/pkgs').resolve())
        except ValueError:
            raise ValueError('Backup escapes package directory')
        if not asset.is_file() or sha256(asset) != entry['sha256']:
            raise ValueError('Missing or changed backup; no deletion: ' + str(asset))
        selected.append((entry, result, recorded == version))
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, choices=['ubuntu20', 'ubuntu22', 'ubuntu24', 'debian12', 'debian13'])
    parser.add_argument('--results', type=Path, default=ROOT / 'test-results/docker')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    manifest_file = ROOT / 'data/pkgs/manifest.json'
    manifest = json.loads(manifest_file.read_text())
    selected = candidates(manifest, args.results, args.target)
    print(json.dumps({'target': args.target, 'delete': [entry['filename'] for entry, _, _ in selected], 'apply': args.apply}, indent=2))
    if not args.apply or not selected:
        return
    audit_file = ROOT / 'data/pkgs/provenance/retired-cache.json'
    audit = json.loads(audit_file.read_text()) if audit_file.exists() else {'schema_version': 1, 'assets': {}}
    matrix = json.loads((args.results / 'matrix.json').read_text())
    retired = set()
    for entry, proof, structured in selected:
        name = entry['filename']
        audit['assets'][name] = {'target': args.target, 'version': entry['version'], 'sha256': entry['sha256'],
                                'retired_at': time.time(), 'reason': 'User requested removal of redundant native APT backups after functional acceptance',
                                'apt_case_id': proof['case_id'], 'image_digest': proof.get('image_digest'),
                                'evidence': 'package_versions' if structured else 'exact package/version in successful APT installation log'}
        entry['download_enabled'] = False
        entry['retired_reason'] = 'Native APT installation and functional smoke passed on ' + args.target + '; redundant backup removed at user request'
        retired.add(name)
    # Persist audit and retire candidates before deleting their bytes.
    atomic_json(audit_file, audit, 0o644)
    atomic_json(manifest_file, manifest, 0o644)
    for case in matrix:
        if case.get('entry', {}).get('filename') in retired:
            folder = args.results / case['case_id']
            result = folder / 'result.json'
            if not result.exists():
                # A currently running --resume driver reads this marker before
                # starting the next case. It is explicitly not a test result.
                atomic_json(result, {'case_id': case['case_id'], 'target': case['target'], 'software': case['software'],
                                     'status': 'not-run', 'scope_excluded': True, 'tested': False,
                                     'reason': 'Retired APT backup; removed from the next generated matrix'}, 0o644)
    for name in retired:
        (ROOT / 'data/pkgs' / name).unlink()
    sums = ROOT / 'data/pkgs/SHA256SUMS'
    if sums.exists():
        sums.write_text('\n'.join(line for line in sums.read_text().splitlines() if line.split()[-1].lstrip('*') not in retired) + '\n')


if __name__ == '__main__':
    main()
