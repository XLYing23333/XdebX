#!/usr/bin/env python3
"""Executed only inside a labelled acceptance-test container."""
import json
import fcntl
import os
import pty
import shlex
import signal
import subprocess
import sys
import tempfile
import termios
import struct
import time
from pathlib import Path

sys.path.insert(0, "/project/src")
from xdebx.catalog import CATALOG
from xdebx.configuration import VIM_PLUGINS
from xdebx.installer import Installer
from xdebx.runtime import Context, InstallError, Unsupported, atomic_json, sha256, redact
from xdebx.resources import safe_extract


def record_process(engine, command):
    record = {"command": redact(shlex.join([str(c) for c in command])), "started_at": time.time()}
    engine.runner.records.append(record)
    print("$ " + record["command"], flush=True)
    return record


def tui(engine):
    transcripts = []
    for options in ([], ["--lite"]):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 100, 0, 0))
        command = ["runuser", "-u", engine.ctx.user, "--", "env", "TERM=xterm-256color", "PYTHONPATH=/project/src",
                   "python3", "-m", "xdebx", "--desktop", "no"] + options
        record = record_process(engine, command)
        process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        os.set_blocking(master, False)
        output = b""
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    output += os.read(master, 65536)
                except (BlockingIOError, OSError):
                    pass
                if b"Fast Install" in output and b"Settings" in output:
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            assert b"Fast Install" in output and b"Settings" in output, output.decode(errors="replace")[-2000:]
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 16, 55, 0, 0))
            os.killpg(process.pid, signal.SIGWINCH)
            time.sleep(0.4)
            os.write(master, b"\tq")
            process.wait(5)
            assert process.returncode == 0
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(5)
            os.close(master)
            record["exit_code"] = process.returncode
            transcripts.append(output.decode(errors="replace"))
    destination = Path(sys.argv[1]).parent / "tui-transcript.txt"
    destination.write_text("\n\n".join(transcripts))
    return {"status": "passed", "validation": "Normal/lite curses menus, resize, log focus and clean quit"}


