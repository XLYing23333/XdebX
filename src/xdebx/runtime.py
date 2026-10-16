"""User resolution, persistent state and cancellable subprocess execution."""
import contextlib
import hashlib
import json
import os
import platform
import pwd
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
TARGETS = {("ubuntu", "20.04"): "ubuntu20", ("ubuntu", "22.04"): "ubuntu22",
           ("ubuntu", "24.04"): "ubuntu24", ("debian", "12"): "debian12",
           ("debian", "13"): "debian13"}


class InstallError(Exception):
    pass


class Unsupported(InstallError):
    """A declared strategy has no applicable candidate on this target."""
    pass


class Cancelled(InstallError):
    pass


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {} if default is None else default


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".xdebx-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(name, mode)
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def user_directory(ctx, path):
    """Create new ancestors with target-user ownership, leaving existing ones alone."""
    path = Path(path)
    home = ctx.home.absolute()
    if os.path.commonpath([str(home), str(path.absolute())]) == str(home):
        resolved_home = home.resolve()
        if os.path.commonpath([str(resolved_home), str(path.resolve())]) != str(resolved_home):
            raise InstallError("Target-user directory resolves outside the user's home")
    missing, ancestor = [], path
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    path.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        for directory in missing:
            os.chown(str(directory), ctx.uid, ctx.gid)
    return path


def redact(text):
    text = re.sub(r"(?i)((?:token|api[_-]?key|password|authorization)[\"']?\s*[=:]\s*[\"']?)[^\s,;\"']+",
                  r"\1[REDACTED]", str(text))
    return re.sub(r"https?://[^\s/@]+:[^\s/@]+@", "https://[REDACTED]@", text)


class Context:
    def __init__(self, user=None, desktop="auto", mirror=None, language=None):
        if os.geteuid() == 0:
            user = user or os.environ.get("SUDO_USER")
            if not user:
                raise InstallError("Direct root execution requires --user USER")
        try:
            account = pwd.getpwnam(user) if user else pwd.getpwuid(os.getuid())
        except KeyError:
            raise InstallError("Unknown target user: " + str(user))
        if os.geteuid() != 0 and account.pw_uid != os.getuid():
            raise InstallError("Selecting another target user requires root invocation")
        self.user, self.uid, self.gid = account.pw_name, account.pw_uid, account.pw_gid
        self.home = Path(account.pw_dir)
        def xdg(name, fallback):
            value = os.environ.get(name) if os.geteuid() != 0 or account.pw_uid == 0 else None
            return Path(value) if value and Path(value).is_absolute() else self.home / fallback
        self.config_dir = xdg("XDG_CONFIG_HOME", ".config") / "xdebx"
        self.state_dir = xdg("XDG_STATE_HOME", ".local/state") / "xdebx"
        self.cache_dir = xdg("XDG_CACHE_HOME", ".cache") / "xdebx"
        self.backups = xdg("XDG_DATA_HOME", ".local/share") / "xdebx/backups"
        self.settings = {"language": "EN", "mirror": "Raw", "music": True}
        self.settings.update(read_json(self.config_dir / "settings.json"))
        if mirror:
            self.settings["mirror"] = mirror
        if language:
            self.settings["language"] = language
        self.env = dict(os.environ, HOME=str(self.home), USER=self.user, LOGNAME=self.user,
                        DEBIAN_FRONTEND="noninteractive", TERM=os.environ.get("TERM", "xterm-256color"))
        self.env["PATH"] = str(self.home / ".local/bin") + ":" + self.env.get("PATH", "")
        os_release = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                os_release[key] = value.strip('"')
        self.os = os_release
        self.target = TARGETS.get((os_release.get("ID"), os_release.get("VERSION_ID")))
        self.arch = platform.machine()
        session = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        installed_desktop = any(Path(p).exists() for p in ("/usr/share/xsessions", "/usr/share/wayland-sessions"))
        self.desktop = (session or installed_desktop) if desktop == "auto" else desktop == "yes"
        self.audio = session

    def ensure_supported(self):
        if self.arch not in ("x86_64", "amd64") or not self.target:
            raise InstallError("Supported: AMD64 Ubuntu 20.04/22.04/24.04, Debian 12/13")

    def save_settings(self):
        user_directory(self, self.config_dir)
        atomic_json(self.config_dir / "settings.json", self.settings)
        self.fix_owner(self.config_dir)

    def fix_owner(self, path):
        if os.geteuid() == 0:
            path = Path(path)
            os.chown(str(path), self.uid, self.gid, follow_symlinks=False)
            if path.is_dir():
                for child in path.rglob("*"):
                    if not child.is_symlink():
                        os.chown(str(child), self.uid, self.gid)


