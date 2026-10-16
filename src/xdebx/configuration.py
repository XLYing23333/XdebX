"""Template application with reversible, conflict-aware file writes."""
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from .resources import safe_extract
from .runtime import PROJECT, InstallError, Cancelled, atomic_json, read_json, sha256, user_directory
from .i18n import tr
from .recovery import no_symlinks


VIM_PLUGINS = ["onedark.vim", "nerdtree", "vim-polyglot", "auto-pairs", "vim-commentary",
               "indentLine", "vim-fugitive", "vim-airline", "vim-airline-themes"]

SNAPSHOT_FILES = {
    "onedark.vim": "colors/onedark.vim", "nerdtree": "plugin/NERD_tree.vim",
    "vim-polyglot": "plugin/polyglot.vim", "auto-pairs": "plugin/auto-pairs.vim",
    "vim-commentary": "plugin/commentary.vim", "indentLine": "after/plugin/indentLine.vim",
    "vim-fugitive": "plugin/fugitive.vim", "vim-airline": "plugin/airline.vim",
    "vim-airline-themes": "plugin/airline-themes.vim", "tpm": "tpm",
    "tmux-sensible": "sensible.tmux", "catppuccin-tmux": "catppuccin.tmux",
    "ohmyzsh": "oh-my-zsh.sh", "zsh-autosuggestions": "zsh-autosuggestions.zsh",
    "zsh-syntax-highlighting": "zsh-syntax-highlighting.zsh",
}


def merge_defaults(current, defaults):
    result = dict(defaults)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(defaults.get(key), dict):
            result[key] = merge_defaults(value, defaults[key])
        else:
            result[key] = value
    return result


