"""Distribution-aware strategies and a failure-isolated installation queue."""
import fcntl
import json
import os
import re
import shlex
import shutil
import tempfile
import time
from pathlib import Path

from .catalog import CATALOG, resolve
from .configuration import Configurator
from .resources import Resources, safe_extract
from .runtime import PROJECT, InstallError, Unsupported, Cancelled, Runner, atomic_json, read_json, sha256, user_directory
from .i18n import tr
from .recovery import Journal, recover
from .mirrors import CN_BASES, original_url


class Installer:
    def __init__(self, ctx, emit=None):
        self.ctx = ctx
        self.runner = Runner(ctx, emit)
        self.progress = None
        self.resources = Resources(ctx, self.runner)
        self.configurator = Configurator(ctx, self.runner, self.resources)
        self.apt_options = []
        self.apt_ready = False
        self.apt_directory = None
        self.apt_mirror = ctx.settings["mirror"]
        self.apt_using_raw = False
        self.apt_originals = {}

    def close(self):
        if self.apt_directory:
            self.apt_directory.cleanup()

    def track_apt_directory(self):
        if self.runner.journal and self.apt_directory:
            directory = Path(self.apt_directory.name)
            self.runner.journal.artifact(directory, "apt-directory")
            atomic_json(directory / ".xdebx-owner.json", {"run_id": self.runner.journal.record["run_id"]}, 0o644)

    def discovery(self):
        """A read-only detector gets its own runner, cancellation and command records."""
        detector = Installer(self.ctx)
        for flag in ("ignore_compatibility", "prefer_snapshots"):
            if hasattr(self.resources, flag):
                setattr(detector.resources, flag, getattr(self.resources, flag))
        return detector

    def command(self, package):
        home = self.ctx.home
        candidates = [home / ".local/bin" / package.command]
        if package.id == "miniconda3":
            candidates.append(home / "Software/miniconda3/bin/conda")
        if package.id == "go":
            candidates.extend([home / "Software/go/bin/go", Path("/opt/go/bin/go")])
        if package.id == "jdk":
            candidates.append(home / "Software/jdk/bin/javac")
        if package.id == "nomachine":
            candidates.append(Path("/usr/NX/bin/nxserver"))
        for path in candidates:
            if path.is_file() and os.access(str(path), os.X_OK):
                return str(path)
        return shutil.which(package.command, path=self.ctx.env["PATH"])

    def detected(self, key, configuration=True):
        package = CATALOG[key]
        if self.ctx.target in package.excluded_targets:
            return {"status": "unsupported", "detail": "Excluded from this target: " + self.ctx.target}
        if package.desktop and not self.ctx.desktop:
            return {"status": "unsupported", "detail": "No desktop environment"}
        if key == "chatgpt-desktop" and self.ctx.target not in ("ubuntu24", "debian13"):
            return {"status": "unsupported", "detail": "Official preview does not support this target"}
        if key == "ghostty" and self.ctx.target == "ubuntu20":
            return {"status": "unsupported", "detail": "No verified focal binary; native GTK4 build requirements unavailable"}
        if key == "maple-mono-nf":
            files = read_json(PROJECT / "src/config.json")["fonts"]["files"]
            for name in files:
                target = self.ctx.home / ".local/share/fonts/MapleMono-NF" / Path(name).name
                if not target.is_file():
                    return {"status": "available", "detail": "Font files missing"}
                expected = next(e["sha256"] for e in self.resources.manifest["local_assets"] if e["filename"] == name)
                if sha256(target) != expected:
                    return {"status": "available", "detail": "Font files differ"}
            return {"status": "installed", "detail": "Four font files verified"}
        command = self.command(package)
        if not command:
            measured = self.resources.software_support.get(key, {}).get(self.ctx.target, {})
            if not getattr(self.resources, "ignore_compatibility", False) and measured.get("status") == "unavailable" and not any(case.get("status") == "blocked" for case in measured.get("cases", [])):
                return {"status": "unsupported", "detail": "No candidate passed acceptance on " + self.ctx.target}
            return {"status": "available", "detail": "Not installed"}
        if package.desktop:
            deb_name = {"vscode": "code", "chrome": "google-chrome-stable", "CTypora": "typora",
                        "ghostty-ubuntu": "ghostty", "clash-verge-rev": "clash-verge",
                        "nomachine-personal-edition": "nomachine-personal-edition", "qq": "linuxqq"}.get(package.resource, package.id)
            _, status = self.runner.probe(["dpkg-query", "-W", "-f=${Status}\n${Version}", deb_name])
            if status.startswith("install ok installed\n"):
                return {"status": "installed", "detail": status.splitlines()[-1]}
            if package.id == "snipaste" and (self.ctx.home / ".local/share/xdebx/apps/snipaste/AppRun").exists():
                return {"status": "installed", "detail": "Verified AppImage extraction present"}
            # Do not launch a GUI process while doing a read-only discovery pass.
            return {"status": "partial", "detail": "Executable exists; managed installation not confirmed"}
        code, text = self.runner.probe([command] + package.probe)
        if code:
            # GUI applications sometimes lack --version; a configured dpkg package
            # plus executable is sufficient for discovery, not acceptance testing.
            if package.desktop and package.resource not in ("snipaste", "CTypora"):
                deb_name = {"vscode": "code", "chrome": "google-chrome-stable", "wechat": "wechat", "qq": "linuxqq"}.get(package.resource, package.resource)
                _, status = self.runner.probe(["dpkg-query", "-W", "-f=${Status}", deb_name])
                if status.strip() == "install ok installed":
                    return {"status": "installed", "detail": "Package installed; desktop smoke test separate"}
            return {"status": "available", "detail": "Executable probe failed"}
        if key == "vim9":
            match = re.search(r"Vi IMproved\s+(\d+)", text)
            if not match or int(match.group(1)) < 9:
                return {"status": "upgrade-required", "detail": text.splitlines()[0] if text else "Vim <9"}
        if key == "mysql" and "MariaDB" in text:
            return {"status": "unsupported", "detail": "MariaDB exists; MySQL data migration requires an explicit separate operation"}
        if key == "nodejs22-nvm":
            if not re.match(r"v22\.", text.strip()):
                return {"status": "upgrade-required", "detail": "Node.js 22 required"}
            if not (self.ctx.home / ".nvm/nvm.sh").exists():
                return {"status": "partial", "detail": "Node 22 exists; NVM missing"}
            if configuration:
                alias = self.ctx.home / ".nvm/alias/default"
                if not alias.is_file() or not alias.read_text().strip():
                    return {"status": "partial", "detail": "Node 22 exists; NVM default alias missing"}
                if not re.match(r"^v?22(?:\.|$)", alias.read_text().strip()):
                    code, default = self.runner.probe(["bash", "-c", '. "$HOME/.nvm/nvm.sh"; nvm version default'])
                    if code or not default.strip().startswith("v22."):
                        return {"status": "partial", "detail": "NVM default runtime is not Node 22"}
        if key == "jdk" and not re.match(r"javac 21\.", text.strip()):
            return {"status": "upgrade-required", "detail": "JDK 21 required"}
        if key == "tmux-tmuxp" and self.runner.probe(["tmuxp", "-V"])[0]:
            return {"status": "partial", "detail": "tmux installed; tmuxp missing"}
        if configuration and self.configurator.missing(key):
            return {"status": "partial", "detail": "Executable works; configuration or plugin entry points missing/invalid"}
        if key == "docker":
            for component in ("dockerd", "containerd", "runc"):
                if self.runner.probe([component, "--version"])[0]:
                    return {"status": "partial", "detail": "Docker component missing: " + component}
            if configuration and self.runner.probe([command, "info", "--format", "{{.ServerVersion}}"])[0]:
                return {"status": "partial", "detail": "Docker installed; daemon unavailable or socket permission denied"}
        return {"status": "installed", "detail": (text.splitlines() or [command])[0]}

    def configure(self, key):
        """Supplement missing components without reinstalling an existing binary."""
        self.configurator.github_unavailable = False
        self.configurator.git_source_failures.clear()
        state = self.detected(key)
        if state["status"] not in ("installed", "partial"):
            raise InstallError("Install software before configuring it")
        if key == "tmux-tmuxp" and state["status"] == "partial":
            self.apt_install(["tmuxp"])
        elif key == "nodejs22-nvm" and state["status"] == "partial":
            self.node(22)
        self.configurator.apply(key)
        verified = self.detected(key)
        if verified["status"] != "installed":
            raise InstallError("Configuration incomplete: " + verified["detail"])

    def repair_node_alias(self):
        alias = self.ctx.home / ".nvm/alias/default"
        if alias.exists():
            return  # Preserve existing user choices, including invalid ones for manual review.
        candidates = sorted((self.ctx.home / ".nvm/versions/node").glob("v22.*"),
                            key=lambda p: tuple(int(n) for n in re.findall(r"\d+", p.name)), reverse=True)
        for candidate in candidates:
            binary = candidate / "bin/node"
            code, version = self.runner.probe([binary, "--version"])
            if code == 0 and version.strip().startswith("v22."):
                self.configurator.write(alias, candidate.name + "\n")
                return
        self.node(22)

    def prepare_apt(self):
        if self.apt_mirror != self.ctx.settings["mirror"]:
            self.close()
            self.apt_directory, self.apt_options, self.apt_ready = None, [], False
            self.apt_mirror = self.ctx.settings["mirror"]
            self.apt_using_raw = False
        if self.apt_ready:
            return
        if self.ctx.settings["mirror"] == "CN" and not self.apt_using_raw:
            self.apt_directory = tempfile.TemporaryDirectory(prefix="xdebx-apt-")
            self.track_apt_directory()
            os.chmod(self.apt_directory.name, 0o755)
            directory = Path(self.apt_directory.name)
            parts = directory / "sources.list.d"
            parts.mkdir(mode=0o755)
            for path in [Path("/etc/apt/sources.list")] + list(Path("/etc/apt/sources.list.d").glob("*")):
                if not path.is_file() or path.suffix not in (".list", ".sources"):
                    continue
                text = path.read_text()
                original_text = text
                for original, mirror in (("http://archive.ubuntu.com/ubuntu", "https://mirrors.tuna.tsinghua.edu.cn/ubuntu"),
                                         ("http://security.ubuntu.com/ubuntu", "https://mirrors.tuna.tsinghua.edu.cn/ubuntu"),
                                         ("http://deb.debian.org/debian-security", "https://mirrors.tuna.tsinghua.edu.cn/debian-security"),
                                         ("http://deb.debian.org/debian", "https://mirrors.tuna.tsinghua.edu.cn/debian")):
                    text = text.replace(original, mirror).replace(original.replace("http:", "https:"), mirror)
                destination = directory / "sources.list" if path.name == "sources.list" else parts / path.name
                self.apt_originals[str(destination)] = original_text
                destination.write_text(text)
                destination.chmod(0o644)
            self.apt_options = ["-o", "Dir::Etc::sourcelist=" + str(directory / "sources.list"),
                                "-o", "Dir::Etc::sourceparts=" + str(parts)]
        self.update_apt()
        self.apt_ready = True

    def raw_apt_fallback(self):
        if self.ctx.settings["mirror"] != "CN" or self.apt_using_raw:
            return False
        self.runner.emit(tr(self.ctx, "mirror_fallback", kind="APT"))
        if self.apt_directory:
            folder = Path(self.apt_directory.name)
            for path in [folder / "sources.list"] + list((folder / "sources.list.d").glob("*")):
                if not path.is_file() or path.suffix not in (".list", ".sources"):
                    continue
                text = self.apt_originals.get(str(path), path.read_text())
                for original, mirror in CN_BASES:
                    text = text.replace(mirror, original)
                path.write_text(text)
        self.apt_using_raw = True
        self.apt_ready = False
        return True

    def update_apt(self):
        args = ["-o", "DPkg::Lock::Timeout=30", "-o", "Acquire::Retries=1",
                "-o", "Acquire::http::Timeout=15", "-o", "Acquire::https::Timeout=15",
                "-o", "APT::Update::Error-Mode=any", "update"]
        try:
            self.runner.run(["apt-get"] + self.apt_options + args, root=True, timeout=180)
        except Cancelled:
            raise
        except InstallError:
            if not self.raw_apt_fallback():
                raise
            self.runner.run(["apt-get"] + self.apt_options + args, root=True, timeout=180)

    def apt_install(self, packages):
        self.prepare_apt()
        command = ["apt-get"] + self.apt_options + ["-o", "DPkg::Lock::Timeout=30", "-o", "Acquire::Retries=1",
                   "-o", "Acquire::http::Timeout=15", "-o", "Acquire::https::Timeout=15", "--no-install-recommends"]
        try:
            _, simulation = self.runner.run(command + ["-s", "install"] + [str(p) for p in packages], root=True, timeout=120)
        except Cancelled:
            raise
        except InstallError:
            if not self.raw_apt_fallback():
                raise
            self.update_apt()
            self.apt_ready = True
            return self.apt_install(packages)
        if re.search(r"^Remv\s", simulation, re.MULTILINE):
            raise InstallError("Dependency resolution would remove installed packages")
        try:
            self.runner.run(command + ["-y", "--no-remove", "install"] + [str(p) for p in packages], root=True, transaction=True)
        except Cancelled:
            raise
        except InstallError:
            if not self.raw_apt_fallback():
                raise
            self.update_apt()
            self.apt_ready = True
            self.apt_install(packages)

    def deb(self, entry, official=False, extra=None):
        path = self.resources.get(entry, official=official)
        _, architecture = self.runner.run(["dpkg-deb", "-f", path, "Architecture"])
        if architecture.strip() not in ("amd64", "all"):
            raise InstallError("Wrong deb architecture")
        debs = [path]
        companions = {"vim": ["vim-runtime"], "git": ["git-man"], "zsh": ["zsh-common"]}.get(entry["id"], [])
        for companion in companions:
            for candidate in self.resources.entries(companion, "deb"):
                if candidate["version"] == entry["version"]:
                    debs.append(self.resources.get(candidate, official=official))
                    break
        self.apt_install(debs + list(extra or []))

    def install_archive(self, entry, command, official=False):
        path = self.resources.get(entry, official=official)
        with self.runner.temporary(prefix="xdebx-unpack-") as tmp:
            safe_extract(path, tmp)
            names = {"gdu": ("gdu", "gdu_linux_amd64_static"), "btop": ("btop",),
                     "uv": ("uv",), "fastfetch": ("fastfetch",)}[command]
            found = [p for p in Path(tmp).rglob("*") if p.is_file() and p.name in names and os.access(str(p), os.X_OK)]
            if not found:
                raise InstallError("Executable missing in archive")
            binary = found[0]
            if command == "fastfetch" and "musl" in path.name and not Path("/lib/ld-musl-x86_64.so.1").exists():
                self.apt_install(["musl"])
            dest = self.ctx.home / ".local/bin" / command
            self.configurator.write(dest, binary.read_bytes(), 0o755)
            if command == "uv":
                sibling = binary.parent / "uvx"
                if sibling.exists():
                    self.configurator.write(dest.with_name("uvx"), sibling.read_bytes(), 0o755)
            if command == "btop":
                themes = binary.parent.parent / "themes"
                if themes.is_dir():
                    target = self.ctx.home / ".local/share/btop/themes"
                    target.mkdir(parents=True, exist_ok=True)
                    for item in themes.iterdir():
                        shutil.copy2(str(item), str(target / item.name))

    def node(self, major=22, official=False, entry_override=None):
        home = self.ctx.home
        nvm = home / ".nvm"
        if not (nvm / "nvm.sh").exists():
            entry = self.resources.entries("nvm", "source")[0]
            with self.runner.temporary(prefix="xdebx-nvm-") as tmp:
                safe_extract(self.resources.get(entry, official=official), tmp)
                root = next(Path(tmp).iterdir())
                if nvm.exists():
                    raise InstallError("Existing incomplete NVM directory; repair separately")
                self.runner.install_tree(root, nvm)
                self.ctx.fix_owner(nvm)
        resource = "nodejs" if major == 22 else "nodejs-openclaw-runtime"
        entry = entry_override or self.resources.entries(resource, "archive")[0]
        version = entry["version"]
        if not version.startswith("v"):
            version = "v" + version
        target = nvm / "versions/node" / version
        if not (target / "bin/node").exists():
            with self.runner.temporary(prefix="xdebx-node-") as tmp:
                safe_extract(self.resources.get(entry, official=official), tmp)
                source = next(p for p in Path(tmp).iterdir() if p.is_dir())
                user_directory(self.ctx, target.parent)
                self.runner.install_tree(source, target)
                self.ctx.fix_owner(target)
        self.runner.run([target / "bin/node", "-e", "console.log('node runtime ready')"])
        if major == 22:
            aliases = nvm / "alias"
            user_directory(self.ctx, aliases)
            self.configurator.write(aliases / "default", version + "\n")
            directory = home / ".local/bin"
            user_directory(self.ctx, directory)
            for name in ("node", "npm", "npx"):
                destination = directory / name
                if destination.exists() or destination.is_symlink():
                    if destination.is_symlink() and destination.resolve() == (target / "bin" / name).resolve():
                        continue
                    raise InstallError("Existing user executable: " + str(destination))
                destination.symlink_to(target / "bin" / name)
                self.ctx.fix_owner(directory)
        return target

    def npm(self, package, official=False, entry_override=None):
        runtime = self.node(24 if package.id == "openclaw" else 22)
        entries = self.resources.entries(package.resource, online=True)
        entry = entry_override or entries[0]
        spec = package.resource + "@" + entry["version"]
        if official:
            spec = str(self.resources.get(entry, official=True))
        else:
            cached = self.resources.entries(package.resource, "npm-package")
            if cached:
                spec = str(self.resources.get(cached[0]))
        registry = "https://registry.npmmirror.com" if self.ctx.settings["mirror"] == "CN" else "https://registry.npmjs.org"
        env = {"PATH": str(runtime / "bin") + ":" + self.ctx.env["PATH"]}
        args = [runtime / "bin/npm", "install", "-g", "--registry", registry,
                "--fetch-retries=1", "--fetch-timeout=15000", spec]
        try:
            self.runner.run(args, env=env, timeout=300)
        except Cancelled:
            raise
        except InstallError:
            if self.ctx.settings["mirror"] != "CN":
                raise
            self.runner.emit(tr(self.ctx, "mirror_fallback", kind="npm"))
            args[args.index(registry)] = "https://registry.npmjs.org"
            self.runner.run(args, env=env, timeout=300)
        if package.id == "openclaw":
            script = '#!/bin/sh\nexport PATH={}:"$PATH"\nexec {} "$@"\n'.format(
                shlex.quote(str(runtime / "bin")), shlex.quote(str(runtime / "bin/openclaw")))
            self.configurator.write(self.ctx.home / ".local/bin/openclaw", script, 0o755)
        else:
            source = runtime / "bin" / package.command
            destination = self.ctx.home / ".local/bin" / package.command
            if destination.is_symlink() and destination.resolve() == source.resolve():
                return
            if destination.exists() or destination.is_symlink():
                raise InstallError("Existing executable: " + str(destination))
            destination.symlink_to(source)

    def source(self, package, entry=None):
        resource = package.source
        sources = [entry] if entry else self.resources.entries(resource, "source")
        if sources:
            entry = sources[0]
            deps = {"vim9": ["build-essential", "libncurses-dev"],
                    "tmux-tmuxp": ["build-essential", "bison", "libevent-dev", "libncurses-dev", "pkg-config"],
                    "chafa": ["build-essential", "libglib2.0-dev", "libfreetype6-dev", "libjpeg-dev", "pkg-config"]}.get(package.id)
            if not deps:
                raise InstallError("No source build recipe for " + package.id)
            self.apt_install(deps)
            with self.runner.temporary(prefix="xdebx-build-") as tmp:
                safe_extract(self.resources.get(entry), tmp)
                source = next(p for p in Path(tmp).iterdir() if p.is_dir())
                source.chmod(0o755)
                # Build as the target user, including when application is invoked with sudo.
                os.chmod(tmp, 0o755)
                self.ctx.fix_owner(source)
                flags = ["--prefix=" + str(self.ctx.home / ".local")]
                if package.id == "vim9":
                    flags += ["--with-features=huge", "--enable-gui=no", "--without-x"]
                self.runner.run(["./configure"] + flags, cwd=str(source))
                self.runner.run(["make", "-j", str(min(os.cpu_count() or 1, 4))], cwd=str(source), timeout=3600)
                self.runner.run(["make", "install"], cwd=str(source))
            if package.id == "tmux-tmuxp":
                self.apt_install(["tmuxp"])
            return
        if not resource:
            raise Unsupported("No vetted source-build recipe for " + package.id)
        self.prepare_source_apt()
        native = package.apt[0] if package.apt else package.id
        _, available = self.runner.run(["apt-cache"] + self.apt_options + ["showsrc", native], check=False)
        if not re.search(r"^Package:\s", available, re.MULTILINE):
            raise Unsupported("No distribution-native source package for " + native + " on " + self.ctx.target)
        self.apt_install(["build-essential", "dpkg-dev", "fakeroot"])
        with self.runner.temporary(prefix="xdebx-native-source-") as tmp:
            os.chmod(tmp, 0o777)
            self.runner.run(["apt-get"] + self.apt_options + ["source", package.apt[0] if package.apt else package.id], cwd=tmp)
            source = next(p for p in Path(tmp).iterdir() if p.is_dir())
            if package.id == "git":
                # The CLI does not need distro GUI, web-server or documentation
                # build dependencies. Match Debian's non-OpenSSL hashing build.
                self.apt_install(["libcurl4-gnutls-dev", "zlib1g-dev", "libexpat1-dev"])
                flags = ["prefix=" + str(self.ctx.home / ".local"), "NO_TCLTK=YesPlease", "NO_OPENSSL=YesPlease",
                         "NO_GETTEXT=YesPlease", "NO_INSTALL_HARDLINKS=YesPlease"]
                self.runner.run(["make", "-j", str(min(os.cpu_count() or 1, 4)), "all"] + flags, cwd=str(source), timeout=3600)
                self.runner.run(["make", "install"] + flags, cwd=str(source))
                return
            if package.id == "zsh-omz":
                # Build the shell, modules and completion functions directly;
                # old distro helpfile generation is not required by the CLI.
                self.apt_install(["libncurses-dev", "autoconf"])
                self.runner.run(["./configure", "--prefix=" + str(self.ctx.home / ".local"), "--with-tcsetpgrp"], cwd=str(source))
                self.runner.run(["make", "-j", str(min(os.cpu_count() or 1, 4))], cwd=str(source), timeout=3600)
                self.runner.run(["make", "install.bin", "install.modules", "install.fns"], cwd=str(source))
                return
            build_deps = ["apt-get"] + self.apt_options + ["-o", "APT::Build-Profiles=nocheck", "-o", "DPkg::Lock::Timeout=30",
                                                        "-o", "Acquire::Retries=2", "--no-install-recommends"]
            _, simulated = self.runner.run(build_deps + ["-s", "build-dep", native], root=True)
            if re.search(r"^Remv\s", simulated, re.MULTILINE):
                raise InstallError("Source dependencies would remove installed packages")
            self.runner.run(build_deps + ["-y", "--no-remove", "build-dep", native], root=True, transaction=True)
            self.runner.run(["dpkg-buildpackage", "-us", "-uc", "-b"], cwd=str(source), timeout=3600,
                            env={"DEB_BUILD_OPTIONS": "nocheck parallel=" + str(min(os.cpu_count() or 1, 4)), "DEB_BUILD_PROFILES": "nocheck"})
            debs = list(Path(tmp).glob("*.deb"))
            if not debs:
                raise InstallError("Source build produced no package")
            # A source package can emit mutually exclusive development variants.
            # Install the requested binaries and only their built runtime closure.
            from email.parser import Parser
            outputs = {}
            for deb in debs:
                _, control = self.runner.run(["dpkg-deb", "-f", deb], quiet=True)
                fields = Parser().parsestr(control)
                outputs[fields["Package"]] = (deb, fields)
            selected = [name for name in package.apt if name in outputs]
            if not selected:
                raise InstallError("Source build omitted requested binary packages")
            cursor = 0
            while cursor < len(selected):
                fields = outputs[selected[cursor]][1]
                cursor += 1
                for dependency in (fields.get("Depends", "") + "," + fields.get("Pre-Depends", "")).split(","):
                    for alternative in dependency.split("|"):
                        match = re.match(r"\s*([a-z0-9][a-z0-9+.-]*)", alternative)
                        if match and match.group(1) in outputs:
                            name = match.group(1)
                            if name not in selected:
                                selected.append(name)
                            break
            self.apt_install([outputs[name][0] for name in selected] + [name for name in package.apt if name not in outputs])

    def prepare_source_apt(self):
        """Enable the same distribution's sources for this task only."""
        self.prepare_apt()
        if not self.apt_directory:
            self.apt_directory = tempfile.TemporaryDirectory(prefix="xdebx-source-apt-")
            self.track_apt_directory()
            os.chmod(self.apt_directory.name, 0o755)
            folder = Path(self.apt_directory.name)
            (folder / "sources.list.d").mkdir(mode=0o755)
            originals = [Path("/etc/apt/sources.list")] + list(Path("/etc/apt/sources.list.d").glob("*"))
            for original in originals:
                if original.is_file() and original.suffix in (".list", ".sources"):
                    destination = folder / "sources.list" if original.name == "sources.list" else folder / "sources.list.d" / original.name
                    shutil.copy2(str(original), str(destination))
            self.apt_options = ["-o", "Dir::Etc::sourcelist=" + str(folder / "sources.list"),
                                "-o", "Dir::Etc::sourceparts=" + str(folder / "sources.list.d")]
        folder = Path(self.apt_directory.name)
        changed = False
        for file in [folder / "sources.list"] + list((folder / "sources.list.d").glob("*")):
            if not file.is_file():
                continue
            original = file.read_text()
            if file.suffix == ".sources":
                def types(match):
                    values = match.group(1).split()
                    if "deb" in values and "deb-src" not in values:
                        values.append("deb-src")
                    return "Types: " + " ".join(values)
                content = re.sub(r"^Types:\s*([^\n]+)", types, original, flags=re.MULTILINE)
            elif file.suffix == ".list":
                lines = original.splitlines()
                existing = set(lines)
                for line in tuple(lines):
                    if re.match(r"^\s*deb\s", line):
                        source = re.sub(r"^(\s*)deb\s", r"\1deb-src ", line)
                        if source not in existing:
                            lines.append(source)
                            existing.add(source)
                content = "\n".join(lines) + "\n"
            else:
                continue
            if content != original:
                file.write_text(content)
                if str(file) in self.apt_originals:
                    self.apt_originals[str(file)] = content
                file.chmod(0o644)
                changed = True
        if changed:
            self.update_apt()

    def add_repository(self, url, suite, component, key_url):
        self.prepare_apt()
        self.apt_install(["ca-certificates", "gnupg"])
        if not self.apt_directory:
            self.apt_directory = tempfile.TemporaryDirectory(prefix="xdebx-repo-")
            self.track_apt_directory()
            os.chmod(self.apt_directory.name, 0o755)
            folder = Path(self.apt_directory.name)
            (folder / "sources.list.d").mkdir(mode=0o755)
            if Path("/etc/apt/sources.list").exists():
                shutil.copy2("/etc/apt/sources.list", str(folder / "sources.list"))
            for file in Path("/etc/apt/sources.list.d").glob("*"):
                if file.is_file() and file.suffix in (".list", ".sources"):
                    shutil.copy2(str(file), str(folder / "sources.list.d" / file.name))
            self.apt_options = ["-o", "Dir::Etc::sourcelist=" + str(folder / "sources.list"),
                                "-o", "Dir::Etc::sourceparts=" + str(folder / "sources.list.d")]
        folder = Path(self.apt_directory.name)
        # Each vendor keeps its own signing key throughout a multi-item task.
        import hashlib
        key = folder / ("vendor-" + hashlib.sha256(key_url.encode()).hexdigest()[:16] + ".asc")
        self.resources.fetch(key_url, key, {})
        key.chmod(0o644)
        repo = folder / "sources.list.d" / ("xdebx-" + re.sub(r"[^a-z0-9]", "-", url) + ".list")
        repo.write_text("deb [arch=amd64 signed-by={}] {} {} {}\n".format(key, url, suite, component))
        repo.chmod(0o644)
        self.apt_ready = False
        # Do not recreate a CN source directory when adding vendor repositories.
        self.update_apt()
        self.apt_ready = True

    def fonts(self):
        self.apt_install(["fontconfig"])
        for entry in self.resources.manifest["local_assets"]:
            path = PROJECT / entry["filename"]
            if not path.is_file():
                raise InstallError("Font asset missing; use the XdebX program release")
            self.resources.validate(path, entry)
            destination = self.ctx.home / ".local/share/fonts/MapleMono-NF" / path.name
            self.configurator.write(destination, path.read_bytes(), 0o644)
        self.runner.run(["fc-cache", "-f"])

    def online_install(self, package, entry):
        key = package.id
        path = self.resources.get(entry, official=True)
        if key == "miniconda3":
            target = self.ctx.home / "Software/miniconda3"
            if target.exists():
                raise InstallError("Existing Conda directory; no overwrite")
            # Conda prefixes are not relocatable. Create a marked, otherwise empty
            # prefix and let its updater initialize that exact final location.
            if self.runner.journal:
                self.runner.journal.begin_component(target)
                self.runner.run(["bash", path, "-b", "-u", "-p", target])
                self.runner.run([target / "bin/conda", "--version"], timeout=30)
                self.runner.journal.complete_component(target)
            else:
                self.runner.run(["bash", path, "-b", "-p", target])
        elif key in ("go", "jdk"):
            with self.runner.temporary(prefix="xdebx-sdk-") as tmp:
                safe_extract(path, tmp)
                source = next(p for p in Path(tmp).iterdir() if p.is_dir())
                target = self.ctx.home / ("Software/go" if key == "go" else "Software/jdk")
                if target.exists():
                    raise InstallError("Existing SDK directory; no overwrite")
                user_directory(self.ctx, target.parent)
                self.runner.install_tree(source, target)
                if key == "go":
                    user_directory(self.ctx, self.ctx.home / "Software/gopath")
                    runtime = 'export GOROOT="$HOME/Software/go"\nexport GOPATH="$HOME/Software/gopath"\nexport PATH="$GOROOT/bin:$GOPATH/bin:$PATH"\n'
                else:
                    runtime = 'export JAVA_HOME="$HOME/Software/jdk"\nexport PATH="$JAVA_HOME/bin:$PATH"\n'
                self.configurator.write(self.ctx.config_dir / (key + ".sh"), runtime)
        else:
            if key == "wechat":
                self.desktop_dependencies()
                self.apt_install(["libatomic1", "libegl1", "libopengl0", "libpulse0", "desktop-file-utils"])
            self.deb(dict(entry, filename=None), official=True)

    def appimage(self, entry, official=False):
        self.desktop_dependencies()
        self.apt_install(["libopengl0", "libegl1", "libxcb-cursor0", "libxkbcommon-x11-0"])
        path = self.resources.get(entry, official=official)
        target = self.ctx.home / ".local/share/xdebx/apps/snipaste"
        if target.exists():
            raise InstallError("Existing AppImage extraction; no overwrite")
        user_directory(self.ctx, target.parent)
        with self.runner.temporary(prefix="xdebx-appimage-") as tmp:
            executable = Path(tmp) / "app.AppImage"
            shutil.copy2(str(path), str(executable))
            executable.chmod(0o755)
            os.chmod(tmp, 0o755)
            self.ctx.fix_owner(Path(tmp))
            self.runner.run([executable, "--appimage-extract"], cwd=tmp)
            self.runner.install_tree(Path(tmp) / "squashfs-root", target)
        script = '#!/bin/sh\nexec {} "$@"\n'.format(shlex.quote(str(target / "AppRun")))
        self.configurator.write(self.ctx.home / ".local/bin/snipaste", script, 0o755)

    def desktop_dependencies(self):
        """Native runtime libraries missing from legacy deb/AppImage metadata."""
        suffix = "t64" if self.ctx.target in ("ubuntu24", "debian13") else ""
        self.apt_install(["libglib2.0-0" + suffix, "libgtk-3-0" + suffix, "libnss3", "libnspr4",
                          "libatk1.0-0" + suffix, "libatk-bridge2.0-0" + suffix, "libcups2" + suffix,
                          "libdrm2", "libdbus-1-3", "libx11-xcb1", "libxcb1", "libxcomposite1",
                          "libxdamage1", "libxext6", "libxfixes3", "libxrandr2", "libgbm1",
                          "libasound2" + suffix, "libpango-1.0-0", "libcairo2", "libxss1", "libxtst6", "libgl1"])

    def strategy(self, package, method, entry=None, allow_unverified=False):
        key = package.id
        if not entry and not getattr(self.resources, "ignore_compatibility", False):
            measured = next((row for row in self.resources.strategy_support
                             if row["target"] == self.ctx.target and row["software"] == key and row["method"] == method), {})
            if measured and measured.get("status") not in ("not-run", "blocked") and not measured.get("default_enabled", False):
                raise Unsupported("Strategy did not pass acceptance: " + key + "/" + method)
        if key == "maple-mono-nf":
            return self.fonts()
        if key == "uv" and method != "source":
            # Minimal Debian/Ubuntu Python installations omit venv support and,
            # on older releases, distutils needed by interpreter inspection.
            self.apt_install(["python3-venv"])
        if key == "nodejs22-nvm":
            if method == "source":
                raise InstallError("No pinned Node source-build recipe; binary strategies exhausted")
            self.node(22, official=method == "official", entry_override=entry)
            return
        if key in ("claude-code", "opencode", "openclaw"):
            if method == "source":
                raise InstallError("No verified source build for this Agent distribution")
            return self.npm(package, official=method == "official", entry_override=entry)
        if method == "source":
            return self.source(package, entry)
        if key == "ctypora":
            if not allow_unverified:
                raise InstallError("CTypora provenance unknown; requires --allow-unverified")
            candidates = [e for e in self.resources.cached if e["id"] == "CTypora"]
            self.desktop_dependencies()
            return self.deb(entry or candidates[0])
        if key == "docker":
            if method == "official" and self.ctx.target != "ubuntu20":
                distro = self.ctx.os["ID"]
                url = "https://download.docker.com/linux/" + distro
                if self.ctx.settings["mirror"] == "CN" and not self.apt_using_raw:
                    url = "https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/" + distro
                key_url = url + "/gpg"
                self.add_repository(url, self.ctx.os["VERSION_CODENAME"], "stable", key_url)
                return self.apt_install(["docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"])
            if method == "official":
                return self.apt_install(["docker.io"])
            raise InstallError("Docker bootstrap is not an offline package")
        if key == "mysql":
            if self.ctx.os["ID"] == "ubuntu":
                return self.apt_install(["mysql-server"])
            self.add_repository("https://repo.mysql.com/apt/debian", self.ctx.os["VERSION_CODENAME"],
                                "mysql-8.4-lts", "https://repo.mysql.com/RPM-GPG-KEY-mysql-2025")
            return self.apt_install(["mysql-community-server"])
        if key == "clashterminalx":
            target = self.ctx.home / ".local/share/xdebx/apps/ClashTerminalX"
            if target.exists():
                raise InstallError("Existing ClashTerminalX directory")
            source = self.resources.entries("clashterminalx", online=True)[0]
            url = source["git_archive_url"]
            path = self.resources.fetch(url, self.ctx.cache_dir / "clashtx.tar.gz", {})
            with self.runner.temporary(prefix="xdebx-clashtx-") as tmp:
                safe_extract(path, tmp)
                user_directory(self.ctx, target.parent)
                self.runner.install_tree(next(Path(tmp).iterdir()), target)
            self.ctx.fix_owner(target)
            self.runner.run(["uv", "python", "install", "3.11"])
            _, python = self.runner.run(["uv", "python", "find", "3.11"])
            index = "https://pypi.tuna.tsinghua.edu.cn/simple" if self.ctx.settings["mirror"] == "CN" else "https://pypi.org/simple"
            env = {"PATH": self.ctx.env["PATH"], "PYTHON": python.strip(), "PIP_INDEX_URL": index}
            try:
                self.runner.run(["bash", "scripts/install.sh"], cwd=str(target), env=env, timeout=300)
            except Cancelled:
                raise
            except InstallError:
                if self.ctx.settings["mirror"] != "CN":
                    raise
                self.runner.emit(tr(self.ctx, "mirror_fallback", kind="PyPI"))
                env["PIP_INDEX_URL"] = "https://pypi.org/simple"
                self.runner.run(["bash", "scripts/install.sh"], cwd=str(target), env=env, timeout=300)
            self.configurator.write(self.ctx.home / ".local/bin/clashtx",
                                    '#!/bin/sh\nexec {} "$@"\n'.format(shlex.quote(str(target / "clashtx.sh"))), 0o755)
            return
        if method == "official":
            if package.apt:
                try:
                    self.apt_install(package.apt)
                    return
                except InstallError as exc:
                    # Ubuntu 20 lacks several modern CLI packages. Its official
                    # upstream portable release belongs to the first strategy.
                    upstream = [e for e in self.resources.entries(package.resource)
                                if e.get("official") and e.get("kind") in ("archive", "deb")
                                and e.get("filename", "").startswith(("portable/", "upstream/"))]
                    if upstream:
                        failures = [str(exc)]
                        for candidate in upstream:
                            try:
                                if candidate["kind"] == "archive":
                                    return self.install_archive(candidate, package.command, official=True)
                                return self.deb(candidate, official=True)
                            except InstallError as failure:
                                failures.append(str(failure))
                        raise InstallError("; ".join(failures))
                    # JDK 21 may be absent in older distro repositories.
                    if not self.resources.entries(package.resource, online=True):
                        raise
            online = [entry] if entry else self.resources.entries(package.resource, online=True)
            if online:
                failures = []
                for candidate in online:
                    try:
                        return self.online_install(package, candidate)
                    except InstallError as exc:
                        failures.append(str(exc))
                raise InstallError("; ".join(failures))
            cached = [entry] if entry else self.resources.entries(package.resource)
            for candidate in cached:
                if candidate["kind"] == "deb":
                    return self.deb(candidate, official=True)
                if candidate["kind"] == "appimage":
                    return self.appimage(candidate, official=True)
                if candidate["kind"] == "script" and key == "uv":
                    self.apt_install(["curl"])
                    path = self.resources.get(candidate, official=True)
                    return self.runner.run(["sh", path], env={"UV_INSTALL_DIR": str(self.ctx.home / ".local/bin"), "UV_NO_MODIFY_PATH": "1"})
            raise InstallError("No official strategy")
        candidates = [entry] if entry else self.resources.entries(package.resource)
        failures = []
        for candidate in candidates:
            try:
                if candidate["kind"] == "deb":
                    self.deb(candidate)
                    if key == "tmux-tmuxp":
                        wheels = self.resources.entries("tmuxp", "deb")
                        self.deb(wheels[0]) if wheels else self.apt_install(["tmuxp"])
                    return
                if candidate["kind"] == "archive":
                    return self.install_archive(candidate, package.command)
                if candidate["kind"] == "appimage":
                    return self.appimage(candidate)
            except InstallError as exc:
                failures.append(str(exc))
        raise InstallError("; ".join(failures) or "No compatible cached strategy")

    def execute(self, ids, method="auto", configure=True, allow_unverified=False, force=False, entry=None):
        self.ctx.ensure_supported()
        ordered = resolve(ids)
        self.configurator.github_unavailable = False
        self.configurator.git_source_failures.clear()
        user_directory(self.ctx, self.ctx.state_dir)
        lock = (self.ctx.state_dir / "install.lock").open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise InstallError("Another XdebX installation is running")
        recover(self.ctx, self.runner.emit)
        results = []
        run_id = str(time.time_ns())
        log_path = self.ctx.state_dir / "logs" / (run_id + ".log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        sink = self.runner.emit
        log = log_path.open("a", encoding="utf-8")
        log_path.chmod(0o600)
        def emit(text):
            sink(text)
            log.write(text + "\n")
            log.flush()
        self.runner.emit = emit
        journal = Journal(self.ctx, self.runner, ordered, run_id, log_path)
        self.runner.journal = journal
        try:
            if self.progress:
                self.progress(0, len(ordered))
            for key in ordered:
                package = CATALOG[key]
                journal.phase(key, "detecting")
                result = {"id": key, "status": "failed", "method": None, "attempts": []}
                try:
                    if self.runner.cancel.is_set():
                        result.update(status="cancelled", reason="Cancelled before starting")
                    elif any(r["id"] in package.dependencies and r["status"] not in ("success", "installed") for r in results):
                        result.update(status="skipped", reason="Dependency failed")
                    else:
                        detected = self.detected(key)
                        if detected["status"] == "unsupported":
                            result.update(status="unsupported", reason=detected["detail"])
                        elif detected["status"] == "installed" and not force:
                            result.update(status="installed", reason=detected["detail"])
                        elif any(self.detected(c)["status"] == "installed" for c in package.conflicts):
                            result.update(status="skipped", reason="Conflicting software already installed")
                        else:
                            methods = ["official", "cache", "source"] if method == "auto" else [method]
                            retry = detected["status"] == "partial" or any(
                                r.get("id") == key and r.get("status") in ("failed", "cancelled", "interrupted", "partial")
                                for path in (self.ctx.state_dir / "runs").glob("*.json")
                                for r in read_json(path).get("results", []))
                            self.configurator.preserve_existing = retry
                            # A working binary only needs missing configuration supplemented.
                            binary_state = self.detected(key, configuration=False) if detected["status"] == "partial" else detected
                            if detected["status"] == "partial" and binary_state["status"] == "installed":
                                result.update(status="success", method="repair", version=binary_state["detail"])
                                methods = []
                            for strategy in methods:
                                journal.phase(key, strategy)
                                try:
                                    self.runner.emit(tr(self.ctx, "attempt", software=key, method=strategy))
                                    self.strategy(package, strategy, entry if key in ids else None, allow_unverified)
                                    verification = self.detected(key, configuration=False)
                                    if verification["status"] not in ("installed", "partial"):
                                        raise InstallError("Post-install verification: " + verification["detail"])
                                    if verification["status"] == "partial":
                                        raise InstallError("Missing components: " + verification["detail"])
                                    result.update(status="success", method=strategy, version=verification["detail"])
                                    break
                                except Cancelled as exc:
                                    result.update(status="cancelled", reason=str(exc))
                                    break
                                except (InstallError, OSError, ValueError) as exc:
                                    result["attempts"].append({"method": strategy, "error": str(exc)})
                                    result["reason"] = str(exc)
                                    self.runner.emit(tr(self.ctx, "attempt_failed", method=strategy, error=str(exc)))
                            if result["status"] == "success" and configure:
                                journal.phase(key, "configuration")
                                try:
                                    if key == "nodejs22-nvm" and result.get("method") == "repair":
                                        self.repair_node_alias()
                                    self.configurator.apply(key)
                                except Cancelled as exc:
                                    result.update(status="cancelled", reason=str(exc))
                                except (InstallError, OSError, ValueError) as exc:
                                    result.update(status="partial", reason="Configuration: " + str(exc))
                            if result["status"] == "success":
                                verified = self.detected(key)
                                if verified["status"] != "installed":
                                    result.update(status="partial", reason=verified["detail"])
                except Cancelled as exc:
                    result.update(status="cancelled", reason=str(exc))
                except Exception as exc:
                    result.update(status="partial" if journal.record.get("phase") == "configuration" else "failed",
                                  reason=str(exc))
                    self.runner.emit(tr(self.ctx, "error") + ": " + str(exc))
                results.append(result)
                self.runner.emit(tr(self.ctx, "result", software=key, status=tr(self.ctx, result["status"])))
                journal.record.update(results=results, current=None, phase="between-items")
                journal.save()
                self.configurator.preserve_existing = False
                if self.progress:
                    self.progress(len(results), len(ordered))
            self.close()
            self.apt_directory, self.apt_ready, self.apt_options = None, False, []
            self.apt_using_raw, self.apt_originals = False, {}
            journal.finish(results)
            return results
        finally:
            self.configurator.preserve_existing = False
            self.runner.journal = None
            self.runner.emit = sink
            log.close()
            lock.close()
            self.ctx.fix_owner(self.ctx.state_dir)

    def plan(self, ids):
        return [{"id": key, "dependencies": CATALOG[key].dependencies, "detection": self.detected(key),
                 "methods": ["official", "cache", "source"],
                 "cache_candidates": [e["filename"] for e in self.resources.entries(CATALOG[key].resource)],
                 "configuration": key in ("vim9", "zsh-omz", "tmux-tmuxp", "claude-code", "nodejs22-nvm")}
                for key in resolve(ids)]
