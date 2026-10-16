import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from xdebx.catalog import CATALOG, resolve
from xdebx.configuration import Configurator, merge_defaults
from xdebx.installer import Installer
from xdebx.resources import Resources, safe_extract
from xdebx.runtime import PROJECT, Runner, InstallError, Cancelled, atomic_json, redact
from xdebx.ui import fit, width


def context(home):
    return SimpleNamespace(home=home, user="tester", uid=os.getuid(), gid=os.getgid(),
                           config_dir=home / "config", state_dir=home / "state", cache_dir=home / "cache",
                           backups=home / "backups", settings={"language": "EN", "mirror": "Raw"},
                           target="ubuntu20", os={"ID": "ubuntu", "VERSION_CODENAME": "focal"}, desktop=False,
                           env=dict(os.environ, HOME=str(home)), fix_owner=lambda p: None, ensure_supported=lambda: None)


class CoreTests(unittest.TestCase):
    def test_fast_list_and_dependencies(self):
        fast = json.loads((PROJECT / "src/config.json").read_text())["fast_install"]["items"]
        self.assertEqual(len(fast), 25)
        self.assertTrue(set(fast) <= set(CATALOG))
        self.assertEqual(resolve(["openclaw", "claude-code"]), ["nodejs22-nvm", "openclaw", "claude-code"])
        with self.assertRaises(ValueError):
            resolve(["clashterminalx", "clash-verge"])

    def test_templates_preserve_existing_secrets(self):
        merged = merge_defaults({"env": {"TOKEN": "existing"}, "permissions": {"custom": True}},
                                {"env": {"TOKEN": "placeholder", "MODEL": "model"}})
        self.assertEqual(merged["env"], {"TOKEN": "existing", "MODEL": "model"})
        self.assertTrue(merged["permissions"]["custom"])

    def test_backup_conflict_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(Path(tmp))
            engine = Installer(ctx)
            dest = ctx.home / "config.json"
            dest.write_text("old")
            engine.configurator.write(dest, "new")
            record = next(ctx.backups.glob("*/record.json"))
            dest.write_text("user edit")
            with self.assertRaises(InstallError):
                engine.configurator.restore(record)
            engine.configurator.restore(record, force=True)
            self.assertEqual(dest.read_text(), "old")
            self.assertEqual((record.parent / "user-modified").read_text(), "user edit")

    def test_reject_archive_escape_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attack.tar.gz"
            for name, link in (("../escape", None), ("link", "/etc/passwd")):
                with tarfile.open(path, "w:gz") as archive:
                    member = tarfile.TarInfo(name)
                    if link:
                        member.type, member.linkname = tarfile.SYMTYPE, link
                        archive.addfile(member)
                    else:
                        member.size = 1
                        archive.addfile(member, io.BytesIO(b"x"))
                with self.assertRaises(InstallError):
                    safe_extract(path, Path(tmp) / "out")

    def test_version_filter_and_wrong_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(Path(tmp))
            resources = Resources(ctx, Runner(ctx))
            self.assertTrue(all("ubuntu20" in e["targets"] for e in resources.entries("vim", "deb")))
            self.assertIn("polyfilled", resources.entries("fastfetch", "deb")[0]["filename"])
            path = Path(tmp) / "asset"
            path.write_bytes(b"broken")
            with self.assertRaises(InstallError):
                resources.validate(path, {"sha256": "0" * 64})

    def test_official_resource_reuses_only_valid_pinned_cache(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = Resources(context(root), Runner(context(root)))
            cached = root / "data/pkgs/tiny.dat"
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"verified")
            entry = {"filename": "tiny.dat", "sha256": hashlib.sha256(b"verified").hexdigest(), "url": "https://example.invalid/tiny.dat"}
            with patch("xdebx.resources.PROJECT", root), patch.object(resources, "fetch", return_value=root / "downloaded") as fetch:
                self.assertEqual(resources.get(entry, official=True), cached)
                fetch.assert_not_called()
                resources.require_official_download = True
                self.assertEqual(resources.get(entry, official=True), root / "downloaded")
                fetch.assert_called_once()
                fetch.reset_mock()
                resources.require_official_download = False
                cached.write_bytes(b"corrupt")
                self.assertEqual(resources.get(entry, official=True), root / "downloaded")
                fetch.assert_called_once()

    def test_failure_continues_and_dependency_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Installer(context(Path(tmp)))
            progress = []
            engine.progress = lambda done, total: progress.append((done, total))
            def strategy(package, *args):
                if package.id == "nodejs22-nvm":
                    raise InstallError("missing dependency")
            def detected(key):
                return {"status": "installed" if key == "wget" else "available", "detail": "fixture"}
            with patch.object(engine, "strategy", side_effect=strategy), patch.object(engine, "detected", side_effect=detected):
                results = engine.execute(["claude-code", "wget"], configure=False)
            self.assertEqual([r["status"] for r in results], ["failed", "skipped", "installed"])
            # Dependencies count as queue items; strategy retries do not.
            self.assertEqual(progress, [(0, 3), (1, 3), (2, 3), (3, 3)])

    def test_vim8_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Installer(context(Path(tmp)))
            with patch.object(engine, "command", return_value="vim"), patch.object(engine.runner, "probe", return_value=(0, "VIM - Vi IMproved 8.1")):
                self.assertEqual(engine.detected("vim9")["status"], "upgrade-required")

    def test_node_archive_preserves_npm_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(Path(tmp))
            (ctx.home / ".nvm").mkdir()
            (ctx.home / ".nvm/nvm.sh").write_text(":")
            (ctx.home / ".nvm/alias").mkdir()
            (ctx.home / ".nvm/alias/default").write_text("v22.1.0")
            root = ctx.home / "payload/node-test"
            (root / "bin").mkdir(parents=True)
            (root / "lib/npm/bin").mkdir(parents=True)
            (root / "bin/node").write_text("#!/bin/sh\nexit 0\n")
            (root / "bin/node").chmod(0o755)
            (root / "lib/npm/bin/npm.js").write_text("// npm")
            (root / "bin/npm").symlink_to("../lib/npm/bin/npm.js")
            (root / "bin/npx").symlink_to("../lib/npm/bin/npm.js")
            archive = ctx.home / "node.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                output.add(root, arcname="node-test")
            engine = Installer(ctx)
            with patch.object(engine.resources, "get", return_value=archive):
                installed = engine.node(entry_override={"version": "v22.1.0"})
            self.assertTrue((installed / "bin/npm").is_symlink())
            self.assertEqual((installed / "bin/npm").resolve(), installed / "lib/npm/bin/npm.js")

    def test_unicode_layout_and_locales(self):
        self.assertEqual(width("安装 A"), 6)
        self.assertEqual(fit("安装ABC", 5), "安装A")
        en = json.loads((PROJECT / "src/locales/EN.json").read_text())
        cn = json.loads((PROJECT / "src/locales/CN.json").read_text())
        self.assertEqual(set(en), set(cn))

    def test_redaction(self):
        text = redact("API_KEY=secret password:hunter2 https://name:pwd@example.com")
        self.assertNotIn("secret", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("name:pwd", text)

    def test_apt_simulation_rejects_removals(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Installer(context(Path(tmp)))
            with patch.object(engine, "prepare_apt"), patch.object(engine.runner, "run", return_value=(0, "Remv libc6 [old]")) as run:
                with self.assertRaises(InstallError):
                    engine.apt_install(["tool"])
                self.assertEqual(run.call_count, 1)

    def test_mysql_never_detects_mariadb_as_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Installer(context(Path(tmp)))
            with patch.object(engine, "command", return_value="mysql"), patch.object(engine.runner, "probe", return_value=(0, "mysql 15.1 Distrib 10.11-MariaDB")):
                self.assertEqual(engine.detected("mysql")["status"], "unsupported")

    def test_partial_install_is_retried_without_separate_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Installer(context(Path(tmp)))
            ready = [False]
            def detect(key, configuration=True):
                return {"status": "installed" if key == "git" or ready[0] else "partial", "detail": "tmuxp missing"}
            def install(*_):
                ready[0] = True
            with patch.object(engine, "detected", side_effect=detect), patch.object(engine, "strategy", side_effect=install) as strategy:
                result = engine.execute(["tmux-tmuxp"], configure=False)
                strategy.assert_called_once()
                self.assertEqual(result[-1]["status"], "success")

    def test_configuration_cannot_escape_through_parent_symlink(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            ctx = context(Path(tmp))
            engine = Installer(ctx)
            (ctx.home / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(InstallError):
                engine.configurator.write(ctx.home / "linked/settings", "managed")
            self.assertFalse((Path(outside) / "settings").exists())

    def test_node_detection_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(Path(tmp))
            (ctx.home / ".nvm").mkdir()
            (ctx.home / ".nvm/nvm.sh").write_text(":")
            (ctx.home / ".nvm/alias").mkdir()
            (ctx.home / ".nvm/alias/default").write_text("v22.1.0")
            engine = Installer(ctx)
            with patch.object(engine, "command", return_value="node"), patch.object(engine.runner, "probe", return_value=(0, "v22.1.0")):
                self.assertEqual(engine.detected("nodejs22-nvm")["status"], "installed")

    def test_cn_apt_sources_are_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(Path(tmp))
            ctx.settings["mirror"] = "CN"
            original = ctx.home / "etc/apt/sources.list.d"
            original.mkdir(parents=True)
            source = original / "debian.sources"
            text = "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: bookworm\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
            source.write_text(text)
            engine = Installer(ctx)
            def redirect(value):
                string = str(value)
                return ctx.home / string.lstrip("/") if string.startswith("/etc/apt") else Path(value)
            with patch("xdebx.installer.Path", side_effect=redirect), patch.object(engine.runner, "run", return_value=(0, "")):
                engine.prepare_apt()
            generated = Path(engine.apt_directory.name) / "sources.list.d/debian.sources"
            self.assertIn("mirrors.tuna.tsinghua.edu.cn/debian", generated.read_text())
            self.assertIn("Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg", generated.read_text())
            self.assertEqual(source.read_text(), text)
            engine.close()

    def test_cancel_waits_for_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(context(Path(tmp)))
            timer = threading.Timer(0.15, runner.cancel.set)
            timer.start()
            start = time.monotonic()
            try:
                code, _ = runner.run([sys.executable, "-c", "import time;time.sleep(.4);print('finished')"], transaction=True)
                self.assertEqual(code, 0)
                self.assertGreaterEqual(time.monotonic() - start, 0.4)
            finally:
                timer.join()

    def test_cancel_terminates_nontransaction_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(context(Path(tmp)))
            timer = threading.Timer(0.15, runner.cancel.set)
            timer.start()
            try:
                with self.assertRaises(Cancelled):
                    runner.run([sys.executable, "-c", "import time;time.sleep(10)"], timeout=20)
            finally:
                timer.join()

    def test_bad_download_does_not_leave_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(Path(tmp))
            resources = Resources(ctx, Runner(ctx))
            data = io.BytesIO(b"wrong content")
            data.url = "https://example.invalid/asset"
            destination = ctx.home / "downloads/asset"
            with patch("urllib.request.urlopen", return_value=data):
                with self.assertRaises(InstallError):
                    resources.fetch(data.url, destination, {"sha256": "0" * 64})
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix(".part").exists())


if __name__ == "__main__":
    unittest.main()