class Configurator:
    def __init__(self, ctx, runner, resources):
        self.ctx, self.runner, self.resources = ctx, runner, resources
        self.preserve_existing = False
        self.github_unavailable = False
        self.git_source_failures = set()

    def write(self, destination, text, mode=0o600):
        destination = Path(destination)
        self.validate_destination(destination)
        no_symlinks(destination)
        if self.preserve_existing and mode != 0o755 and destination.exists():
            self.runner.emit(tr(self.ctx, "preserved_config", path=str(destination)))
            return
        if destination.is_symlink():
            raise InstallError("Refusing to replace symlink: " + str(destination))
        data = text.encode("utf-8") if isinstance(text, str) else text
        if destination.exists() and destination.read_bytes() == data:
            return
        record = {"destination": str(destination), "before": None}
        stamp = str(time.time_ns())
        folder = self.ctx.backups / stamp
        user_directory(self.ctx, folder)
        folder.chmod(0o700)
        if destination.exists():
            record["original_mode"] = destination.stat().st_mode & 0o777
            shutil.copy2(str(destination), str(folder / "original"))
            (folder / "original").chmod(0o600)
            record["before"] = str(folder / "original")
        parents = []
        parent = destination.parent
        while not parent.exists():
            parents.append(parent)
            parent = parent.parent
        destination.parent.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            for parent in parents:
                os.chown(str(parent), self.ctx.uid, self.ctx.gid)
        fd, name = tempfile.mkstemp(prefix=".xdebx-", dir=str(destination.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.chmod(name, mode)
            os.replace(name, str(destination))
        finally:
            if os.path.exists(name):
                os.unlink(name)
        record["after_sha256"] = sha256(destination)
        atomic_json(folder / "record.json", record)
        self.ctx.fix_owner(destination)
        self.ctx.fix_owner(folder)
        self.runner.emit(tr(self.ctx, "configured", path=str(destination)))

    def restore(self, record_path, force=False):
        record_path = Path(record_path).resolve()
        if os.path.commonpath([str(self.ctx.backups.resolve()), str(record_path)]) != str(self.ctx.backups.resolve()):
            raise InstallError("Restore record outside backup directory")
        record = read_json(record_path)
        dest = Path(record["destination"])
        self.validate_destination(dest)
        if dest.is_symlink():
            raise InstallError("Restore destination is now a symlink")
        if dest.exists() and sha256(dest) != record["after_sha256"]:
            if not force:
                raise InstallError("Restore conflict: file changed since installation; use --force after review")
            shutil.copy2(str(dest), str(record_path.parent / "user-modified"))
            (record_path.parent / "user-modified").chmod(0o600)
        if record["before"]:
            original = Path(record["before"]).resolve()
            if original.parent != record_path.parent:
                raise InstallError("Invalid backup source")
            shutil.copy2(str(original), str(dest))
            dest.chmod(record.get("original_mode", 0o600))
            self.ctx.fix_owner(dest)
        elif dest.exists():
            dest.unlink()
        self.ctx.fix_owner(record_path.parent)

    def validate_destination(self, destination):
        """Backups and user-controlled symlinks cannot redirect elevated writes."""
        destination = Path(destination).resolve()
        roots = (self.ctx.home.resolve(), self.ctx.config_dir.absolute())
        if not any(os.path.commonpath([str(root), str(destination)]) == str(root) for root in roots):
            raise InstallError("Configuration destination outside target-user directories")

    def snapshot_ready(self, key, destination):
        entry = SNAPSHOT_FILES.get(key)
        path = Path(destination) / entry if entry else Path(destination)
        return path.is_file() and path.stat().st_size > 0 if entry else path.is_dir()

    def git_candidates(self, key, entry):
        record = read_json(PROJECT / "src/git-mirrors.json").get("resources", {}).get(key, {})
        mirrors = []
        if (record.get("version") == entry.get("version") and record.get("upstream") == entry.get("git_url")
                and record.get("member") and record.get("sha256")):
            mirrors = [url for url in record.get("urls", []) if url.startswith("https://gitee.com/")]
        upstream = [entry["git_url"]] if entry.get("git_url") and not self.github_unavailable else []
        ordered = mirrors + upstream if self.ctx.settings.get("mirror") == "CN" else upstream + mirrors
        return [url for url in dict.fromkeys(ordered) if url not in self.git_source_failures]

    def clone_pinned(self, key, entry, destination):
        if getattr(self.resources, "prefer_snapshots", False):
            return False
        for url in self.git_candidates(key, entry):
            git = ["git", "-c", "http.version=HTTP/1.1", "-c", "core.hooksPath=/dev/null",
                   "-c", "credential.helper=", "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never",
                   "-c", "http.lowSpeedLimit=1024", "-c", "http.lowSpeedTime=10"]
            self.runner.emit(tr(self.ctx, "git_source_attempt", url=url, version=entry["version"]))
            try:
                self.runner.run(git + ["clone", "--depth", "1", "--no-checkout", "--filter=blob:none", url, destination], timeout=30)
                _, head = self.runner.run(git + ["-C", destination, "rev-parse", "HEAD"], timeout=10)
                if head.strip() != entry["version"]:
                    self.runner.run(git + ["-C", destination, "fetch", "--depth", "1", "--filter=blob:none", "origin", entry["version"]], timeout=30)
                self.runner.run(git + ["-C", destination, "checkout", "--detach", entry["version"]], timeout=30)
                _, actual = self.runner.run(git + ["-C", destination, "rev-parse", "HEAD"], timeout=10)
                if actual.strip() != entry["version"] or not self.snapshot_ready(key, destination):
                    raise InstallError("Pinned checkout incomplete or mismatched: " + key)
                record = read_json(PROJECT / "src/git-mirrors.json").get("resources", {}).get(key, {})
                if url.startswith("https://gitee.com/"):
                    member = Path(destination) / record["member"]
                    if sha256(member) != record["sha256"]:
                        raise InstallError("Mirror entry point differs from pinned snapshot")
                atomic_json(Path(destination) / ".xdebx-snapshot.json", {"commit": entry["version"], "git_checkout": True,
                                                                          "source": url, "upstream": entry["git_url"]})
                return True
            except Cancelled:
                raise
            except (InstallError, OSError, ValueError) as exc:
                self.git_source_failures.add(url)
                if url.startswith("https://github.com/"):
                    self.github_unavailable = True
                self.runner.emit(tr(self.ctx, "git_source_failed", url=url, error=str(exc)))
                if Path(destination).exists():
                    shutil.rmtree(str(destination))
        self.runner.emit(tr(self.ctx, "git_snapshot_fallback"))
        return False

    def snapshot(self, key, destination):
        destination = Path(destination)
        if self.snapshot_ready(key, destination):
            return
        no_symlinks(destination)
        if destination.exists():
            self.validate_destination(destination)
            if destination.is_symlink():
                raise InstallError("Incomplete plugin directory is a symlink")
            backup = self.ctx.backups / str(time.time_ns())
            user_directory(self.ctx, backup)
            backup.chmod(0o700)
            shutil.move(str(destination), str(backup / "incomplete-plugin"))
            atomic_json(backup / "plugin-backup.json", {"destination": str(destination), "plugin": key})
        entries = self.resources.entries(key, "git-snapshot")
        if not entries:
            entries = self.resources.entries(key, "source")
        if not entries:
            raise InstallError("Missing snapshot: " + key)
        entry = entries[0]
        # Fixed commit from verified Git sources; pinned archives remain the fallback.
        user_directory(self.ctx, destination.parent)
        with self.runner.temporary(prefix="xdebx-plugin-") as tmp:
            staged = Path(tmp) / "checkout"
            self.clone_pinned(key, entry, staged)
            if not staged.exists():
                path = self.resources.get(entry)
                extracted = Path(tmp) / "archive"
                safe_extract(path, extracted)
                roots = list(extracted.iterdir())
                if len(roots) != 1 or not roots[0].is_dir():
                    raise InstallError("Unexpected snapshot layout")
                staged = roots[0]
                atomic_json(staged / ".xdebx-snapshot.json", {"commit": entry["version"], "git_checkout": False})
            if not self.snapshot_ready(key, staged):
                raise InstallError("Plugin archive missing entry point: " + key)
            self.runner.install_tree(staged, destination)

    def apply(self, key, switch_shell=True):
        home = self.ctx.home
        if key == "vim9":
            entry = self.resources.entries("vim-plug-autoload")[0]
            self.write(home / ".vim/autoload/plug.vim", self.resources.get(entry).read_bytes())
            for plugin in VIM_PLUGINS:
                self.snapshot(plugin, home / ".vim/plugged" / plugin)
            self.write(home / ".vimrc", (PROJECT / "data/confs/.vimrc").read_text())
            self.runner.run([home / ".local/bin/vim" if (home / ".local/bin/vim").exists() else "vim",
                             "-Nu", home / ".vimrc", "-i", "NONE", "-n", "-es", "+qa"], timeout=30)
        elif key == "tmux-tmuxp":
            for plugin in ("tpm", "tmux-sensible", "catppuccin-tmux"):
                self.snapshot(plugin, home / ".tmux/plugins" / plugin)
            self.write(home / ".tmux.conf", (PROJECT / "data/confs/tmux.conf").read_text())
            # Validate using an isolated server, without modifying user's tmux sessions.
            socket = "xdebx-check-" + str(os.getpid())
            try:
                self.runner.run(["tmux", "-L", socket, "-f", home / ".tmux.conf", "new-session", "-d", "-s", "check"])
            finally:
                self.runner.run(["tmux", "-L", socket, "kill-server"], check=False)
        elif key == "zsh-omz":
            self.snapshot("ohmyzsh", home / ".oh-my-zsh")
            for plugin in ("zsh-autosuggestions", "zsh-syntax-highlighting"):
                self.snapshot(plugin, home / ".oh-my-zsh/custom/plugins" / plugin)
            text = (PROJECT / "data/confs/.zshrc").read_text()
            text = text.replace('SH_THEME=', 'ZSH_THEME=').replace('<PATH>/miniconda3', str(home / "Software/miniconda3"))
            text = text.replace('source $ZSH/oh-my-zsh.sh', '[ -f "$ZSH/oh-my-zsh.sh" ] && source "$ZSH/oh-my-zsh.sh"')
            text = text.replace('\nff\n', '\ncommand -v fastfetch >/dev/null 2>&1 && fastfetch\n')
            lines = []
            for line in text.splitlines():
                if line.startswith("alias ") and any('="'+cmd in line for cmd in ("lsd", "tmuxp", "batcat")):
                    cmd = "lsd" if '="lsd' in line else "tmuxp" if '="tmuxp' in line else "batcat"
                    line = "command -v {} >/dev/null 2>&1 && {}".format(cmd, line)
                lines.append(line)
            text = "\n".join(lines) + "\n"
            # Do not shadow a user's preexisting Conda install with a missing one.
            text = text.replace("conda() {", 'if [ -x "$_CONDA_ROOT/bin/conda" ]; then\nconda() {', 1)
            text = text.replace("# ============= USER ALIAS", "fi\n\n# ============= USER ALIAS", 1)
            import shlex
            config_dir = shlex.quote(str(self.ctx.config_dir))
            text += '\n# XdebX runtime environment\nfor _xdebx_runtime in ' + config_dir + '/*.sh(N); do\n  [ -f "$_xdebx_runtime" ] && source "$_xdebx_runtime"\ndone\nunset _xdebx_runtime\n'
            self.write(home / ".zshrc", text)
            self.runner.run(["zsh", "-n", home / ".zshrc"])
            if switch_shell:
                code, shell = self.runner.probe(["sh", "-c", "command -v zsh"])
                if code == 0:
                    self.runner.run(["chsh", "-s", shell.strip(), self.ctx.user], root=True)
        elif key == "claude-code":
            defaults = read_json(PROJECT / "data/confs/settings.json.deepseek")
            defaults["language"] = "Chinese" if self.ctx.settings["language"] == "CN" else "English"
            dest = home / ".claude/settings.json"
            current = read_json(dest)
            self.write(dest, json.dumps(merge_defaults(current, defaults), ensure_ascii=False, indent=2) + "\n")
        elif key == "nodejs22-nvm":
            self.write(self.ctx.config_dir / "runtime.sh",
                       'export NVM_DIR="$HOME/.nvm"\n[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"\n')

    def missing(self, key):
        home = self.ctx.home
        if key == "vim9":
            return not (home / ".vimrc").is_file() or not (home / ".vim/autoload/plug.vim").is_file() or any(not self.snapshot_ready(p, home / ".vim/plugged" / p) for p in VIM_PLUGINS)
        if key == "tmux-tmuxp":
            return not (home / ".tmux.conf").is_file() or any(not self.snapshot_ready(p, home / ".tmux/plugins" / p) for p in ("tpm", "tmux-sensible", "catppuccin-tmux"))
        if key == "zsh-omz":
            return not (home / ".zshrc").is_file() or not self.snapshot_ready("ohmyzsh", home / ".oh-my-zsh") or any(not self.snapshot_ready(p, home / ".oh-my-zsh/custom/plugins" / p) for p in ("zsh-autosuggestions", "zsh-syntax-highlighting"))
        if key == "claude-code":
            try:
                settings = json.loads((home / ".claude/settings.json").read_text())
                env = settings.get("env", {})
                return not isinstance(env, dict) or not env.get("ANTHROPIC_BASE_URL") or not env.get("ANTHROPIC_MODEL")
            except (OSError, ValueError, AttributeError):
                return True
        return False
