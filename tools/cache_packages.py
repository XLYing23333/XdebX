#!/usr/bin/env python3
"""Fetch cache assets with pinned hashes; never install. Default honors the slim bundle."""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from xdebx.runtime import Runner, sha256
from xdebx.resources import Resources


def collect(full=False):
    ctx = SimpleNamespace(home=ROOT, settings={'mirror': 'Raw', 'language': 'EN'}, target=None,
                          cache_dir=ROOT / 'data/pkgs', fix_owner=lambda _: None,
                          uid=__import__('os').getuid(), gid=__import__('os').getgid())
    resources = Resources(ctx, Runner(ctx, print))
    selected = [e for e in resources.cached if e.get('download_enabled') is not False and
                e.get('redistribution_allowed') is not False and e.get('url') and
                (full or e.get('bundle') != 'on-demand')]
    source_report = json.loads((ROOT / 'data/pkgs/provenance/corresponding-source.json').read_text())
    for entry in selected:
        destination = ROOT / 'data/pkgs' / entry['filename']
        if not destination.is_file() or sha256(destination) != entry['sha256']:
            resources.fetch(entry['url'], destination, entry)
        for source in source_report.get('packages', {}).get(entry['filename'], {}).get('files', []):
            destination = ROOT / 'data/pkgs' / source['filename']
            if not destination.is_file() or sha256(destination) != source['sha256']:
                resources.fetch(source['url'], destination, source)
    (ROOT / 'data/pkgs/SHA256SUMS').write_text(''.join(sha256(ROOT / 'data/pkgs' / entry['filename']) + '  ' + entry['filename'] + '\n' for entry in selected))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full', action='store_true')
    args = parser.parse_args()
    collect(args.full)
