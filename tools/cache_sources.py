#!/usr/bin/env python3
"""Collect exact corresponding source alongside redistributable distro caches."""
import hashlib
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from xdebx.runtime import atomic_json, sha256


def fetch(url, destination, digest=None):
    if destination.exists() and (not digest or sha256(destination) == digest):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(destination.suffix + ".part")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "XdebX/0.1"})
        with urllib.request.urlopen(request, timeout=30) as response, part.open("wb") as stream:
            if not response.url.startswith("https://"):
                raise ValueError("Insecure source redirect")
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                stream.write(block)
        if digest and sha256(part) != digest:
            raise ValueError("Corresponding-source checksum mismatch")
        part.replace(destination)
    finally:
        if part.exists():
            part.unlink()


def collect(full=False):
    manifest = json.loads((ROOT / "data/pkgs/manifest.json").read_text())
    output = ROOT / "data/pkgs/corresponding-source"
    report_path = ROOT / "data/pkgs/provenance/corresponding-source.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {"schema_version": 1, "packages": {}}
    for entry in manifest["packages"]:
        if entry.get("download_enabled") is False or entry.get("kind") != "deb" or not (entry.get("source_package") or entry["id"] == "mysql-apt-config") or not entry.get("url"):
            continue
        if entry.get("bundle") == "on-demand" and not full:
            continue
        if not (ROOT / "data/pkgs" / entry["filename"]).is_file():
            raise ValueError("Fetch binary first with tools/cache_packages.py: " + entry["filename"])
        source = entry.get("source_package") or entry["id"]
        control = subprocess.check_output(["dpkg-deb", "-f", str(ROOT / "data/pkgs" / entry["filename"]), "Source"], text=True).strip()
        match = re.search(r"\(([^)]+)\)", control)
        version = match.group(1) if match else entry["version"]
        version_file = version.split(":", 1)[-1]
        parent = entry["url"].rsplit("/", 1)[0] + "/"
        folder = output / source / version_file
        dsc_url = parent + source + "_" + version_file + ".dsc"
        dsc = folder / (source + "_" + version_file + ".dsc")
        record = {"source": source, "version": version, "files": [], "status": "unavailable"}
        try:
            fetch(dsc_url, dsc)
            text = dsc.read_text()
            recorded_version = re.search(r"^Version: (.+)$", text, re.MULTILINE)
            if not recorded_version or recorded_version.group(1) != version:
                raise ValueError("Source version does not match binary")
            hashes = re.search(r"^Checksums-Sha256:\n((?:[ \t].*\n)+)", text, re.MULTILINE)
            if not hashes:
                raise ValueError("No corresponding-source SHA-256 list")
            files = [(dsc, dsc_url)]
            for line in hashes.group(1).splitlines():
                digest, size, name = line.split()
                if Path(name).name != name or name in (".", ".."):
                    raise ValueError("Invalid source member filename")
                asset = folder / name
                url = urllib.parse.urljoin(parent, name)
                fetch(url, asset, digest)
                if asset.stat().st_size != int(size):
                    raise ValueError("Corresponding-source size mismatch")
                files.append((asset, url))
            record.update(status="collected", integrity="Official HTTPS pool; DSC member hashes checked; signer not independently audited",
                          files=[{"filename": str(path.relative_to(ROOT / "data/pkgs")), "sha256": sha256(path), "url": url} for path, url in files])
        except Exception as exc:
            record["reason"] = str(exc)
        report["packages"][entry["filename"]] = record
        atomic_json(report_path, report, 0o644)
        print(entry["filename"] + " " + record["status"] + " " + record.get("reason", ""), flush=True)
    report["notices"] = []
    for resource, url in (
        ("gdu", "https://raw.githubusercontent.com/dundee/gdu/v5.37.0/LICENSE.md"),
        ("uv", "https://raw.githubusercontent.com/astral-sh/uv/0.12.22/LICENSE-MIT"),
        ("uv", "https://raw.githubusercontent.com/astral-sh/uv/0.12.22/LICENSE-APACHE"),
    ):
        destination = ROOT / "data/pkgs/provenance/licenses" / (resource + "-" + Path(url).name)
        try:
            fetch(url, destination)
            report["notices"].append({"resource": resource, "filename": str(destination.relative_to(ROOT / "data/pkgs")), "sha256": sha256(destination), "url": url})
        except Exception as exc:
            report["notices"].append({"resource": resource, "status": "unavailable", "reason": str(exc)})
    btop = next(entry for entry in manifest["packages"] if entry["id"] == "btop")
    url = "https://codeload.github.com/aristocratos/btop/tar.gz/refs/tags/v" + btop["version"].lstrip("v")
    file = output / "btop" / ("btop-" + btop["version"].lstrip("v") + ".tar.gz")
    try:
        fetch(url, file)
        report["packages"][btop["filename"]] = {"status": "collected", "source": "btop", "version": btop["version"],
            "files": [{"filename": str(file.relative_to(ROOT / "data/pkgs")), "sha256": sha256(file), "url": url}]}
    except Exception as exc:
        report["packages"][btop["filename"]] = {"status": "unavailable", "reason": str(exc)}
    atomic_json(report_path, report, 0o644)
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="Collect corresponding source for the complete cache")
    args = parser.parse_args()
    collect(args.full)
