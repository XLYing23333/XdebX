"""Hash-pinned downloads, local cache imports and safe archive extraction."""
import base64
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .runtime import PROJECT, InstallError, Cancelled, read_json, sha256, user_directory
from .i18n import tr
from .mirrors import cn_url, original_url
from .recovery import no_symlinks


def safe_extract(path, destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    try:
        archive = tarfile.open(path)
    except (tarfile.TarError, OSError) as exc:
        raise InstallError("Invalid archive: " + str(exc))
    with archive:
        if not archive.getmembers():
            raise InstallError("Empty archive")
        for item in archive.getmembers():
            target = (destination / item.name).resolve()
            if os.path.commonpath([str(destination), str(target)]) != str(destination):
                raise InstallError("Archive path escapes destination")
            if item.isdev() or item.isfifo():
                raise InstallError("Special archive member rejected")
            if item.issym() or item.islnk():
                link = ((target.parent if item.issym() else destination) / item.linkname).resolve()
                if os.path.commonpath([str(destination), str(link)]) != str(destination):
                    raise InstallError("Archive link escapes destination")
        try:
            if sys.version_info >= (3, 12):
                archive.extractall(str(destination), filter="data")
            else:
                archive.extractall(str(destination))
        except (tarfile.TarError, OSError) as exc:
            raise InstallError("Archive extraction failed: " + str(exc))


class Resources:
    def __init__(self, ctx, runner):
        self.ctx, self.runner = ctx, runner
        self.manifest = read_json(PROJECT / "data/pkgs/manifest.json")
        self.cached = self.manifest.get("packages", [])
        self.online = read_json(PROJECT / "data/pkgs/online-sources.json").get("sources", [])
        self.release = read_json(PROJECT / "src/release.json")
        if self.release.get("repository") and not self.release.get("assets"):
            assets = {}
            for entry in self.cached:
                if entry.get("targets") and entry.get("download_enabled") is not False and entry.get("redistribution_allowed") is not False and entry.get("sha256"):
                    name = entry["sha256"][:16] + "-" + Path(entry["filename"]).name
                    assets[entry["filename"]] = {"name": name, "url": "https://github.com/{}/releases/download/{}/{}".format(
                        self.release["repository"], self.release["cache_snapshot"], name)}
            self.release["assets"] = assets
        measured = read_json(PROJECT / "data/pkgs/compatibility.json")
        self.compatibility = measured.get("entries", {})
        self.software_support = measured.get("software", {})
        self.strategy_support = measured.get("strategy_tests", [])

    def entries(self, resource, kind=None, online=False):
        result = []
        for entry in self.online if online else self.cached:
            if entry["id"] != resource or entry.get("download_enabled") is False:
                continue
            if kind and entry.get("kind") != kind:
                continue
            if entry.get("targets") is not None and self.ctx.target not in entry["targets"]:
                continue
            identity = entry.get("filename") or entry["url"] + "#" + entry["version"]
            measured = self.compatibility.get(identity, {}).get(self.ctx.target, {})
            if not getattr(self, "ignore_compatibility", False) and measured and measured.get("status") not in ("not-run", "blocked") and not measured.get("default_enabled", False):
                continue
            result.append(entry)
        if resource == "fastfetch" and self.ctx.target == "ubuntu20":
            result.sort(key=lambda e: "polyfilled" not in e.get("filename", ""))
        if resource == "lsd" and self.ctx.target == "ubuntu20":
            result.sort(key=lambda e: "musl" not in e.get("filename", ""))
        return result

    def validate(self, path, entry):
        expected = entry.get("sha256_expected") or entry.get("sha256")
        if expected and sha256(path) != expected:
            raise InstallError("SHA-256 mismatch: " + Path(path).name)
        integrity = entry.get("integrity_expected") or entry.get("integrity")
        if integrity and integrity.startswith(("sha512-", "sha256-")):
            algorithm, digest = integrity.split("-", 1)
            h = hashlib.new(algorithm)
            with Path(path).open("rb") as stream:
                for data in iter(lambda: stream.read(1048576), b""):
                    h.update(data)
            if base64.b64encode(h.digest()).decode() != digest:
                raise InstallError("Upstream integrity mismatch")
        if not Path(path).stat().st_size:
            raise InstallError("Empty asset")

    def inspect_local(self):
        """Read and hash present assets without fetching or creating user state."""
        verified, missing, errors, retired, on_demand = [], [], [], [], []
        for entry, root in [(e, PROJECT / "data/pkgs") for e in self.cached] + [(e, PROJECT) for e in self.manifest.get("local_assets", [])]:
            name = entry["filename"]
            if entry.get("retired_reason"):
                retired.append(name)
                continue
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                errors.append({"filename": name, "reason": "Invalid resource filename"})
                continue
            path = root / relative
            if not path.is_file():
                if entry.get("bundle") == "on-demand":
                    on_demand.append(name)
                    continue
                missing.append(name)
                continue
            try:
                self.validate(path, entry)
                verified.append(name)
            except (InstallError, OSError) as exc:
                errors.append({"filename": name, "reason": str(exc)})
        return {"verified_count": len(verified), "missing_count": len(missing), "retired_count": len(retired), "on_demand_count": len(on_demand), "errors": errors}

    def fetch(self, url, destination, entry, timeout=600):
        candidates = []
        if self.ctx.settings.get("mirror") == "CN":
            candidates.append(cn_url(url))
        candidates.extend([url, original_url(url)])
        failures = []
        for candidate in dict.fromkeys(candidates):
            try:
                return self._fetch(candidate, destination, entry, timeout)
            except Cancelled:
                raise
            except InstallError as exc:
                failures.append(str(exc))
                self.runner.emit(tr(self.ctx, "download_source_failed", url=candidate))
        raise InstallError("; ".join(failures))

    def _fetch(self, url, destination, entry, timeout):
        if not url.startswith("https://"):
            raise InstallError("Only HTTPS downloads are accepted")
        destination = no_symlinks(destination)
        user_directory(self.ctx, destination.parent)
        part = destination.with_suffix(destination.suffix + ".part")
        no_symlinks(part)
        journal = getattr(self.runner, "journal", None)
        if journal and part.parent == (self.ctx.cache_dir / "downloads").absolute():
            journal.artifact(part, "download-part")
        for attempt in range(2):
            if self.runner.cancel.is_set():
                raise Cancelled("Cancelled")
            try:
                start = time.monotonic()
                self.runner.emit(tr(self.ctx, "download", url=url))
                request = urllib.request.Request(url, headers={"User-Agent": "XdebX/0.1"})
                with urllib.request.urlopen(request, timeout=10) as response, part.open("wb") as stream:
                    if not response.url.startswith("https://"):
                        raise InstallError("Insecure download redirect")
                    total = 0
                    while True:
                        if self.runner.cancel.is_set():
                            raise Cancelled("Cancelled")
                        if time.monotonic() - start > timeout:
                            raise TimeoutError("Download deadline")
                        data = response.read1(65536)
                        if not data:
                            break
                        stream.write(data)
                        total += len(data)
                        if total > max(2 * 1024**3, entry.get("size_bytes") or 0):
                            raise InstallError("Download exceeds size limit")
                self.validate(part, entry)
                os.replace(str(part), str(destination))
                return destination
            except Cancelled:
                raise
            except (OSError, urllib.error.URLError) as exc:
                self.runner.emit(tr(self.ctx, "network_hint", error=str(exc)))
                if attempt == 1:
                    raise InstallError("Download blocked: " + str(exc))
            finally:
                if part.exists():
                    part.unlink()
        raise InstallError("Download failed")

    def get(self, entry, official=False):
        filename = entry.get("filename")
        if filename:
            relative = Path(filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise InstallError("Invalid resource filename")
            local = PROJECT / "data/pkgs" / relative
            imported = self.ctx.cache_dir / "imported" / relative
            pinned = entry.get("sha256_expected") or entry.get("sha256") or entry.get("integrity_expected") or entry.get("integrity")
            if not official or pinned and not getattr(self, "require_official_download", False):
                for path in (local, imported):
                    if path.is_file():
                        try:
                            self.validate(path, entry)
                            return path
                        except InstallError as exc:
                            self.runner.emit(tr(self.ctx, "cache_invalid", filename=path.name, error=str(exc)))
        name = Path(filename).name if filename else Path(urllib.parse.urlparse(entry["url"]).path).name
        expected = entry.get("sha256_expected") or entry.get("sha256") or hashlib.sha256(entry["url"].encode()).hexdigest()
        path = self.ctx.cache_dir / "downloads" / (expected[:16] + "-" + name)
        if path.is_file():
            try:
                self.validate(path, entry)
                return path
            except InstallError as exc:
                no_symlinks(path)
                self.runner.emit(tr(self.ctx, "cache_invalid", filename=path.name, error=str(exc)))
                path.unlink()
        asset = self.release.get("assets", {}).get(filename, {})
        url = entry.get("url") if official else asset.get("url") or entry.get("url")
        if not official and asset and self.ctx.settings.get("resource_mirror"):
            url = self.ctx.settings["resource_mirror"].rstrip("/") + "/" + asset["name"]
        if not url:
            raise InstallError("No downloadable source for asset")
        try:
            return self.fetch(url, path, entry)
        except InstallError:
            if not official and entry.get("url") and entry["url"] != url:
                return self.fetch(entry["url"], path, entry)
            raise

    def import_cache(self, archive):
        with tempfile.TemporaryDirectory(prefix="xdebx-import-") as tmp:
            safe_extract(archive, tmp)
            imported = []
            for entry in self.cached:
                source = Path(tmp) / "data/pkgs" / entry["filename"]
                if source.is_file():
                    self.validate(source, entry)
                    target = self.ctx.cache_dir / "imported" / entry["filename"]
                    user_directory(self.ctx, target.parent)
                    shutil.copy2(str(source), str(target))
                    imported.append(entry["filename"])
            if not imported:
                raise InstallError("No recognized cache assets")
            self.ctx.fix_owner(self.ctx.cache_dir)
            return imported