def interactive(engine, command, seconds=3):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 100, 0, 0))
    env = dict(engine.ctx.env, TERM="xterm-256color")
    invocation = [str(c) for c in command]
    if os.geteuid() == 0 and engine.ctx.uid != 0:
        invocation = ["runuser", "-u", engine.ctx.user, "--", "env", "HOME=" + str(engine.ctx.home),
                      "TERM=xterm-256color"] + invocation
    record = record_process(engine, invocation)
    process = subprocess.Popen(invocation,
                               stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
    os.close(slave)
    os.set_blocking(master, False)
    output = b""
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                output += os.read(master, 65536)
            except (BlockingIOError, OSError):
                pass
            if process.poll() is not None:
                break
            time.sleep(0.05)
        if process.poll() not in (None, 0):
            raise InstallError("Interactive process exited: " + output.decode(errors="replace")[-2000:])
        if not output:
            raise InstallError("Interactive process produced no terminal output")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        os.close(master)
        record.update(exit_code=process.poll(), terminated_after_validation=True)
        print("[exit {} after PTY validation]".format(process.poll()), flush=True)


def graphical(engine, binary, args, folder):
    display = ":99"
    server_command = ["Xvfb", display, "-screen", "0", "1280x800x24", "-ac"]
    server_record = record_process(engine, server_command)
    with (folder / "xvfb.log").open("w") as stream:
        server = subprocess.Popen(server_command, stdout=stream, stderr=subprocess.STDOUT)
    app = None
    app_record = None
    try:
        time.sleep(0.6)
        with (folder / "desktop.log").open("w") as stream:
            launch = [binary] + args
            if Path(binary).name == "wechat":
                launch = ["strace", "-f", "-e", "trace=file,process,signal", "-o", str(folder / "desktop-strace.log")] + launch
            command = ["runuser", "-u", engine.ctx.user, "--", "env", "DISPLAY=" + display,
                       "HOME=" + str(engine.ctx.home), "PATH=" + engine.ctx.env["PATH"],
                       "dbus-run-session"] + launch
            app_record = record_process(engine, command)
            app = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 15
        window = None
        while time.monotonic() < deadline:
            probe_command = ["xwininfo", "-display", display, "-root", "-tree"]
            probe_record = record_process(engine, probe_command)
            probe = subprocess.run(probe_command, capture_output=True, text=True)
            probe_record["exit_code"] = probe.returncode
            import re
            if re.search(r"[1-9][0-9]* child(?:ren)?", probe.stdout):
                window = probe.stdout
                break
            if app.poll() not in (None, 0):
                break
            time.sleep(0.5)
        errors = (folder / "desktop.log").read_text(errors="replace")
        if not window:
            engine.runner.run(["ldd", str(Path(binary).resolve())], check=False)
            raise InstallError("No application window in Xvfb: " + errors[-2500:])
        (folder / "windows.txt").write_text(window)
        time.sleep(1)
        if app.poll() not in (None, 0):
            raise InstallError("Application exited after creating a window: " + errors[-2500:])
    finally:
        if app:
            try:
                os.killpg(app.pid, signal.SIGTERM)
                app.wait(5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(app.pid, signal.SIGKILL)
                    app.wait(5)
                except ProcessLookupError:
                    pass
            if app_record:
                app_record.update(exit_code=app.poll(), terminated_after_validation=True)
        server.terminate()
        server.wait(5)
        server_record.update(exit_code=server.poll(), terminated_after_validation=True)


def smoke(engine, key, folder=None):
    ctx, run = engine.ctx, engine.runner.run
    package = CATALOG[key]
    binary = engine.command(package)
    if not binary:
        raise InstallError("Executable missing after installation: " + package.command)
    folder = Path(folder) if folder is not None else ctx.home / "smoke"
    folder.mkdir(exist_ok=True)
    ctx.fix_owner(folder)
    sample = folder / "sample.txt"
    sample.write_text("XdebX functional test\n")
    ctx.fix_owner(sample)
    if key == "git":
        run([binary, "init", str(folder / "repository")])
        run([binary, "-C", folder / "repository", "-c", "user.name=XdebX", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "smoke"])
    elif key == "curl":
        _, output = run([binary, "--fail", "file://" + str(sample)])
        assert "functional test" in output
    elif key == "wget":
        import http.server
        import functools
        import threading
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(folder))
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            run([binary, "-O", folder / "download.txt", "http://127.0.0.1:{}/sample.txt".format(server.server_port)])
            assert (folder / "download.txt").read_text() == sample.read_text()
        finally:
            server.shutdown()
    elif key == "rsync":
        run([binary, sample, folder / "copied.txt"])
        assert (folder / "copied.txt").read_text() == sample.read_text()
    elif key == "lsd":
        _, output = run([binary, "--color", "never", folder])
        assert "sample.txt" in output
    elif key == "gdu":
        run([binary, "-n", "-p", folder])
    elif key == "chafa":
        # PNG is supported by both old ImageMagick and current built-in loaders.
        import zlib
        def chunk(kind, data):
            return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff)
        image = folder / "image.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
                          + chunk(b"IDAT", zlib.compress((b"\0" + bytes([255, 0, 0]) * 2) * 2)) + chunk(b"IEND", b""))
        _, output = run([binary, "--size", "4x2", image])
        assert output.strip(), "No rendered image output"
    elif key in ("btop", "htop"):
        interactive(engine, [binary])
    elif key == "vim9":
        output = folder / "vim-written.txt"
        scripts = folder / "vim-scripts.txt"
        check = folder / "vim-check.vim"
        check.write_text("call assert_true(exists(':NERDTree'))\ncall assert_true(exists(':Commentary'))\n"
                         "call assert_true(exists(':Git'))\ncall assert_equal('onedark', g:colors_name)\n"
                         "call writefile(split(execute('scriptnames'), '\\n'), '" + str(scripts) + "')\n"
                         "if !empty(v:errors) | cquit | endif\ncall setline(1, 'XdebX')\nwrite " + str(output) + "\nqa\n")
        run([binary, "-Nu", ctx.home / ".vimrc", "-i", "NONE", "-n", "-es", "-S", check])
        assert output.read_text().strip() == "XdebX"
        loaded = scripts.read_text().lower()
        for plugin in VIM_PLUGINS:
            assert "/.vim/plugged/" + plugin.lower() + "/" in loaded, "Plugin did not load: " + plugin
    elif key == "zsh-omz":
        _, output = run([binary, "-ic", "[[ $ZSH_THEME == jispwoso ]] && (( ${+functions[_zsh_highlight]} )) && (( ${+functions[_zsh_autosuggest_start]} )) && print XdebX-ready"])
        assert "XdebX-ready" in output and ".zshrc:" not in output, output
        assert all((ctx.home / ".oh-my-zsh/custom/plugins" / p / (p + ".zsh")).exists() for p in ("zsh-autosuggestions", "zsh-syntax-highlighting"))
    elif key == "tmux-tmuxp":
        socket = "xdebx-smoke"
        try:
            run([binary, "-L", socket, "-f", ctx.home / ".tmux.conf", "new-session", "-d", "-s", "smoke"])
            _, output = run([binary, "-L", socket, "show", "-g", "base-index"])
            assert "1" in output
            _, palette = run([binary, "-L", socket, "show", "-g", "status-bg"])
            assert "#" in palette, "Catppuccin theme did not load"
            # Sensible only changes escape-time when the upstream default is
            # 500. New tmux releases default to 10, which the plugin preserves.
            _, focus_events = run([binary, "-L", socket, "show", "-g", "focus-events"])
            _, display_time = run([binary, "-L", socket, "show", "-g", "display-time"])
            assert focus_events.strip().endswith(" on") and display_time.strip().endswith(" 4000"), "tmux-sensible did not load"
            run(["tmuxp", "-V"])
            profile = folder / "session.json"
            result_file = folder / "tmuxp-output.txt"
            profile.write_text(json.dumps({"session_name": "xdebx-tmuxp", "windows": [{"window_name": "main", "panes": ["printf XdebX > " + shlex.quote(str(result_file)) + "; sleep 10"]}]}))
            run(["tmuxp", "load", "-d", profile])
            for _ in range(30):
                if result_file.exists():
                    break
                time.sleep(0.1)
            assert result_file.read_text() == "XdebX"
        finally:
            run([binary, "-L", socket, "kill-server"], check=False)
            run([binary, "kill-server"], check=False)
    elif key == "nodejs22-nvm":
        run([binary, "-e", "if (process.versions.node.split('.')[0] !== '22') process.exit(1); console.log(6*7)"])
        run(["bash", "-c", '. "$HOME/.nvm/nvm.sh"; test "$(nvm alias default | head -1 | cut -d\' \' -f3)" != ""; node -e "console.log(42)"'])
        assert (ctx.home / ".nvm/alias/default").read_text().strip().startswith("v22")
    elif key == "uv":
        run([binary, "venv", "--python", "/usr/bin/python3", folder / "venv"])
        run([folder / "venv/bin/python", "-c", "print(6*7)"])
    elif key == "miniconda3":
        run([ctx.home / "Software/miniconda3/bin/python", "-c", "print(6*7)"])
        run([binary, "info", "--json"])
    elif key == "jdk":
        source = folder / "Hello.java"
        source.write_text('class Hello { public static void main(String[] a) { System.out.println("XdebX"); }}')
        run([binary, source])
        java = Path(binary).with_name("java")
        _, output = run([java, "-cp", folder, "Hello"])
        assert "XdebX" in output
    elif key == "go":
        source = folder / "main.go"
        source.write_text('package main\nimport "fmt"\nfunc main(){fmt.Println("XdebX")}')
        run([binary, "build", "-o", folder / "hello", source])
        run([folder / "hello"])
    elif key == "texlive":
        source = folder / "sample.tex"
        source.write_text('\\documentclass{ctexart}\n\\begin{document}XdebX 中文测试\\end{document}')
        run([binary, "-interaction=nonstopmode", "-halt-on-error", source], cwd=str(folder))
        assert (folder / "sample.pdf").stat().st_size > 100
    elif key == "redis":
        run([binary, "--daemonize", "yes", "--port", "6380", "--dir", folder, "--pidfile", folder / "redis.pid"])
        try:
            run(["redis-cli", "-p", "6380", "SET", "xdebx", "42"])
            _, output = run(["redis-cli", "-p", "6380", "GET", "xdebx"])
            assert output.strip() == "42"
        finally:
            run(["redis-cli", "-p", "6380", "SHUTDOWN", "NOSAVE"], check=False)
    elif key == "mysql":
        run(["service", "mysql", "start"], root=True)
        _, output = run(["mysql", "-N", "-e", "CREATE DATABASE IF NOT EXISTS xdebx_test; CREATE TABLE xdebx_test.t(v INT); INSERT INTO xdebx_test.t VALUES(42); SELECT v FROM xdebx_test.t; DROP DATABASE xdebx_test;"], root=True)
        assert "42" in output
        assert "MariaDB" not in run(["mysql", "--version"])[1]
    elif key == "docker":
        log = folder / "dockerd.log"
        command = ["dockerd", "--host=unix:///tmp/xdebx-docker.sock", "--storage-driver=vfs"]
        record = record_process(engine, command)
        with log.open("w") as stream:
            daemon = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT)
        try:
            for _ in range(60):
                probe = ["docker", "-H", "unix:///tmp/xdebx-docker.sock", "info"]
                probe_record = record_process(engine, probe)
                probe_code = subprocess.run(probe, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
                probe_record["exit_code"] = probe_code
                if probe_code == 0:
                    break
                if daemon.poll() is not None:
                    raise InstallError(log.read_text()[-2500:])
                time.sleep(1)
            run(["docker", "-H", "unix:///tmp/xdebx-docker.sock", "run", "--rm", "hello-world"], root=True)
        finally:
            daemon.terminate()
            daemon.wait(20)
            record.update(exit_code=daemon.returncode, terminated_after_validation=True)
    elif key == "maple-mono-nf":
        _, output = run([binary, "Maple Mono NF"])
        assert "Maple" in output
        _, registered = run(["fc-list", "--format=%{file}\\n"])
        for asset in engine.resources.manifest["local_assets"]:
            font = ctx.home / ".local/share/fonts/MapleMono-NF" / Path(asset["filename"]).name
            assert sha256(font) == asset["sha256"]
            _, properties = run(["fc-scan", "--format=%{family} %{style}\\n", font])
            assert "Maple" in properties and str(font) in registered, "Fontconfig did not recognize " + font.name
    elif key == "clashterminalx":
        run([binary, "help"])
    elif package.desktop:
        engine.apt_install(["xvfb", "x11-utils", "dbus-x11"])
        # Use a user session: Electron sandboxes are not disabled for normal use.
        args = []
        if key in ("google-chrome", "visual-studio-code", "chatgpt-desktop", "ctypora"):
            args += ["--no-sandbox", "--disable-gpu"]  # Container test only.
        if key == "wechat":
            engine.apt_install(["strace"])
            args += ["--no-sandbox", "--disable-gpu"]  # CEF container startup diagnostics.
        if key == "visual-studio-code":
            args += ["--user-data-dir=" + str(folder / "editor-data")]
        if key == "nomachine":
            binary = "/usr/NX/bin/nxplayer"
        graphical(engine, binary, args, folder)
    else:
        run([binary] + package.probe)
        if key in ("claude-code", "opencode", "openclaw"):
            run([binary, "--help"])
            if key == "openclaw":
                assert (ctx.home / ".nvm/alias/default").read_text().strip().startswith("v22")


def script_case(engine, entry, key):
    engine.apt_install(["curl", "wget", "git", "xz-utils"])
    path = engine.resources.get(entry)
    run = engine.runner.run
    if key in ("claude-code", "opencode", "openclaw"):
        engine.node(22)
        runtime = engine.node(24) if key == "openclaw" else engine.node(22)
        env = {"PATH": str(runtime / "bin") + ":" + engine.ctx.env["PATH"]}
        if key == "openclaw":
            version = engine.resources.entries("openclaw", online=True)[0]["version"]
            run(["bash", path, "--no-onboard", "--no-prompt", "--version", version], env=env)
            script = '#!/bin/sh\nexport PATH={}:"$PATH"\nexec {} "$@"\n'.format(shlex.quote(str(runtime / "bin")), shlex.quote(str(runtime / "bin/openclaw")))
            engine.configurator.write(engine.ctx.home / ".local/bin/openclaw", script, 0o755)
        elif key == "claude-code":
            version = engine.resources.entries("@anthropic-ai/claude-code", online=True)[0]["version"]
            run(["bash", path, version], env=env)
        else:
            version = engine.resources.entries("opencode-ai", online=True)[0]["version"]
            run(["bash", path, "--version", version], env=env)
    elif entry["id"] == "nvm":
        run(["bash", path], env={"PROFILE": "/dev/null", "NVM_DIR": str(engine.ctx.home / ".nvm"), "METHOD": "script"})
        engine.node(22)
    elif entry["id"] == "ohmyzsh-install":
        engine.apt_install(["zsh"])
        run(["bash", path], env={"RUNZSH": "no", "CHSH": "no", "KEEP_ZSHRC": "yes"})
    elif key == "docker":
        run(["sh", path], root=True, transaction=True)
    elif key == "uv":
        run(["sh", path], env={"UV_INSTALL_DIR": str(engine.ctx.home / ".local/bin"), "UV_NO_MODIFY_PATH": "1"})
    elif key == "clashterminalx":
        engine.strategy(CATALOG[key], "official")
    else:
        raise InstallError("No full bootstrap test recipe")


def behaviour(engine):
    """Real package locks, user files and subprocesses in the disposable system."""
    from xdebx.runtime import Runner
    runner, ctx = engine.runner, engine.ctx
    checked = []
    try:
        runner.run(["sleep", "5"], timeout=0.3)
        raise AssertionError("Timeout did not stop the process")
    except InstallError as exc:
        assert "timeout" in str(exc).lower()
        checked.append("subprocess timeout")
    import tarfile
    with tempfile.TemporaryDirectory() as tmp:
        entry = next(e for e in engine.resources.cached if e.get("download_enabled") is not False and e.get("sha256"))
        wrong = Path(tmp) / "wrong"
        wrong.write_text("wrong hash")
        bundle = Path(tmp) / "wrong.tar.gz"
        with tarfile.open(bundle, "w:gz") as archive:
            archive.add(wrong, arcname="data/pkgs/" + entry["filename"])
        try:
            engine.resources.import_cache(bundle)
            raise AssertionError("Corrupt imported cache accepted")
        except InstallError as exc:
            assert "mismatch" in str(exc)
    checked.append("corrupt cache rejected")
    apt_files = [Path("/etc/apt/sources.list")] + list(Path("/etc/apt/sources.list.d").glob("*"))
    before = {str(p): sha256(p) for p in apt_files if p.is_file()}
    ctx.settings["mirror"] = "CN"
    engine.prepare_apt()
    engine.prepare_source_apt()
    assert before == {str(p): sha256(p) for p in apt_files if p.is_file()}
    checked.append("CN/source task leaves global APT files unchanged")
    ctx.settings["mirror"] = "Raw"
    engine.apt_install(["tmux", "git"])
    assert engine.detected("tmux-tmuxp")["status"] == "partial"
    state = engine.execute(["tmux-tmuxp"])
    partial = next(row for row in state if row["id"] == "tmux-tmuxp")
    assert partial["status"] == "success", state
    smoke(engine, "tmux-tmuxp")
    checked.append("partial install repaired through normal install action")
    target = ctx.home / "config-restore-test"
    target.write_text("original")
    target.chmod(0o640)
    ctx.fix_owner(target)
    engine.configurator.write(target, "managed")
    record = next(p for p in ctx.backups.glob("*/record.json") if json.loads(p.read_text())["destination"] == str(target))
    target.write_text("user edit")
    try:
        engine.configurator.restore(record)
        raise AssertionError("Conflicting restore accepted")
    except InstallError:
        pass
    engine.configurator.restore(record, force=True)
    assert target.read_text() == "original" and target.stat().st_mode & 0o777 == 0o640
    assert target.stat().st_uid == ctx.uid
    checked.append("configuration backup, conflict preservation and target ownership")
    fake_vim = ctx.home / ".local/bin/vim"
    fake_vim.parent.mkdir(parents=True, exist_ok=True)
    fake_vim.write_text("#!/bin/sh\nprintf 'VIM - Vi IMproved 8.1\\n'\n")
    fake_vim.chmod(0o755)
    ctx.fix_owner(fake_vim)
    try:
        assert engine.detected("vim9")["status"] == "upgrade-required"
    finally:
        fake_vim.unlink()
    checked.append("injected Vim 8 is rejected as satisfying Vim 9")
    original_strategy = engine.strategy
    def failure(package, *args, **kwargs):
        if package.id == "nodejs22-nvm":
            raise InstallError("Injected unavailable dependency")
        return original_strategy(package, *args, **kwargs)
    engine.strategy = failure
    try:
        results = engine.execute(["claude-code", "rsync"], configure=False)
        assert [r["status"] for r in results] == ["failed", "skipped", "success"], results
        smoke(engine, "rsync")
    finally:
        engine.strategy = original_strategy
    checked.append("dependency skip and independent-item continuation")
    holder = subprocess.Popen(["python3", "-c", "import fcntl,sys; f=open('/var/lib/dpkg/lock-frontend','w'); fcntl.lockf(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read()"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        try:
            engine.apt_install(["wget"])
            raise AssertionError("Foreign APT lock ignored")
        except InstallError as exc:
            assert "lock" in str(exc).lower(), str(exc)
    finally:
        holder.stdin.close()
        holder.wait(5)
        holder.stdout.close()
    engine.apt_install(["wget"])
    smoke(engine, "wget")
    checked.append("APT lock timeout and subsequent recovery")
    print("Behaviour checks: " + json.dumps(checked), flush=True)
    return {"status": "passed", "validation": checked}


def test_case(engine, case):
    key, entry = case["software"], case.get("entry")
    if case["kind"] == "integration":
        if key == "tui":
            return tui(engine)
        if key == "behaviour":
            return behaviour(engine)
        if key == "music":
            engine.apt_install(["ffmpeg"])
            path = Path("/project/data/music/電脳眠眠猫short.mp3")
            engine.runner.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"], timeout=60)
            engine.runner.run(["ffplay", "-nodisp", "-autoexit", "-t", "2", "-loglevel", "error", path], env={"SDL_AUDIODRIVER": "dummy"}, timeout=20)
            return {"status": "passed", "audio_device": "container-limited", "validation": "MP3 decode and playback process with dummy output"}
        fast = json.loads(Path("/project/src/config.json").read_text())["fast_install"]["items"]
        if key.startswith("fast"):
            engine.ctx.desktop = key == "fast-desktop"
            ids = [k for k in fast if (engine.ctx.desktop or not CATALOG[k].desktop)
                   and engine.detected(k)["status"] != "unsupported"]
        elif key == "select-network":
            ids = ["clash-verge"] if engine.detected("clash-verge")["status"] != "unsupported" else []
        else:
            engine.ctx.desktop = engine.ctx.target == "ubuntu20"
            ids = [k for k in CATALOG if k != "clash-verge" and (engine.ctx.desktop or not CATALOG[k].desktop)
                   and engine.detected(k)["status"] != "unsupported"]
        results = engine.execute(ids, allow_unverified=True)
        if any(r["status"] not in ("success", "installed") for r in results):
            raise InstallError("Integration results: " + json.dumps(results))
        for software in ids:
            smoke(engine, software)
        repeat = engine.execute(ids)
        assert all(r["status"] == "installed" for r in repeat)
        return {"results": results}
    companion_hosts = {"git-man": "git", "vim-runtime": "vim9", "zsh-common": "zsh-omz"}
    if entry and entry["id"] in companion_hosts:
        key = companion_hosts[entry["id"]]
    package = CATALOG.get(key)
    if entry and entry.get("targets") and engine.ctx.target not in entry["targets"]:
        return {"status": "unsupported", "reason": "Outside declared target list"}
    if package and engine.detected(key)["status"] == "unsupported":
        return {"status": "unsupported", "reason": engine.detected(key)["detail"]}
    if package and package.dependencies:
        dependencies = engine.execute(package.dependencies, configure=False)
        if any(r["status"] not in ("success", "installed") for r in dependencies):
            raise InstallError("Dependency setup failed: " + json.dumps(dependencies))
    if entry and "kind" in entry:
        kind = entry["kind"]
        if kind == "checksums":
            path = engine.resources.get(entry)
            version = entry["version"]
            matches = [e for e in engine.resources.cached if e.get("version") == version and e.get("kind") == "archive" and e["id"].startswith("nodejs")]
            assert matches
            for asset in matches:
                local = engine.resources.get(asset)
                assert any(line.split()[-1].lstrip("*") == Path(asset["filename"]).name and line.split()[0] == sha256(local) for line in path.read_text().splitlines() if line.split())
            return {"status": "passed", "validation": "Referenced Node archive hashes matched"}
        if kind == "deb" and entry["id"] in companion_hosts:
            base = next(e for e in engine.resources.cached
                        if e["id"] == package.resource and e["version"] == entry["version"] and e["kind"] == "deb")
            engine.strategy(package, "cache", base)
            engine.configurator.apply(key, switch_shell=False)
        elif kind == "deb" and key not in CATALOG:
            engine.deb(entry)
            _, version = engine.runner.run(["dpkg-query", "-W", "-f=${Version}", entry.get("deb_control", {}).get("Package", entry["id"])])
            assert version.strip() == entry["version"]
            return {"status": "passed", "resolved_version": version}
        elif kind == "wheel":
            engine.strategy(CATALOG["uv"], "cache")
            engine.apt_install(["tmux"])
            path = engine.resources.get(entry)
            engine.runner.run(["uv", "tool", "install", "--python", "3.12", str(path)])
            engine.configurator.apply("tmux-tmuxp", switch_shell=False)
            smoke(engine, "tmux-tmuxp")
            return {"status": "passed", "resolved_version": engine.runner.run(["tmuxp", "-V"])[1].strip()}
        elif kind in ("git-snapshot", "vim-plugin"):
            engine.resources.validate(engine.resources.get(entry), entry)
            engine.apt_install(package.apt)
            if key == "vim9" and engine.detected(key)["status"] == "upgrade-required":
                engine.source(package)
            engine.configurator.apply(key, switch_shell=False)
        elif kind == "script":
            script_case(engine, entry, key)
            engine.configurator.apply(key, switch_shell=False)
        elif entry["id"] == "nvm":
            engine.node(22)
        elif entry["id"] == "nodejs-openclaw-runtime":
            runtime = engine.node(24, entry_override=entry)
            engine.runner.run([runtime / "bin/node", "-e", "if (!process.version.startsWith('v24.')) process.exit(1)"])
            return {"status": "passed", "resolved_version": entry["version"]}
        elif entry["id"] == "mysql-apt-config":
            # This is a repository configurator, not a database binary.
            engine.deb(entry)
            engine.strategy(package, "official")
            smoke(engine, key)
            return {"status": "passed", "validation": "Repository configurator plus real MySQL read/write"}
        elif entry["id"] == "texlive-netinstaller":
            engine.apt_install(["perl", "wget", "xz-utils", "fontconfig"])
            with tempfile.TemporaryDirectory() as tmp:
                safe_extract(engine.resources.get(entry), tmp)
                root = next(Path(tmp).iterdir())
                prefix = engine.ctx.home / "Software/texlive"
                profile = Path(tmp) / "profile"
                profile.write_text("selected_scheme scheme-small\nTEXDIR {}\nTEXMFCONFIG {}/.texlive/texmf-config\nTEXMFVAR {}/.texlive/texmf-var\nTEXMFHOME {}/texmf\ncollection-xetex 1\ncollection-langchinese 1\noption_doc 0\noption_src 0\n".format(prefix, engine.ctx.home, engine.ctx.home, engine.ctx.home))
                os.chmod(tmp, 0o755)
                engine.ctx.fix_owner(Path(tmp))
                engine.runner.run(["perl", root / "install-tl", "-profile", profile, "-repository", "https://mirror.ctan.org/systems/texlive/tlnet/"], timeout=3600)
                bin_dir = prefix / "bin/x86_64-linux"
                engine.ctx.env["PATH"] = str(bin_dir) + ":" + engine.ctx.env["PATH"]
            smoke(engine, key)
            return {"status": "passed", "resolved_version": engine.runner.run([engine.command(package), "--version"])[1].splitlines()[0]}
        elif kind == "deb" and entry["id"] == "tmuxp":
            engine.apt_install(["tmux"])
            engine.deb(entry)
            engine.configurator.apply(key, switch_shell=False)
        elif kind == "source":
            engine.source(package, entry)
            engine.configurator.apply(key, switch_shell=False)
        else:
            method = "cache" if case["kind"] == "cached" else "official"
            engine.strategy(package, method, entry, allow_unverified=True)
            engine.configurator.apply(key, switch_shell=False)
    else:
        engine.strategy(package, case["method"], entry, allow_unverified=True)
        engine.configurator.apply(key, switch_shell=False)
    if entry and entry.get("kind") == "deb":
        _, version = engine.runner.run(["dpkg-query", "-W", "-f=${Version}", entry.get("deb_control", {}).get("Package", entry["id"])])
        expected = entry.get("deb_control", {}).get("Version", entry["version"])
        assert version.strip() == expected, (version, expected)
    smoke(engine, key)
    detected = engine.detected(key)
    package_versions = {}
    for name in package.apt:
        code, version = engine.runner.run(["dpkg-query", "-W", "-f=${Version}", name], check=False, quiet=True)
        if code == 0:
            package_versions[name] = version.strip()
    if case["kind"] == "strategy" and detected["status"] != "installed":
        return {"status": "unsupported", "resolved_version": detected["detail"], "package_versions": package_versions,
                "eligible_for_vim9": False if key == "vim9" else None,
                "rejection_verified": key == "vim9",
                "reason": "Native candidate does not satisfy requested capability: " + detected["detail"]}
    if key == "vim9" and detected["status"] != "installed":
        return {"status": "unsupported", "resolved_version": detected["detail"], "package_versions": package_versions,
                "eligible_for_vim9": False, "rejection_verified": True, "reason": "Candidate can run but does not satisfy Vim >=9"}
    return {"status": "passed", "resolved_version": detected["detail"],
            "package_versions": package_versions,
            "eligible_for_vim9": detected["status"] == "installed" if key == "vim9" else None}


def main():
    case = json.loads(Path(sys.argv[1]).read_text())
    ctx = Context("xdebx", "yes" if case["target"] == "ubuntu20" else "no")
    engine = Installer(ctx, lambda line: print(line, flush=True))
    engine.resources.prefer_snapshots = True
    engine.resources.ignore_compatibility = case["kind"] != "integration"
    engine.resources.require_official_download = case["kind"] != "integration" and case["method"] == "official"
    result = {}
    try:
        result = test_case(engine, case)
        result.setdefault("status", "passed")
    except Unsupported as exc:
        result = {"status": "unsupported", "reason": str(exc)}
    except (Exception, AssertionError) as exc:
        import traceback
        traceback.print_exc()
        reason = str(exc)
        blocked = any(s in reason.lower() for s in ("download blocked", "could not resolve", "connection timed out", "temporary failure resolving", "network is unreachable", "could not connect", "unable to connect"))
        result = {"status": "blocked" if blocked else "failed", "reason": reason[-5000:]}
    finally:
        engine.close()
        result["desktop_context"] = ctx.desktop
        atomic_json(Path(sys.argv[1]).parent / "commands.json", engine.runner.records, 0o644)
        # Preserve functional evidence before the disposable container is removed.
        import shutil
        evidence = ctx.home / "smoke"
        if evidence.exists():
            destination = Path(sys.argv[1]).parent / "artifacts"
            destination.mkdir(exist_ok=True)
            os.chown(destination, ctx.uid, ctx.gid)
            for name in ("desktop.log", "desktop-strace.log", "xvfb.log", "windows.txt", "sample.pdf", "dockerd.log", "vim-scripts.txt"):
                asset = evidence / name
                if asset.is_file():
                    shutil.copy2(str(asset), str(destination / name))
        atomic_json(Path(sys.argv[1]).parent / "outcome.json", result, 0o644)
    return 0 if result["status"] in ("passed", "unsupported", "container-limited") else 1


if __name__ == "__main__":
    sys.exit(main())
