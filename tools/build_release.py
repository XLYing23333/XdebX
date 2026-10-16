#!/usr/bin/env python3
"""Build lightweight program/cache releases. Never uploads or installs anything."""
import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from xdebx import __version__
from xdebx.runtime import sha256, atomic_json


def archive(destination, files):
    with tarfile.open(destination, "w:gz", compresslevel=6) as output:
        seen = {}
        for source, name in files:
            if name in seen:
                if Path(source) != seen[name]:
                    raise ValueError("Conflicting archive member: " + name)
                continue
            seen[name] = Path(source)
            output.add(str(source), arcname=name, recursive=False)


def build(repository, snapshot, out, music=False, full_cache=False):
    if out.exists() and any(out.iterdir()):
        raise ValueError("Release output must be empty to prevent stale attachments: " + str(out))
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / "data/pkgs/manifest.json").read_text())
    compatibility_path = ROOT / "data/pkgs/compatibility.json"
    compatibility = json.loads(compatibility_path.read_text()).get("entries", {}) if compatibility_path.exists() else {}
    sources_path = ROOT / "data/pkgs/provenance/corresponding-source.json"
    source_report = json.loads(sources_path.read_text()) if sources_path.exists() else {}
    source_records = source_report.get("packages", {})
    notices = source_report.get("notices", [])
    corresponding = {}
    targets = {"ubuntu20", "ubuntu22", "ubuntu24", "debian12", "debian13"}
    groups = {name: [] for name in ["common"] + sorted(targets)}
    index = {"schema_version": 1, "repository": repository, "cache_snapshot": snapshot, "assets": {}, "excluded": {}, "on_demand": {}}
    attachments = out / "cache-assets"
    attachments.mkdir(exist_ok=True)
    for entry in manifest["packages"]:
        if entry.get("download_enabled") is False or entry.get("redistribution_allowed") is False or not entry.get("targets") or entry["id"] == "CTypora":
            index["excluded"][entry["filename"]] = entry.get("distribution") or "Historical or unverified asset"
            continue
        if entry.get("bundle") == "on-demand" and not full_cache:
            index["on_demand"][entry["filename"]] = {"url": entry.get("url"), "sha256": entry.get("sha256")}
            continue
        measured = compatibility.get(entry["filename"], {})
        applicable = [target for target in entry["targets"]
                      if target not in measured or measured[target].get("status") == "not-run" or measured[target].get("default_enabled")]
        if not applicable:
            index["excluded"][entry["filename"]] = "No target passed the required capability checks"
            continue
        needs_source = entry.get("source_package") or entry["id"] in ("btop", "mysql-apt-config")
        if needs_source:
            record = source_records.get(entry["filename"], {})
            if record.get("status") != "collected":
                index["excluded"][entry["filename"]] = "Exact corresponding source not collected for public redistribution"
                continue
            for asset in record["files"]:
                file = ROOT / "data/pkgs" / asset["filename"]
                if sha256(file) != asset["sha256"]:
                    raise ValueError("Corresponding source checksum mismatch")
                corresponding[asset["filename"]] = (file, asset["filename"])
        source = ROOT / "data/pkgs" / entry["filename"]
        if not source.is_file():
            raise ValueError("Missing asset: " + str(source))
        digest = sha256(source)
        if digest != entry["sha256"]:
            raise ValueError("Asset checksum changed: " + str(source))
        name = digest[:16] + "-" + source.name
        shutil.copy2(str(source), str(attachments / name))
        asset = {"name": name, "sha256": digest, "targets": applicable,
                 "url": "https://github.com/{}/releases/download/{}/{}".format(repository, snapshot, name)}
        if needs_source:
            asset["corresponding_source"] = "xdebx-{}-corresponding-source.tar.gz".format(snapshot)
        asset_notices = [notice for notice in notices if notice["resource"] == entry["id"]]
        if any(notice.get("status") == "unavailable" for notice in asset_notices):
            raise ValueError("Missing third-party notices: " + entry["id"])
        if asset_notices:
            asset["notices"] = "xdebx-{}-notices.tar.gz".format(snapshot)
        index["assets"][entry["filename"]] = asset
        members = [(source, "data/pkgs/" + entry["filename"])]
        for notice in asset_notices:
            text = ROOT / "data/pkgs" / notice["filename"]
            if sha256(text) != notice["sha256"]:
                raise ValueError("Third-party notice checksum mismatch")
            members.append((text, "data/pkgs/" + notice["filename"]))
        if set(applicable) == targets:
            groups["common"].extend(members)
        else:
            for target in applicable:
                groups[target].extend(members)
    atomic_json(out / "resources.json", index, 0o644)
    if corresponding:
        archive(out / ("xdebx-{}-corresponding-source.tar.gz".format(snapshot)), list(corresponding.values()) + [(sources_path, "provenance.json")])
    if notices:
        archive(out / ("xdebx-{}-notices.tar.gz".format(snapshot)), [(ROOT / "data/pkgs" / notice["filename"], notice["filename"]) for notice in notices])
    for name, files in groups.items():
        if files:
            archive(out / ("xdebx-{}-{}.tar.gz".format(snapshot, name)), files + [(out / "resources.json", "resources.json")])
    files = [(ROOT / "run_xdebx.sh", "run_xdebx.sh")]
    for folder in ("src", "data/confs", "assets"):
        files.extend((p, str(p.relative_to(ROOT))) for p in (ROOT / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.name != "release.json")
    for name in ("README.md", "README-CN.md", "data/pkgs/manifest.json", "data/pkgs/online-sources.json", "data/fonts/SHA256SUMS", "data/fonts/LICENSE.txt", "data/pkgs/compatibility.json"):
        if not (ROOT / name).exists():
            continue
        files.append((ROOT / name, name))
    for asset in manifest["local_assets"]:
        source = ROOT / asset["filename"]
        if sha256(source) != asset["sha256"]:
            raise ValueError("Font checksum mismatch")
        files.append((source, asset["filename"]))
    for notice in notices:
        files.append((ROOT / "data/pkgs" / notice["filename"], "data/pkgs/" + notice["filename"]))
    files.append((out / "resources.json", "src/release.json"))
    archive(out / ("xdebx-v{}-linux-amd64.tar.gz".format(__version__)), files)
    if music:
        source = ROOT / "data/music/電脳眠眠猫short.mp3"
        archive(out / "xdebx-music.tar.gz", [(source, "data/music/" + source.name)])
    program_members = list(out.glob("xdebx-v*-linux-amd64.tar.gz")) + list(out.glob("xdebx-music.tar.gz"))
    cache_members = list(attachments.iterdir()) + list(out.glob("xdebx-cache-*.tar.gz")) + [out / "resources.json"]
    for group, members in (("program", program_members), ("cache", cache_members)):
        (out / ("SHA256SUMS-" + group)).write_text("".join(sha256(p) + "  " + p.name + "\n" for p in sorted(members)))
    all_files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (out / "SHA256SUMS").write_text("".join(sha256(p) + "  " + str(p.relative_to(out)) + "\n" for p in all_files))
    return index


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="GitHub owner/repository")
    parser.add_argument("--snapshot", default="cache-2026.10.03.1")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--full-cache", action="store_true", help="Include all enabled assets; obtain them with tools/cache_packages.py --full first")
    parser.add_argument("--include-music", action="store_true", help="Use only after checking redistribution permission")
    args = parser.parse_args()
    if len(args.repository.split("/")) != 2 or any(not x for x in args.repository.split("/")):
        parser.error("Expected owner/repository")
    build(args.repository, args.snapshot, args.output, args.include_music, args.full_cache)
    print(args.output)
