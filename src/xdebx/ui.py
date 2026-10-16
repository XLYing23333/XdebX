"""Responsive curses interface. Only the UI thread touches terminal state."""
import curses
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from collections import deque

from . import __version__
from .background import Background
from .catalog import CATALOG
from .runtime import PROJECT, InstallError, read_json


def clean_log_line(line):
    """Keep terminal escape sequences in raw evidence, not in the TUI."""
    line = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", str(line))
    line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
    return line.rstrip("\r").rsplit("\r", 1)[-1]


def log_severity(line):
    text = line.strip()
    if text.startswith("$ "):
        return "command"
    exit_code = re.match(r"^\[exit\s+([^\]]+)\]", text)
    if exit_code:
        return "success" if exit_code[1] == "0" else "error"
    if text.startswith("Ignoring invalid cached asset") or text.startswith("忽略"):
        return "warning"
    if re.search(r"(?i)(^E:|\b(error|failed|failure|fatal|traceback|exception|denied|mismatch|timeout)\b|not found|unable to|could not|timed out|失败|错误|异常|不匹配|超时|找不到|拒绝)", text):
        return "error"
    if re.search(r"(?i)(^W:|\b(warning|warn|deprecated|deprecation|skipped|cancelled|unverified|ignoring|fallback|retry|waiting|unsupported|missing)\b|not installed|upgrade required|跳过|取消|未验证|来源未确认|警告|等待|重试|不支持|待补齐|需升级)", text):
        return "warning"
    if re.search(r"(?i)(\b(success|successfully|configured|completed|done|installed|passed)\b|成功|配置完成|已安装|完成)", text):
        return "success"
    if re.search(r"(?i)(^(download|downloading|fetching|get:|hit:|reading|setting up|unpacking)|下载|安装计划|Installation plan)", text):
        return "info"
    return "output"


def width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def fit(text, columns):
    result, used = [], 0
    for char in str(text).replace("\t", "  "):
        size = width(char)
        if used + size > columns:
            break
        if ord(char) >= 32:
            result.append(char)
            used += size
    return "".join(result)


class Music:
    def __init__(self, ctx):
        self.ctx, self.process = ctx, None

    def start(self):
        self.stop()
        path = PROJECT / "data/music/電脳眠眠猫short.mp3"
        if not self.ctx.audio or not self.ctx.settings["music"] or not path.is_file():
            return
        for player, args in (("ffplay", ["-nodisp", "-loop", "0", "-loglevel", "quiet"]),
                             ("mpv", ["--no-video", "--really-quiet", "--loop-file=inf"]),
                             ("mpg123", ["-q", "--loop", "-1"])):
            if shutil.which(player):
                command = [player] + args + [str(path)]
                if os.geteuid() == 0 and self.ctx.uid != 0:
                    command = ["runuser", "-u", self.ctx.user, "--"] + command
                try:
                    self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    continue
                return

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process = None

    def playing(self):
        return bool(self.process and self.process.poll() is None)


