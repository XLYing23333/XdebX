#!/usr/bin/env python3
"""Serial, disposable-container acceptance matrix. No global Docker cleanup."""
import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from xdebx.catalog import CATALOG
from xdebx.runtime import atomic_json

IMAGES = {"ubuntu20": "ubuntu:20.04", "ubuntu22": "ubuntu:22.04", "ubuntu24": "ubuntu:24.04",
          "debian12": "debian:12-slim", "debian13": "debian:13-slim"}
ALIASES = {"vim": "vim9", "vim9-source": "vim9", "zsh": "zsh-omz", "tmux": "tmux-tmuxp",
           "nodejs": "nodejs22-nvm", "nodejs-openclaw-runtime": "nodejs22-nvm", "ghostty-ubuntu": "ghostty",
           "nvm": "nodejs22-nvm", "tmuxp": "tmux-tmuxp", "ohmyzsh-install": "zsh-omz", "docker-bootstrap": "docker",
           "uv": "uv", "claude-code-bootstrap": "claude-code", "@anthropic-ai/claude-code": "claude-code",
           "opencode-bootstrap": "opencode", "opencode-ai": "opencode", "openclaw-bootstrap": "openclaw",
           "clashterminalx-installer": "clashterminalx", "texlive-netinstaller": "texlive",
           "CTypora": "ctypora", "mysql-apt-config": "mysql", "jdk-temurin21": "jdk", "chrome": "google-chrome",
           "vscode": "visual-studio-code", "chatgpt": "chatgpt-desktop", "clash-verge-rev": "clash-verge",
           "nomachine-personal-edition": "nomachine"}


def matrix():
    manifest = json.loads((ROOT / "data/pkgs/manifest.json").read_text())
    online = json.loads((ROOT / "data/pkgs/online-sources.json").read_text())["sources"]
    cases = []
    for target, image in IMAGES.items():
        for key, package in CATALOG.items():
            if target in package.excluded_targets:
                continue
            cases.append({"target": target, "image": image, "software": key, "kind": "strategy", "method": "official", "version": "resolved-on-test"})
            if package.source:
                cases.append({"target": target, "image": image, "software": key, "kind": "strategy", "method": "source", "version": "resolved-on-test"})
        for entry in manifest["packages"]:
            if entry.get("download_enabled") is False:
                continue
            if target not in entry.get("targets", []) and entry["id"] != "CTypora":
                continue
            key = ALIASES.get(entry["id"], entry["id"])
            if key in CATALOG and target in CATALOG[key].excluded_targets:
                continue
            if entry["kind"] in ("git-snapshot", "vim-plugin"):
                # Validate each snapshot in an isolated host application session.
                key = "zsh-omz" if entry["id"] in ("ohmyzsh", "zsh-autosuggestions", "zsh-syntax-highlighting") else "tmux-tmuxp" if entry["id"] in ("tpm", "tmux-sensible", "catppuccin-tmux") else "vim9"
            cases.append({"target": target, "image": image, "software": key, "kind": "cached", "method": "cache",
                          "version": entry["version"], "entry": entry})
        for entry in online:
            if entry["id"] == "goland":
                continue  # Human explicitly chose the Go toolchain, not GoLand IDE.
            key = ALIASES.get(entry["id"], entry["id"])
            if key in CATALOG and target in CATALOG[key].excluded_targets:
                continue
            cases.append({"target": target, "image": image, "software": key, "kind": "online", "method": "official",
                          "version": entry["version"], "entry": entry})
        for mode in ("fast", "fast-desktop", "select", "select-network", "behaviour", "music", "tui"):
            cases.append({"target": target, "image": image, "software": mode, "kind": "integration", "version": "0.1.0", "method": "auto"})
    cases.sort(key=lambda case: case["kind"] == "integration")
    for case in cases:
        identity = json.dumps(case, sort_keys=True).encode()
        import hashlib
        case["case_id"] = "{}-{}-{}".format(case["target"], case["software"], hashlib.sha256(identity).hexdigest()[:8])
    return cases


