#!/usr/bin/env python3
"""Publish verified local assets through draft GitHub Releases."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from xdebx import __version__
from xdebx.runtime import sha256


def gh(args, **kwargs):
    return subprocess.run(["gh"] + args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "dist")
    parser.add_argument("--results", type=Path, default=ROOT / "test-results/docker/summary.json")
    parser.add_argument("--publish", action="store_true", help="Publish completed drafts")
    parser.add_argument("--redistribution-reviewed", action="store_true")
    args = parser.parse_args()
    results = json.loads(args.results.read_text())
    if not results or any(r["status"] in ("not-run", "failed") for r in results):
        parser.error("All declared cases must be run; unresolved failures prevent publishing")
    if not args.redistribution_reviewed:
        parser.error("Review third-party redistribution notices before uploading binaries")
    directory = args.directory
    index = json.loads((directory / "resources.json").read_text())
    if len(list(directory.glob("xdebx-v*-linux-amd64.tar.gz"))) != 1:
        parser.error("Expected exactly one program archive")
    for row in results:
        if row["status"] == "passed":
            evidence = args.results.parent / row["case_id"]
            if not row.get("fixture_id") or not (evidence / "commands.json").exists():
                parser.error("Passed case needs final command/image evidence; revalidate: " + row["case_id"])
    repository, snapshot = index["repository"], index["cache_snapshot"]
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        if sha256(directory / name) != digest:
            parser.error("Checksum mismatch: " + name)
    program = "v" + __version__
    for tag in (snapshot, program):
        view = subprocess.run(["gh", "release", "view", tag, "--repo", repository, "--json", "isDraft"], capture_output=True, text=True)
        if view.returncode == 0:
            if not json.loads(view.stdout)["isDraft"]:
                parser.error("Snapshot already published; create a new tag instead of replacing bytes")
        else:
            gh(["release", "create", tag, "--repo", repository, "--draft", "--latest=false", "--title", tag,
                "--notes", "XdebX verified release. Cache assets are not a complete offline dependency repository."])
    cache = list((directory / "cache-assets").iterdir()) + list(directory.glob("xdebx-cache-*.tar.gz")) + [directory / "resources.json", directory / "SHA256SUMS-cache"]
    program_files = list(directory.glob("xdebx-v*-linux-amd64.tar.gz")) + list(directory.glob("xdebx-music.tar.gz")) + [directory / "SHA256SUMS-program"]
    for tag, files in ((snapshot, cache), (program, program_files)):
        for start in range(0, len(files), 30):
            gh(["release", "upload", tag, "--repo", repository, "--clobber"] + [str(p) for p in files[start:start + 30]])
        # Download the draft assets and validate the remote bytes before publish.
        import tempfile
        with tempfile.TemporaryDirectory(prefix="xdebx-release-verify-") as tmp:
            gh(["release", "download", tag, "--repo", repository, "--dir", tmp])
            for source in files:
                if sha256(source) != sha256(Path(tmp) / source.name):
                    raise RuntimeError("Uploaded asset differs: " + source.name)
    if args.publish:
        gh(["release", "edit", snapshot, "--repo", repository, "--draft=false", "--latest=false"])
        gh(["release", "edit", program, "--repo", repository, "--draft=false", "--latest"])
    print("https://github.com/{}/releases".format(repository))


if __name__ == "__main__":
    main()