class Interface:
    def __init__(self, engine, lite=False):
        self.engine, self.ctx = engine, engine.ctx
        self.lite = lite
        self.events, self.logs = queue.Queue(maxsize=4096), deque(maxlen=3000)
        self.engine.runner.emit = self.events.put
        self.engine.progress = lambda done, total: self.events.put(("progress", done, total))
        self.progress_done, self.progress_total = 0, 0
        self.menu = ["FastInstall", "InstallList", "Settings"]
        self.fast_pending = False
        self.scanner = None
        self.scan_stop = threading.Event()
        self.detector = None
        self.runs, self.history, self.backups = [], [], []
        self.confirm_prompt = "confirm"
        self.page, self.index, self.focus, self.offset = "home", 0, 0, 0
        self.selected, self.query, self.worker = set(), "", None
        self.task_uses_cache = False
        self.confirm_action = None
        self.quit_confirm = False
        self.exit_requested = False
        self.exit_cleanup = False
        self.music = Music(self.ctx)
        self.background = Background()
        self.colors = (0, 0, 0, 0)
        self.log_colors = {}
        self.colors_enabled = False
        self.selection_color = 0
        self.home_button_color = 0
        self.frame_color = curses.A_DIM
        self.footer_color = self.accent_color = self.version_color = 0
        self.searching = False
        self.words, self.language = {}, None
        self.fast_items = set(read_json(PROJECT / "src/config.json")["fast_install"]["items"])
        release = getattr(self.ctx, "os", {})
        fallback = {"ubuntu20": "Ubuntu 20.04", "ubuntu22": "Ubuntu 22.04", "ubuntu24": "Ubuntu 24.04",
                    "debian12": "Debian 12", "debian13": "Debian 13"}
        self.os_label = (release.get("NAME", release.get("ID", "Linux")) + " " + release.get("VERSION_ID", "")).strip() if release else fallback.get(self.ctx.target, "Linux")
        self.os_short = (release.get("ID", "Linux").capitalize() + " " + release.get("VERSION_ID", "")).strip() if release else self.os_label
        self.reload()

    def reload(self):
        if self.busy() or (self.scanner and self.scanner.is_alive()):
            return
        keys = [key for key, p in CATALOG.items()
                if (self.ctx.desktop or not p.desktop) and self.ctx.target not in p.excluded_targets]
        if not hasattr(self, "states"):
            self.states = {key: {"status": "checking"} for key in keys}
        def scan():
            discovery = self.engine.discovery() if hasattr(self.engine, "discovery") else self.engine
            self.detector = discovery
            try:
                states = {}
                for key in keys:
                    if self.scan_stop.is_set():
                        return
                    try:
                        states[key] = discovery.detected(key)
                    except Exception as exc:
                        states[key] = {"status": "failed", "detail": str(exc)}
                from .recovery import installation_running
                active = installation_running(self.ctx)
                runs = sorted((self.ctx.state_dir / "runs").glob("*.json"))
                history = []
                for path in reversed(runs):
                    record = read_json(path)
                    history.extend(dict(result, run_id=record.get("run_id", path.stem), log=record.get("log", ""))
                                   for result in record.get("results", []) if result.get("id") in CATALOG)
                    current = record.get("current")
                    if current and current not in {r["id"] for r in record.get("results", [])}:
                        history.append({"id": current, "status": "running" if active else "interrupted", "method": record.get("phase"),
                                        "run_id": record.get("run_id", path.stem), "log": record.get("log", "")})
                latest = {}
                for result in history:
                    latest.setdefault(result["id"], result)
                for key, result in latest.items():
                    if key in states and states[key]["status"] == "available" and result["status"] in ("failed", "cancelled", "interrupted"):
                        states[key] = {"status": result["status"], "detail": result.get("reason", "")}
                snapshot = ("snapshot", states, runs, history, sorted(self.ctx.backups.glob("*/record.json")))
                if hasattr(self.engine, "discovery"):
                    self.events.put(snapshot)
                else:
                    self.process_event(snapshot)
            except Exception as exc:
                self.events.put(self.t("error") + ": " + str(exc))
            finally:
                if discovery is not self.engine:
                    discovery.close()
        if hasattr(self.engine, "discovery"):
            self.scanner = threading.Thread(target=scan, daemon=True)
            self.scanner.start()
        else:
            scan()

    def t(self, key):
        return self.words.get(key, key)

    def busy(self):
        return bool(self.worker and self.worker.is_alive())

    def items(self):
        if self.page == "home":
            return self.menu
        if self.page == "settings":
            return ["language", "mirror", "music", "clean_cache", "DeleteXdebX"]
        if self.page == "backups":
            return self.backups
        if self.page == "history":
            return self.history
        keys = list(self.states)
        if self.page == "fast":
            keys = [k for k in keys if k in self.fast_items]
        return [k for k in keys if self.query.lower() in CATALOG[k].name.lower()]

    def start(self, callback, uses_cache=True):
        if self.busy():
            return
        if self.scanner and self.scanner.is_alive():
            self.events.put(self.t("checking"))
            return
        if os.geteuid() != 0:
            curses.def_prog_mode()
            curses.endwin()
            result = subprocess.run(["sudo", "-v"])
            curses.reset_prog_mode()
            if result.returncode:
                self.events.put(self.t("auth_failed"))
                return
        self.engine.runner.cancel.clear()
        self.task_uses_cache = uses_cache
        self.progress_done, self.progress_total = 0, 0
        def work():
            try:
                callback()
            except Exception as exc:
                self.events.put(self.t("error") + ": " + str(exc))
            finally:
                self.events.put("__DONE__")
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def draw_text(self, screen, y, x, text, color=0, limit=None):
        h, w = screen.getmaxyx()
        if y < 0 or y >= h or x >= w or x < 0 or (limit is not None and limit <= 0):
            return
        try:
            screen.addstr(y, x, fit(text, min(w - x - 1, w if limit is None else limit)), color)
        except curses.error:
            pass

    def draw_progress(self, screen):
        if not self.progress_total:
            return
        h, w = screen.getmaxyx()
        total = self.progress_total
        done = min(max(self.progress_done, 0), total)
        label = self.t("progress_count").format(done=done, total=total)
        label_x = w - 3 - width(label)
        bar_width = max(2, label_x - 5)
        inner = bar_width - 2
        filled = inner * done // total
        full, empty = ("#", "-") if self.lite else ("█", "░")
        self.draw_text(screen, h - 4, 2, " " * (w - 4))
        self.draw_text(screen, h - 4, 3, "[" + full * filled + empty * (inner - filled) + "]", self.accent_color, bar_width)
        self.draw_text(screen, h - 4, label_x, label, self.footer_color)

    def log_attribute(self, severity):
        if self.lite or not self.colors_enabled:
            return 0
        return self.log_colors.get(severity, 0) | (curses.A_BOLD if severity in ("error", "warning", "command") else 0)

    def draw(self, screen):
        if self.language != self.ctx.settings["language"]:
            self.language = self.ctx.settings["language"]
            self.words = read_json(PROJECT / "src/locales" / (self.language + ".json"))
        screen.erase()
        h, w = screen.getmaxyx()
        cyan, purple, green, yellow = self.colors
        if h < 12 or w < 38:
            self.draw_text(screen, 0, 0, self.t("small"))
            screen.refresh()
            return
        if not self.lite:
            self.background.draw(screen)
            try:
                screen.attron(self.frame_color)
                screen.box()
                screen.attroff(self.frame_color)
            except curses.error:
                pass
        version = "XdebX v" + __version__ + " · " + self.os_label
        if width(version) > w - 6:
            version = "XdebX v" + __version__ + " · " + self.os_short
        version = fit(version, w - 6)
        self.draw_text(screen, 1, (w - width(version)) // 2, version, self.version_color)
        split = w // 2 if w >= 90 else w - 2
        show_logs = w >= 90 or self.focus == 1
        home_visible = self.page == "home"
        subtitle_attr = 0 if self.lite else cyan | curses.A_BOLD
        if w >= 90 or self.focus == 0:
            subtitle = fit(self.t(self.page), split - 6)
            self.draw_text(screen, 2, (split - width(subtitle)) // 2, subtitle, subtitle_attr, split - 6)
        if w >= 90 and not self.lite:
            for y in range(2, h - 4):
                self.draw_text(screen, y, split, "│", self.frame_color)
        items = self.items()
        self.index = min(self.index, max(0, len(items) - 1))
        visible = h - 7
        start = max(0, self.index - visible // 2)
        if w >= 90 or self.focus == 0:
            home_width = split - 6
            home_x = 3
            home_step = 1 if h < 13 else 2
            home_height = 3 + home_step * 2
            home_y = max(2, 2 + (h - 6 - home_height) // 2)
            for row, item in enumerate(items[start:start + visible], 3):
                actual = start + row - 3
                active = actual == self.index
                attr = 0
                text = str(item)
                if self.page == "home":
                    text = "{}  {}".format(actual + 1, self.t(item))
                elif self.page == "settings":
                    if item in ("clean_cache", "DeleteXdebX"):
                        text = self.t(item)
                    else:
                        value = self.ctx.settings[item]
                        if self.lite and item == "music":
                            value = False
                        text = self.t(item) + "  [ " + (self.t("on") if value is True else self.t("off") if value is False else str(value)) + " ]"
                elif self.page == "backups":
                    record = read_json(item)
                    text = record["destination"]
                elif self.page == "history":
                    text = "{} · {} · {}".format(CATALOG[item["id"]].name, self.t(item["status"]), item.get("method") or "")
                elif item in CATALOG:
                    status = self.states[item]["status"]
                    checkbox = {"installed": "[OK]", "partial": "[~]", "upgrade-required": "[^]",
                                "unsupported": "[!]", "failed": "[!]", "interrupted": "[~]",
                                "cancelled": "[~]", "checking": "[?]"}.get(status, "[x]" if item in self.selected else "[ ]")
                    text = "- {} {} · {}".format(checkbox, CATALOG[item].name, self.t(status))
                    attr |= green if status == "installed" else yellow if status in ("partial", "upgrade-required", "cancelled", "interrupted") else purple if status == "failed" else 0
                text = (("> " if self.lite else "› ") if active else "  ") + text
                if active and not self.lite:
                    attr = self.selection_color | curses.A_BOLD
                    if not self.selection_color:
                        attr |= curses.A_UNDERLINE
                    span = home_width - 1 if home_visible else split - 6
                    text = fit(text, span)
                    text += " " * max(0, span - width(text)) + "‹"
                if home_visible:
                    button_y = home_y + 2 + actual * home_step
                    if not self.lite:
                        if not active:
                            attr = self.home_button_color
                        label = fit("{}  {}".format(actual + 1, self.t(item)), home_width - 4)
                        inner = home_width - 2
                        padding = (inner - width(label)) // 2
                        text = ("›" if active else " ") + " " * padding + label
                        text += " " * (inner - padding - width(label)) + ("‹" if active else " ")
                        self.draw_text(screen, button_y, home_x, text, attr, home_width)
                    else:
                        self.draw_text(screen, button_y, home_x, text, 0, home_width)
                else:
                    self.draw_text(screen, row, 3, text, attr, split - 5)
        if show_logs:
            x = split + 2 if w >= 90 else 3
            subtitle = fit(self.t("logs"), w - x - 2)
            title_x = split + (w - split - width(subtitle)) // 2 if w >= 90 else (w - width(subtitle)) // 2
            self.draw_text(screen, 2, title_x, subtitle, subtitle_attr)
            legend = [(name, self.t("log_" + name)) for name in ("success", "warning", "error", "command")]
            legend_width = sum(width(label) for _, label in legend) + 2 * (len(legend) - 1)
            legend_x = w - 3 - legend_width
            show_legend = self.colors_enabled and not self.lite and legend_x >= x
            if show_legend:
                self.draw_text(screen, h - 5, x, " " * (w - x - 2))
                for severity, label in legend:
                    self.draw_text(screen, h - 5, legend_x, label, self.log_attribute(severity))
                    legend_x += width(label) + 2
            log_visible = visible - 1 if show_legend else visible
            lines = list(self.logs)
            end = max(0, len(lines) - self.offset)
            for row, line in enumerate(lines[max(0, end - log_visible):end], 3):
                text = clean_log_line(line)
                self.draw_text(screen, row, x, text, self.log_attribute(log_severity(text)), w - x - 2)
        bars = "".join("▁▂▃▄▅▆▇"[int((math.sin(time.monotonic() * 4 + i) + 1) * 3)] for i in range(9)) if not self.lite and self.music.playing() else ""
        if not self.lite:
            self.draw_text(screen, h - 4, 2, "─" * (w - 4), self.frame_color)
        self.draw_progress(screen)
        searchable = self.page in ("fast", "select", "list")
        right_x = w - width(bars) - 3
        if bars:
            self.draw_text(screen, h - 2, right_x, bars, self.accent_color)
        context = "search_keys" if self.searching else "list_keys" if self.page == "list" else "history_keys" if self.page == "history" else "home_extra_keys" if self.page == "home" else "extra_keys"
        footer = self.t(context)
        if searchable:
            prefix = "/ " + self.t("search") + ": "
            available = max(0, right_x - width(prefix) - 7)
            query = self.query
            while width(query) > available:
                query = query[1:]
            search = prefix + query + ("▏" if self.searching else "")
            self.draw_text(screen, h - 2, 3, search, self.accent_color | (curses.A_BOLD if self.searching else 0), right_x - 5)
            context_x = 3 + width(search) + 3
            self.draw_text(screen, h - 2, context_x, footer, self.footer_color, right_x - context_x - 2)
        else:
            self.draw_text(screen, h - 2, 3, footer, self.footer_color, right_x - 5)
        key_limit = w - 6
        keys = self.t(("keys_home" if self.page == "home" else "keys") if key_limit >= 99 else "keys_compact")
        if width(keys) > key_limit:
            keys = self.t("keys_tiny")
        self.draw_text(screen, h - 3, 3, self.t(self.confirm_prompt) if self.confirm_action else keys, yellow if self.confirm_action else self.footer_color, key_limit)
        if self.quit_confirm:
            for y in (h - 4, h - 3, h - 2):
                self.draw_text(screen, y, 2, " " * (w - 4))
            self.draw_text(screen, h - 4, 3, self.t("quit_installing") if self.busy() else self.t("quit_title"), yellow, key_limit)
            self.draw_text(screen, h - 3, 3, self.t("quit_cache"), yellow, key_limit)
            self.draw_text(screen, h - 2, 3, self.t("quit_choices"), yellow, key_limit)
        screen.refresh()

    def back(self):
        if self.page != "home":
            previous = self.page
            self.page = "list" if previous in ("backups", "history") else "home"
            self.index = {"fast": 0, "select": 1, "list": 2, "settings": 4}.get(previous, 0) if self.page == "home" else 0
            self.focus, self.offset, self.query, self.searching = 0, 0, "", False
            self.selected.clear()

    def start_music(self):
        if self.lite:
            return
        self.music.start()
        if self.ctx.settings["music"]:
            self.background.start_entrance()

    def clean_cache(self):
        if self.busy():
            self.events.put(self.t("cache_busy"))
            return False
        try:
            directory = self.ctx.cache_dir
            if directory.is_symlink():
                raise InstallError("Cleanup directory is a symlink")
            if directory.exists():
                shutil.rmtree(str(directory))
        except (OSError, InstallError) as exc:
            self.events.put(self.t("error") + ": " + str(exc))
            return False
        self.events.put(self.t("cache_cleaned"))
        return True

    def finish_exit(self):
        """Clean only after the installer has stopped using its cache."""
        if not self.exit_requested or self.busy():
            return False
        if self.exit_cleanup and not self.clean_cache():
            self.exit_requested = False
            return False
        return True

    def process_event(self, item):
        if isinstance(item, tuple) and item[0] == "snapshot":
            _, self.states, self.runs, self.history, self.backups = item
            self.selected.intersection_update(k for k, state in self.states.items()
                                              if state["status"] not in ("installed", "unsupported", "checking"))
            if self.page == "fast" and self.fast_pending:
                self.selected = {k for k in self.items() if self.states[k]["status"] not in ("installed", "unsupported", "checking")}
                self.fast_pending = False
        elif isinstance(item, tuple) and item[0] == "progress":
            _, self.progress_done, self.progress_total = item
        elif item == "__DONE__":
            # The worker may still be leaving its finally block; defer refresh to the UI loop.
            self.refresh_pending = True
        else:
            self.logs.append(item)

    def handle(self, key):
        if key in (ord("Q"), ord("C"), ord("B"), ord("H"), ord("R")):
            key = ord(chr(key).lower())
        if self.exit_requested:
            return not self.finish_exit()
        if self.quit_confirm:
            if key in (27, ord("q")):
                self.quit_confirm = False
            elif key in (ord("y"), ord("Y"), ord("n"), ord("N")):
                self.quit_confirm = False
                self.exit_requested = True
                self.exit_cleanup = key in (ord("y"), ord("Y"))
                if self.busy():
                    self.engine.runner.cancel.set()
                    self.events.put(self.t("cancel_wait"))
                return not self.finish_exit()
            return True
        if self.confirm_action:
            if key in (ord("y"), ord("Y")):
                action, self.confirm_action = self.confirm_action, None
                self.confirm_prompt = "confirm"
                action()
            elif key in (27, ord("q"), ord("n"), ord("N")):
                self.confirm_action = None
                self.confirm_prompt = "confirm"
            return True
        if key == ord("q"):
            if not self.busy():
                return False
            if not self.task_uses_cache:
                self.exit_requested, self.exit_cleanup = True, False
                self.engine.runner.cancel.set()
                self.events.put(self.t("cancel_wait"))
                return True
            self.quit_confirm = True
            self.events.put(self.t("quit_installing"))
            return True
        if key == 27 and self.page != "home":
            self.back()
            return True
        if key == 27:
            return True
        if key in (ord("x"), ord("X")) and self.busy():
            self.engine.runner.cancel.set()
            self.events.put(self.t("cancel_wait"))
            return True
        items = self.items()
        self.index = min(self.index, max(0, len(items) - 1))
        current = items[self.index] if items else None
        if key == 9:
            self.focus = 1 - self.focus
        elif key == curses.KEY_PPAGE:
            self.offset = min(len(self.logs), self.offset + 8)
        elif key == curses.KEY_NPAGE:
            self.offset = max(0, self.offset - 8)
        elif key in (curses.KEY_UP, ord("k")):
            self.index = max(0, self.index - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.index = min(len(items) - 1, self.index + 1)
        elif key == ord("/") and self.page in ("select", "list", "fast"):
            self.query = ""
            self.index = 0
            self.searching = True
        elif key == ord("b") and self.page == "list" and not self.busy():
            self.page, self.index = "backups", 0
        elif key == ord("h") and self.page == "list" and not self.busy():
            self.page, self.index = "history", 0
        elif key == ord("r") and self.page == "history" and current and not self.busy():
            self.confirm_action = lambda: self.start(lambda: self.engine.execute([current["id"]]))
        elif key == ord("c") and self.page == "list" and current and not self.busy():
            self.confirm_action = lambda: self.start(lambda: self.engine.configure(current))
        elif key == ord("r") and not self.busy():
            self.reload()
        elif key == ord(" ") and self.page in ("fast", "select", "list") and current:
            if self.states[current]["status"] not in ("installed", "unsupported", "checking"):
                if current in self.selected:
                    self.selected.remove(current)
                else:
                    self.selected.add(current)
        elif key in (10, 13, curses.KEY_ENTER):
            if self.page == "home":
                self.page = {"FastInstall": "fast", "InstallList": "list", "Settings": "settings"}[current]
                self.index = 0
                self.fast_pending = self.page == "fast" and bool(self.scanner and self.scanner.is_alive())
                self.selected = {k for k in self.items() if self.states[k]["status"] not in ("installed", "unsupported", "checking")} if self.page == "fast" else set()
            elif self.page == "settings":
                if current == "DeleteXdebX":
                    if self.busy() or (self.scanner and self.scanner.is_alive()):
                        self.events.put(self.t("cache_busy"))
                        return True
                    def clean():
                        if self.busy() or (self.scanner and self.scanner.is_alive()):
                            self.events.put(self.t("cache_busy"))
                            return
                        try:
                            for directory in (self.ctx.cache_dir, self.ctx.config_dir, self.ctx.state_dir):
                                if directory.is_symlink():
                                    raise InstallError("Cleanup directory is a symlink")
                            for directory in (self.ctx.cache_dir, self.ctx.config_dir, self.ctx.state_dir):
                                if directory.exists():
                                    shutil.rmtree(str(directory))
                            self.events.put(self.t("cleaned"))
                        except (InstallError, OSError) as exc:
                            self.events.put(self.t("error") + ": " + str(exc))
                    def second():
                        self.confirm_prompt = "delete_confirm_final"
                        self.confirm_action = clean
                    self.events.put(self.t("cleanup_info"))
                    self.confirm_prompt = "delete_confirm_first"
                    self.confirm_action = second
                    return True
                if current == "clean_cache":
                    if self.busy():
                        self.events.put(self.t("cache_busy"))
                    else:
                        self.events.put(self.t("cache_confirm"))
                        self.confirm_action = self.clean_cache
                    return True
                if current == "mirror" and self.busy():
                    self.events.put(self.t("mirror_busy"))
                    return True
                if current == "music":
                    if self.lite:
                        self.events.put(self.t("lite_music"))
                        return True
                    self.ctx.settings[current] = not self.ctx.settings[current]
                    self.start_music()
                elif current == "language":
                    self.ctx.settings[current] = "CN" if self.ctx.settings[current] == "EN" else "EN"
                else:
                    self.ctx.settings[current] = "CN" if self.ctx.settings[current] == "Raw" else "Raw"
                    if not self.busy():
                        self.engine.close()
                        self.engine.apt_ready, self.engine.apt_directory, self.engine.apt_options = False, None, []
                self.ctx.save_settings()
            elif self.page in ("fast", "select", "list") and self.selected and not self.busy():
                ids = [k for k in self.items() if k in self.selected]
                self.events.put(self.t("plan") + ": " + ", ".join(ids))
                if "ctypora" in ids:
                    self.events.put(self.t("unverified"))
                self.confirm_action = lambda: self.start(lambda: self.engine.execute(ids, allow_unverified="ctypora" in ids))
            elif self.page == "backups" and current and not self.busy():
                self.confirm_action = lambda: self.start(lambda: self.engine.configurator.restore(current), uses_cache=False)
            elif self.page == "history" and current:
                from pathlib import Path
                path = Path(current["log"])
                if path.is_file():
                    self.logs.extend(path.read_text(errors="replace").splitlines()[-1000:])
                    self.offset, self.focus = 0, 1
        return True

    def run(self, screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        screen.timeout(250 if self.lite else 50)
        if self.lite:
            # wrapper may initialize colors itself. Leave pair zero at the
            # terminal default instead of its implicit white-on-black pair.
            try:
                curses.use_default_colors()
            except curses.error:
                pass
        if not self.lite and curses.has_colors() and "NO_COLOR" not in os.environ:
            curses.start_color()
            background = -1
            try:
                curses.use_default_colors()
            except curses.error:
                background = curses.COLOR_BLACK
            palette = (153, 203, 108, 180) if curses.COLORS >= 256 else (curses.COLOR_WHITE, curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW)
            for index, color in enumerate(palette, 1):
                curses.init_pair(index, color, background)
            self.colors = tuple(curses.color_pair(i) for i in range(1, 5))
            self.colors_enabled = True
            self.log_colors = dict(command=self.colors[0], error=self.colors[1], warning=self.colors[3], success=self.colors[2], info=self.colors[0], output=0)
            if curses.COLOR_PAIRS > 27:
                log_palette = (153, 203, 180, 108, 110, 250) if curses.COLORS >= 256 else (curses.COLOR_CYAN, curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_GREEN, curses.COLOR_BLUE, curses.COLOR_WHITE)
                for pair, (severity, color) in enumerate(zip(("command", "error", "warning", "success", "info", "output"), log_palette), 22):
                    curses.init_pair(pair, color, background)
                    self.log_colors[severity] = curses.color_pair(pair)
            self.frame_color = self.colors[0] | curses.A_DIM
            self.accent_color, self.version_color = self.colors[0], self.colors[3]
            if curses.COLOR_PAIRS > 21:
                decoration = (240, 250, 153, 250) if curses.COLORS >= 256 else (curses.COLOR_WHITE, curses.COLOR_WHITE, curses.COLOR_CYAN, curses.COLOR_WHITE)
                for pair, color in enumerate(decoration, 18):
                    curses.init_pair(pair, color, background)
                self.frame_color, self.footer_color, self.accent_color, self.version_color = (curses.color_pair(pair) for pair in range(18, 22))
            self.background.configure_colors(background)
            if curses.COLOR_PAIRS > 17:
                foreground, selected_background = (255, 24) if curses.COLORS >= 256 else (curses.COLOR_WHITE, curses.COLOR_BLUE)
                curses.init_pair(17, foreground, selected_background)
                self.selection_color = curses.color_pair(17)
            else:
                self.selection_color = self.colors[0]
        if self.colors_enabled and curses.COLOR_PAIRS > 28:
            # Opaque cards keep animated lettering and terminal wallpaper out of labels.
            foreground, card_background = (252, 234) if curses.COLORS >= 256 else (curses.COLOR_WHITE, curses.COLOR_BLACK)
            curses.init_pair(28, foreground, card_background)
            self.home_button_color = curses.color_pair(28)
        self.words = read_json(PROJECT / "src/locales" / (self.ctx.settings["language"] + ".json"))
        if not self.lite:
            self.music.start()
            self.background.start_entrance()
        self.searching = False
        try:
            while True:
                for _ in range(200):
                    try:
                        item = self.events.get_nowait()
                    except queue.Empty:
                        break
                    self.process_event(item)
                if getattr(self, "refresh_pending", False) and not self.busy():
                    self.refresh_pending = False
                    self.reload()
                if self.finish_exit():
                    break
                self.draw(screen)
                key = screen.getch()
                if key in (27, ord("q"), ord("Q")):
                    if not self.handle(key):
                        break
                    continue
                if self.searching and not self.quit_confirm and not self.exit_requested and not self.confirm_action:
                    if key in (10, 13):
                        self.searching = False
                    elif key in (curses.KEY_BACKSPACE, 127):
                        self.query = self.query[:-1]
                        self.index = 0
                    elif 32 <= key < 127:
                        self.query += chr(key)
                        self.index = 0
                    continue
                if not self.handle(key):
                    break
        finally:
            self.music.stop()
            self.scan_stop.set()
            shutdown = getattr(getattr(self.detector, "runner", None), "shutdown", None)
            if shutdown is not None and self.detector is not self.engine:
                shutdown.set()
            if self.scanner:
                self.scanner.join(timeout=1)
            if self.busy():
                self.engine.runner.cancel.set()
                # Do not abandon an active package-manager transaction.
                curses.endwin()
                while self.busy():
                    # Keep draining stdout events during safe shutdown; a full log queue
                    # must not stall a package transaction or hide its wait notices.
                    for _ in range(200):
                        try:
                            item = self.events.get_nowait()
                        except queue.Empty:
                            break
                        if isinstance(item, str) and item != "__DONE__":
                            print(clean_log_line(item), flush=True)
                    self.worker.join(timeout=0.2)


def launch(engine, lite=False):
    # Python 3.8 has no set_escdelay; ncurses reads this before wrapper setup.
    os.environ["ESCDELAY"] = "25"
    interface = Interface(engine, lite=lite)
    curses.wrapper(interface.run)