def command(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def desktop_scope_reason(case):
    """The user waived desktop acceptance outside Ubuntu 20, not availability."""
    if case["target"] == "ubuntu20":
        return None
    package = CATALOG.get(case["software"])
    if package and package.desktop or case["kind"] == "integration" and case["software"] in ("fast-desktop", "select-network"):
        return "Desktop tests outside Ubuntu 20 waived by user; availability is unverified"
    return None


def debian12_reference(case, cases):
    """Only Debian 13-specific recipes need independent acceptance now."""
    if case["target"] != "debian13":
        return None
    # Debian 12 lacks native fastfetch/source and JDK 21; its successful
    # official recipes use portable/Temurin archives. Trixie has native routes.
    unique = {("fastfetch", "strategy", "official"), ("fastfetch", "strategy", "source"),
              ("jdk", "strategy", "official")}
    if (case["software"], case["kind"], case["method"]) in unique:
        return None
    fields = ("software", "kind", "method", "version", "entry")
    return next((peer for peer in cases if peer["target"] == "debian12"
                 and all(peer.get(field) == case.get(field) for field in fields)), None)


def scope_reason(case, cases):
    return desktop_scope_reason(case) or (
        "Shared Debian 12 installation recipe; independent Debian 13 test waived by user"
        if debian12_reference(case, cases) else None)


class DiskSpaceError(OSError):
    pass


def check_disk_space(out, minimum_gib):
    # The inspected Docker/containerd store lives on the root filesystem.
    for path in (Path("/"), Path(out)):
        free = shutil.disk_usage(str(path)).free
        if free < minimum_gib * 1024 ** 3:
            raise DiskSpaceError("Disk space guard: {} has {:.1f} GiB free; requires {} GiB".format(
                path, free / 1024 ** 3, minimum_gib))


def wait_container(name, out, timeout):
    deadline = time.monotonic() + timeout
    while True:
        check_disk_space(out, 5)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(["docker", "wait", name], timeout)
        try:
            return subprocess.run(["docker", "wait", name], capture_output=True, text=True,
                                  timeout=min(10, remaining))
        except subprocess.TimeoutExpired:
            continue


def run_case(case, out, timeout=3600):
    directory = out / case["case_id"]
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "case.json", case, 0o644)
    name = "xdebx-test-" + uuid.uuid4().hex[:12]
    log = directory / "container.log"
    started = time.time()
    status = {"case_id": case["case_id"], "target": case["target"], "software": case["software"],
              "candidate_version": case["version"], "kind": case["kind"], "method": case["method"],
              "status": "blocked", "started_at": started, "acceptance_schema": 2}
    status["container_name"] = name
    import hashlib
    source_hash = hashlib.sha256()
    for source in sorted((ROOT / "src").rglob("*")):
        if source.is_file() and "__pycache__" not in source.parts:
            source_hash.update(str(source.relative_to(ROOT)).encode())
            source_hash.update(source.read_bytes())
    status["source_sha256"] = source_hash.hexdigest()
    harness_hash = hashlib.sha256()
    for source in (ROOT / "tools/docker_matrix.py", ROOT / "tools/container_case.py"):
        harness_hash.update(source.name.encode())
        harness_hash.update(source.read_bytes())
    status["harness_sha256"] = harness_hash.hexdigest()
    created = False
    try:
        check_disk_space(out, 15)
        inspected = subprocess.run(["docker", "image", "inspect", case["image"]], capture_output=True, text=True)
        if inspected.returncode:
            command(["docker", "pull", "--platform", "linux/amd64", case["image"]], stdout=subprocess.DEVNULL)
            inspected = command(["docker", "image", "inspect", case["image"]], capture_output=True, text=True)
        image = json.loads(inspected.stdout)[0]
        status["image_id"], status["image_digest"] = image["Id"], image.get("RepoDigests", [])
        fixture = "xdebx-test-base:" + case["target"]
        fixture_inspect = subprocess.run(["docker", "image", "inspect", fixture], capture_output=True, text=True)
        if fixture_inspect.returncode:
            recipe = ('FROM {}\nLABEL xdebx.fixture="true"\n'
                      'RUN apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 update && '
                      'DEBIAN_FRONTEND=noninteractive apt-get -y --no-install-recommends install python3 ca-certificates sudo && '
                      'useradd -m -s /bin/bash xdebx\n').format((image.get("RepoDigests") or [case["image"]])[0])
            with (directory / "fixture.log").open("w") as stream:
                command(["docker", "build", "-t", fixture, "-"], input=recipe, text=True, stdout=stream, stderr=subprocess.STDOUT)
        status["fixture"] = fixture
        fixture_data = json.loads(command(["docker", "image", "inspect", fixture], capture_output=True, text=True).stdout)[0]
        status["fixture_id"] = fixture_data["Id"]
        base_layers = image.get("RootFS", {}).get("Layers", [])
        if fixture_data.get("RootFS", {}).get("Layers", [])[:len(base_layers)] != base_layers:
            raise ValueError("Fixture does not derive from the recorded base image; rebuild this fixture")
        args = ["docker", "create", "--platform", "linux/amd64", "--name", name, "--label", "xdebx.test=true",
                "--memory", "6g", "--pids-limit", "2048", "--ulimit", "core=0:0", "--init", "--mount", "type=bind,src={},dst=/project,readonly".format(ROOT),
                "--mount", "type=bind,src={},dst=/results".format(directory.resolve()),
                "--env", "PYTHONPATH=/project/src", "--env", "TERM=xterm-256color"]
        if case["software"] == "docker" or case["kind"] == "integration" and case["software"] in ("fast", "fast-desktop", "select"):
            args += ["--privileged"]  # Disposable DinD; never expose host Docker socket.
        bootstrap = 'exec python3 /project/tools/container_case.py /results/case.json'
        args += [fixture, "sh", "-c", bootstrap]
        command(args, stdout=subprocess.DEVNULL)
        created = True
        command(["docker", "start", name], stdout=subprocess.DEVNULL)
        with log.open("w") as stream:
            follower = subprocess.Popen(["docker", "logs", "--follow", name], stdout=stream, stderr=subprocess.STDOUT)
            try:
                wait = wait_container(name, out, timeout)
                code = int(wait.stdout.strip()) if wait.returncode == 0 else -1
                status["exit_code"] = code
            except subprocess.TimeoutExpired:
                status.update(status="blocked", reason="Case deadline exceeded")
                command(["docker", "stop", "-t", "30", name], stdout=subprocess.DEVNULL)
            finally:
                try:
                    follower.wait(5)
                except subprocess.TimeoutExpired:
                    follower.terminate()
                    follower.wait()
        outcome = directory / "outcome.json"
        if outcome.exists():
            status.update(json.loads(outcome.read_text()))
        elif "reason" not in status:
            status.update(status="blocked", reason="Container bootstrap or infrastructure failed; see container.log")
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        status.update(status="blocked", reason=str(exc))
        if isinstance(exc, DiskSpaceError):
            status["disk_space_blocked"] = True
    finally:
        if created:
            cleanup = subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, text=True)
            status["cleanup_ok"] = cleanup.returncode == 0
            if status["cleanup_ok"]:
                absent = subprocess.run(["docker", "container", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                status["cleanup_ok"] = absent.returncode != 0
            if not status["cleanup_ok"]:
                status.update(status="failed", reason="Container cleanup failed: " + (cleanup.stderr or "container still exists after removal"))
        status["elapsed_seconds"] = round(time.time() - started, 2)
        atomic_json(directory / "result.json", status, 0o644)
    return status


def summarize(out, cases):
    results = []
    for case in cases:
        path = out / case["case_id"] / "result.json"
        result = json.loads(path.read_text()) if path.exists() else {"case_id": case["case_id"], "target": case["target"], "software": case["software"], "status": "not-run"}
        reason = scope_reason(case, cases)
        if reason and result["status"] not in ("passed", "unsupported"):
            result = dict(result, previous_status=result["status"], status="skipped", reason=reason, scope_excluded=True)
            reference = debian12_reference(case, cases)
            if reference and not desktop_scope_reason(case):
                reference_path = out / reference["case_id"] / "result.json"
                evidence = json.loads(reference_path.read_text()) if reference_path.exists() else {}
                result.update(reference_case_id=reference["case_id"], reference_target="debian12",
                              reference_status=evidence.get("status", "not-run"),
                              support_inferred=evidence.get("status") == "passed" and evidence.get("eligible_for_vim9") is not False)
        results.append(result)
    atomic_json(out / "summary.json", results, 0o644)
    report = ["# Docker acceptance results", "", "Each case uses a fresh container; tests run serially.", "", "| Case | System | Software | Status |", "|---|---|---|---|"]
    report += ["| {} | {} | {} | {} |".format(r["case_id"], r["target"], r["software"], r["status"]) for r in results]
    (out / "report.md").write_text("\n".join(report) + "\n")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "test-results/docker")
    parser.add_argument("--target", choices=list(IMAGES))
    parser.add_argument("--software")
    parser.add_argument("--case")
    parser.add_argument("--phase", choices=("candidates", "integration"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry", action="store_true", help="Retry failed/blocked cases")
    parser.add_argument("--revalidate", action="store_true", help="Re-run passes that predate complete command/window evidence")
    parser.add_argument("--stop-on-failure", action="store_true")
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    cases = matrix()
    if args.case and not any(case["case_id"] == args.case for case in cases):
        parser.error("Unknown case ID: " + args.case)
    if args.software and not any(case["software"] == args.software for case in cases):
        parser.error("Unknown matrix software: " + args.software)
    args.output.mkdir(parents=True, exist_ok=True)
    # Migrate the original numbered IDs without discarding previous test evidence.
    for case in cases:
        destination = args.output / case["case_id"]
        if destination.exists():
            continue
        matches = list(args.output.glob("[0-9][0-9][0-9][0-9]-" + case["case_id"]))
        if len(matches) == 1:
            matches[0].rename(destination)
            atomic_json(destination / "case.json", case, 0o644)
            result = destination / "result.json"
            if result.exists():
                value = json.loads(result.read_text())
                value["case_id"] = case["case_id"]
                atomic_json(result, value, 0o644)
    atomic_json(args.output / "matrix.json", cases, 0o644)
    if args.generate:
        summarize(args.output, cases)
        print("{} cases generated".format(len(cases)))
        return
    serial_path = ROOT / "test-results/docker-serial.lock"
    serial_path.parent.mkdir(parents=True, exist_ok=True)
    serial_lock = serial_path.open("a+")
    try:
        fcntl.flock(serial_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("Another XdebX Docker acceptance run is active")
    count = 0
    for case in cases:
        if scope_reason(case, cases):
            continue
        if args.target and case["target"] != args.target or args.software and case["software"] != args.software or args.case and case["case_id"] != args.case:
            continue
        if args.phase and (case["kind"] == "integration") != (args.phase == "integration"):
            continue
        path = args.output / case["case_id"] / "result.json"
        if args.resume and path.exists():
            previous = json.loads(path.read_text())
            legacy_pass = args.revalidate and previous["status"] == "passed" and (
                not previous.get("fixture_id") or not (path.parent / "commands.json").exists()
                or CATALOG.get(case["software"]) and CATALOG[case["software"]].desktop and not (path.parent / "artifacts/windows.txt").exists())
            if not legacy_pass and (not args.retry or previous["status"] in ("passed", "unsupported", "container-limited")):
                continue
        if path.exists():
            attempt = str(time.time_ns())
            history = path.parent / "previous" / attempt
            history.mkdir(parents=True, exist_ok=True)
            for name in ("result.json", "outcome.json", "container.log", "commands.json", "artifacts"):
                p = path.parent / name
                if p.exists():
                    try:
                        p.rename(history / name)
                    except PermissionError:
                        if not p.is_dir():
                            raise
                        # Legacy container-owned evidence can still be preserved
                        # read-only. The next container fixes its output ownership.
                        shutil.copytree(str(p), str(history / name))
        print("START " + case["case_id"], flush=True)
        result = run_case(case, args.output, args.timeout)
        print("END {} {} ({:.1f}s) {}".format(case["case_id"], result["status"], result["elapsed_seconds"], result.get("reason", "")[:200]), flush=True)
        summarize(args.output, cases)
        count += 1
        if not result.get("cleanup_ok", True):
            break
        if result.get("disk_space_blocked"):
            break
        if args.stop_on_failure and result["status"] in ("failed", "blocked"):
            break
        if args.limit and count >= args.limit:
            break
    summarize(args.output, cases)


if __name__ == "__main__":
    main()
