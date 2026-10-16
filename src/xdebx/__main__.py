import argparse
import json
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import __version__
from .catalog import CATALOG
from .installer import Installer
from .runtime import PROJECT, Context, InstallError, read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="XdebX system setup / 系统环境配置")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--lite", action="store_true", help="Plain TUI: no music, animation, colors or borders")
    parser.add_argument("--user")
    parser.add_argument("--desktop", choices=["auto", "yes", "no"], default="auto")
    parser.add_argument("--language", choices=["EN", "CN"])
    parser.add_argument("--mirror", choices=["Raw", "CN"])
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--install", nargs="+", choices=list(CATALOG))
    parser.add_argument("--configure", choices=list(CATALOG))
    parser.add_argument("--restore", metavar="BACKUP_RECORD")
    parser.add_argument("--force", action="store_true", help="Restore a modified file after preserving it")
    parser.add_argument("--import-cache", metavar="ARCHIVE")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Accept the displayed installation/cleanup plan")
    parser.add_argument("--allow-unverified", action="store_true", help="Explicitly accept the provided CTypora asset")
    parser.add_argument("--method", choices=["auto", "official", "cache", "source"], default="auto")
    args = parser.parse_args(argv)
    engine = None
    try:
        ctx = Context(args.user, args.desktop, args.mirror, args.language)
        engine = Installer(ctx, lambda line: print(line, file=sys.stderr, flush=True))
        if args.doctor:
            integrity = engine.resources.inspect_local()
            print(json.dumps({"version": __version__, "user": ctx.user, "home": str(ctx.home),
                              "target": ctx.target, "architecture": ctx.arch, "desktop": ctx.desktop,
                              "settings": ctx.settings, "supported": bool(ctx.target and ctx.arch == "x86_64"),
                              "font_assets": all((PROJECT / e["filename"]).is_file() for e in engine.resources.manifest["local_assets"]),
                              "local_integrity": integrity}, indent=2))
            return 1 if integrity["errors"] else 0
        if args.list:
            print(json.dumps([dict(id=p.id, name=p.name, **engine.detected(p.id)) for p in CATALOG.values()
                              if (ctx.desktop or not p.desktop) and ctx.target not in p.excluded_targets], ensure_ascii=False, indent=2))
            return 0
        if args.import_cache:
            print(json.dumps(engine.resources.import_cache(args.import_cache), indent=2))
            return 0
        if args.restore:
            engine.configurator.restore(args.restore, args.force)
            return 0
        if args.cleanup:
            print("Remove XdebX settings, history and downloaded cache; preserve software and backups.")
            if not args.yes:
                raise InstallError("Use --yes to accept cleanup")
            for directory in (ctx.cache_dir, ctx.state_dir, ctx.config_dir):
                if directory.is_symlink():
                    raise InstallError("Cleanup directory is a symlink")
                if directory.exists():
                    shutil.rmtree(str(directory))
            return 0
        ids = args.install or []
        if args.fast:
            ids = [key for key in read_json(PROJECT / "src/config.json")["fast_install"]["items"]
                   if (ctx.desktop or not CATALOG[key].desktop) and ctx.target not in CATALOG[key].excluded_targets]
        if args.dry_run:
            print(json.dumps(engine.plan(ids or list(CATALOG)), ensure_ascii=False, indent=2))
            return 0
        if ids or args.configure:
            ctx.ensure_supported()
            if args.configure:
                if engine.detected(args.configure)["status"] not in ("installed", "partial"):
                    raise InstallError("Install the software before applying configuration")
                if not args.yes:
                    raise InstallError("Use --yes to accept configuration changes with backups")
            else:
                print(json.dumps(engine.plan(ids), ensure_ascii=False, indent=2))
                if not args.yes:
                    raise InstallError("Review the plan and use --yes to install")
            if ctx.uid != 0 and __import__("os").geteuid() != 0:
                subprocess.run(["sudo", "-v"], check=True)
            if args.configure:
                engine.configure(args.configure)
                return 0
            previous = signal.signal(signal.SIGINT, lambda *_: engine.runner.cancel.set())
            try:
                results = engine.execute(ids, args.method, allow_unverified=args.allow_unverified)
            finally:
                signal.signal(signal.SIGINT, previous)
            print(json.dumps(results, ensure_ascii=False, indent=2))
            if engine.runner.cancel.is_set():
                return 130
            return 0 if all(r["status"] in ("success", "installed") for r in results) else 2
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise InstallError("TUI requires a terminal; use --list, --doctor or --dry-run")
        from .ui import launch
        launch(engine, lite=args.lite)
        return 0
    except (InstallError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print("XdebX: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if engine:
            engine.runner.cancel.set()
        return 130
    finally:
        if engine:
            engine.close()


if __name__ == "__main__":
    sys.exit(main())