class Runner:
    def __init__(self, context, emit=None):
        self.ctx = context
        self.emit = emit or (lambda text: None)
        self.cancel = threading.Event()
        self.shutdown = threading.Event()
        self.records = []
        self.journal = None

    @contextlib.contextmanager
    def temporary(self, prefix="xdebx-"):
        manager = self.journal.temporary(prefix) if self.journal else tempfile.TemporaryDirectory(prefix=prefix)
        with manager as directory:
            self.ctx.fix_owner(Path(directory))
            yield directory

    def install_tree(self, source, target):
        if self.journal:
            return self.journal.install_tree(source, target)
        shutil.copytree(str(source), str(target), symlinks=True)
        self.ctx.fix_owner(target)

    def run(self, args, root=False, timeout=1800, cwd=None, env=None, check=True, transaction=False, quiet=False, cancellable=True):
        if self.shutdown.is_set() or cancellable and self.cancel.is_set():
            raise Cancelled("Cancelled")
        args = [str(a) for a in args]
        command = list(args)
        effective_env = dict(self.ctx.env)
        effective_env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_SSH_COMMAND="ssh -oBatchMode=yes -oConnectTimeout=10")
        if env:
            effective_env.update(env)
        if root and os.geteuid() != 0:
            command = ["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive"] + command
        elif not root and os.geteuid() == 0 and self.ctx.uid != 0:
            command = ["runuser", "-u", self.ctx.user, "--", "env"] + [
                k + "=" + effective_env[k] for k in ("HOME", "PATH", "USER", "LOGNAME")
            ] + command
        emit = (lambda text: None) if quiet else self.emit
        def clean(value):
            value = redact(value)
            for key, secret in effective_env.items():
                if re.search(r"(?i)(token|password|api.?key)", key) and len(secret) >= 6:
                    value = value.replace(secret, "[REDACTED]")
            return value
        emit("$ " + clean(shlex.join(args)))
        record = {"command": clean(shlex.join(args)), "started_at": time.time(), "cwd": str(cwd) if cwd else None}
        self.records.append(record)
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    env=effective_env, cwd=cwd, start_new_session=True)
        except OSError as exc:
            record.update(exit_code=None, error=clean(str(exc)))
            raise
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        output, pending, start = [], b"", time.monotonic()
        killed = None
        deadline_reached = False
        last_wait_notice = 0
        try:
            while sel.get_map() or proc.poll() is None:
                if time.monotonic() - start > timeout:
                    if transaction:
                        deadline_reached = True
                    else:
                        killed = "Command timeout"
                if (self.shutdown.is_set() or cancellable and self.cancel.is_set()) and not transaction:
                    killed = "Cancelled"
                if transaction and (deadline_reached or self.cancel.is_set()) and time.monotonic() - last_wait_notice >= 10:
                    from .i18n import tr
                    emit(tr(self.ctx, "package_wait"))
                    last_wait_notice = time.monotonic()
                if killed:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        proc.wait(5)
                    break
                for key, _ in sel.select(0.2):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    pending += chunk.replace(b"\r", b"\n")
                    if len(pending) >= 65536 and b"\n" not in pending:
                        pending = pending[:65536] + b"\n" + pending[65536:]
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        line = clean(line.decode("utf-8", "replace"))[:8192]
                        output.append(line)
                        emit(line)
                if len(output) > 12000:
                    output = output[-6000:]
            if pending:
                line = clean(pending.decode("utf-8", "replace"))
                output.append(line)
                emit(line)
            proc.wait(timeout=5)
            if transaction and time.monotonic() - start > timeout:
                deadline_reached = True
        finally:
            sel.close()
            proc.stdout.close()
            record.update(exit_code=proc.poll(), elapsed_seconds=round(time.monotonic() - start, 3))
            if killed:
                record["error"] = killed
            emit("[exit {}]".format(proc.poll()))
        text = "\n".join(output)
        if killed:
            raise Cancelled(killed) if killed == "Cancelled" else InstallError(killed)
        if deadline_reached:
            raise InstallError("Package transaction exceeded deadline and has now finished")
        if check and proc.returncode:
            raise InstallError("exit {}: {}".format(proc.returncode, text[-1800:]))
        return proc.returncode, text

    def probe(self, args):
        try:
            return self.run(args, check=False, timeout=20, quiet=True, cancellable=False)
        except (OSError, InstallError):
            return 127, ""
