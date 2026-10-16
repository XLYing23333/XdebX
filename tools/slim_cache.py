#!/usr/bin/env python3
"""Keep small, redistributable cache assets and matching source; retain download metadata."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from xdebx.runtime import atomic_json, sha256

LIMIT = 5 * 1024 * 1024


def slim(root=ROOT, apply=False):
    folder = root / 'data/pkgs'
    manifest = json.loads((folder / 'manifest.json').read_text())
    sources = json.loads((folder / 'provenance/corresponding-source.json').read_text())
    records = sources.get('packages', {})
    keep = set()
    changes = []
    for entry in manifest['packages']:
        local = folder / entry['filename']
        reason = 'Small active asset'
        retain = entry.get('download_enabled') is not False and entry.get('redistribution_allowed') is not False
        retain = retain and entry['id'] != 'CTypora' and (entry.get('size_bytes') or (local.stat().st_size if local.exists() else LIMIT + 1)) <= LIMIT
        if retain and entry['filename'] in records:
            members = records[entry['filename']].get('files', [])
            if any((folder / asset['filename']).exists() and (folder / asset['filename']).stat().st_size > LIMIT for asset in members):
                retain = False
                reason = 'Matching redistribution source exceeds small-cache limit'
            else:
                keep.update(asset['filename'] for asset in members)
        if retain:
            keep.add(entry['filename'])
        else:
            if reason == 'Small active asset':
                reason = 'Large, retired, unused or unverified asset'
        entry['bundle'] = 'local' if retain else 'on-demand'
        changes.append({'filename': entry['filename'], 'bundle': entry['bundle'], 'reason': reason,
                        'size_bytes': local.stat().st_size if local.is_file() else 0,
                        'downloadable': bool(entry.get('url'))})
    # Preserve all catalogues, provenance evidence and notices, even for remote assets.
    metadata = {str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file() and
                (p.parent == folder and p.suffix in ('.json', '.md') or 'provenance' in p.parts and 'corresponding-source' not in p.parts)}
    keep.update(metadata)
    keep.add('SHA256SUMS')
    removed = []
    before = sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
    for path in sorted(folder.rglob('*')):
        if not path.is_file() or str(path.relative_to(folder)) in keep:
            continue
        relative = str(path.relative_to(folder))
        if path.is_symlink() or os.path.commonpath([str(path.resolve()), str(folder.resolve())]) != str(folder.resolve()):
            raise ValueError('Unsafe cache path: ' + relative)
        entry = next((e for e in manifest['packages'] if e['filename'] == relative), None)
        if entry and entry.get('sha256') and sha256(path) != entry['sha256']:
            raise ValueError('Changed asset preserved: ' + relative)
        removed.append({'filename': relative, 'size_bytes': path.stat().st_size})
    report = {'schema_version': 1, 'small_asset_limit': LIMIT, 'before_bytes': before,
              'after_bytes': before - sum(item['size_bytes'] for item in removed),
              'assets': changes, 'removed': removed, 'applied': apply}
    if apply:
        atomic_json(folder / 'manifest.json', manifest, 0o644)
        atomic_json(folder / 'provenance/cache-size.json', report, 0o644)
        for item in removed:
            (folder / item['filename']).unlink()
        for path in sorted(folder.rglob('*'), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        sums = []
        for entry in manifest['packages']:
            path = folder / entry['filename']
            if path.is_file():
                sums.append(sha256(path) + '  ' + entry['filename'])
        (folder / 'SHA256SUMS').write_text('\n'.join(sums) + '\n')
        report['after_bytes'] = sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
        atomic_json(root / 'docs/cache-size.json', report, 0o644)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    report = slim(apply=args.apply)
    print(json.dumps({key: report[key] for key in ('before_bytes', 'after_bytes', 'applied')}, indent=2))
