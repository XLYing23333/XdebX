"""Footer layout, non-color status and looping music regression checks."""
import curses
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from xdebx import __version__
from xdebx.ui import Interface, Music
from test_background import Screen


def interface(root):
    ctx = SimpleNamespace(settings={"language": "EN", "mirror": "Raw", "music": False},
                          target="ubuntu24", desktop=False, audio=False,
                          state_dir=root, backups=root)
    engine = SimpleNamespace(ctx=ctx, runner=SimpleNamespace(),
                             detected=lambda _: {"status": "available"})
    return Interface(engine)


class InterfaceTests(unittest.TestCase):
    def test_progress_events_update_counts_without_entering_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = interface(Path(tmp))
            ui.engine.progress(0, 4)
            ui.engine.progress(2, 4)
            while not ui.events.empty():
                ui.process_event(ui.events.get_nowait())
            self.assertEqual((ui.progress_done, ui.progress_total), (2, 4))
            self.assertEqual(list(ui.logs), [])

    def test_progress_bar_and_right_count_fit_both_languages_and_lite(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = interface(Path(tmp))
            for language in ('EN', 'CN'):
                ui.ctx.settings['language'] = language
                for lite in (False, True):
                    ui.lite = lite
                    for columns in (38, 70, 120):
                        for done in (0, 2, 4):
                            ui.progress_done, ui.progress_total = done, 4
                            screen = Screen(12, columns)
                            ui.draw(screen)
                            line = ''.join(screen.cells[-4]).replace(' ', '')
                            label = ui.t('progress_count').format(done=done, total=4)
                            self.assertIn(label, line)
                            self.assertTrue(line.rstrip().endswith(label))
                            self.assertIn('[', line)
                            self.assertIn(']', line)
                            if lite:
                                self.assertNotIn('█', line)
                                self.assertNotIn('░', line)
                            if done == 4:
                                self.assertNotIn('-' if lite else '░', line)

    def test_lite_disables_decorations_and_preserves_music_preference(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = interface(Path(tmp))
            ui = Interface(ui.engine, lite=True)
            ui.ctx.settings["music"] = True
            ui.ctx.save_settings = Mock()
            screen = Screen()
            screen.keypad = lambda _: None
            screen.timeout = lambda _: None
            quit_keys = iter((ord("q"), ord("n")))
            screen.getch = lambda: next(quit_keys)
            with patch("xdebx.ui.curses.curs_set"), \
                    patch("xdebx.ui.curses.has_colors", side_effect=AssertionError("No color initialization")), \
                    patch.object(ui.music, "start", side_effect=AssertionError("No playback")), \
                    patch.object(ui.background, "draw", side_effect=AssertionError("No animation")), \
                    patch.object(ui.background, "start_entrance", side_effect=AssertionError("No entrance")), \
                    patch.object(screen, "box", side_effect=AssertionError("No border")):
                ui.run(screen)
                ui.page, ui.index = "settings", 2
                ui.draw(screen)
                ui.handle(10)
            self.assertTrue(ui.ctx.settings["music"])
            ui.ctx.save_settings.assert_not_called()
            self.assertTrue(all(attr == 0 for _, _, _, attr in screen.writes))
            self.assertIn("> Music  [ OFF ]", "".join(screen.cells[5]))
            self.assertFalse(any(set(text) & set("│─░▒▓█▁▂▃▄▅▆▇") for _, _, text, _ in screen.writes))

    def test_cli_lite_reaches_tui(self):
        from xdebx.__main__ import main
        with patch("xdebx.__main__.Context") as context, patch("xdebx.__main__.Installer") as installer, \
                patch("xdebx.__main__.sys.stdin.isatty", return_value=True), \
                patch("xdebx.__main__.sys.stdout.isatty", return_value=True), patch("xdebx.ui.launch") as launch:
            self.assertEqual(main(["--lite"]), 0)
            launch.assert_called_once_with(installer.return_value, lite=True)

    def test_footer_and_selection_in_both_languages_and_small_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = interface(Path(tmp))
            for language in ("EN", "CN"):
                ui.ctx.settings["language"] = language
                for rows, columns in ((32, 120), (20, 70), (12, 38)):
                    screen = Screen(rows, columns)
                    ui.draw(screen)
                    self.assertNotIn("AMD64", "\n".join(screen.cells[1]))
                    self.assertIn("v" + __version__, "".join(screen.cells[1]))
                    self.assertNotIn("v" + __version__, "".join(screen.cells[-2]))
                    self.assertNotIn("Cursor &", "".join(screen.cells[-2]))
                    frame = "\n".join("".join(row) for row in screen.cells)
                    self.assertIn("›", frame)
                    self.assertIn("‹", frame)
                    foreground = [attr for _, _, text, attr in screen.writes if "›" in text]
                    self.assertTrue(foreground)
                    self.assertTrue(all(not (attr & curses.A_REVERSE) for attr in foreground))
                    self.assertIn("Q", "".join(screen.cells[-3]))

    def test_installation_markers_and_locked_items_in_monochrome(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = interface(Path(tmp))
            ids = list(ui.states)[:6]
            statuses = ("available", "installed", "partial", "upgrade-required", "unsupported", "available")
            markers = ("- [ ]", "- [OK]", "- [~]", "- [^]", "- [!]", "- [x]")
            ui.states = {key: {"status": status} for key, status in zip(ids, statuses)}
            ui.selected = {ids[-1]}
            ui.page = "select"
            screen = Screen()
            ui.draw(screen)
            for row, marker in enumerate(markers, 3):
                self.assertIn(marker, "".join(screen.cells[row]))
            for index in (1, 4):
                ui.index = index
                ui.handle(ord(" "))
                self.assertNotIn(ids[index], ui.selected)
            ui.index = 0
            ui.handle(ord(" "))
            self.assertIn(ids[0], ui.selected)

    def test_search_stays_in_footer_and_resets_scroll(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = interface(Path(tmp))
            ui.page, ui.index = "select", 12
            ui.handle(ord("/"))
            self.assertEqual(ui.index, 0)
            ui.query = "vim"
            screen = Screen()
            ui.draw(screen)
            self.assertIn("Search: vim", "".join(screen.cells[-2]))
            self.assertNotIn("vim", "".join(screen.cells[1]))
            self.assertIn("Enter: keep filter", "".join(screen.cells[-2]))
            screen.keypad = lambda _: None
            screen.timeout = lambda _: None
            keys = iter((ord("/"), ord("v"), ord("i"), ord("m"), 27, ord("q"), ord("n")))
            screen.getch = lambda: next(keys)
            with patch("xdebx.ui.curses.curs_set"), patch("xdebx.ui.curses.has_colors", return_value=False):
                ui.run(screen)
            self.assertFalse(ui.searching)
            self.assertEqual(ui.query, "")
            self.assertEqual(ui.page, "home")

    def test_equal_panes_no_ready_and_right_audio_with_os(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = interface(Path(tmp))
            ui.os_label = "Ubuntu 24.04"
            with patch.object(ui.music, "playing", return_value=True):
                screen = Screen(32, 121)
                ui.draw(screen)
            dividers = [x for _, x, text, _ in screen.writes if text == "│"]
            self.assertTrue(dividers)
            self.assertEqual(set(dividers), {60})
            self.assertEqual(dividers[0] - 1, screen.columns - dividers[0] - 2)
            self.assertNotIn("READY", "".join(screen.cells[-4]))
            audio = [(x, text) for y, x, text, _ in screen.writes if y == screen.rows - 2 and text and set(text) <= set("▁▂▃▄▅▆▇")]
            self.assertTrue(audio)
            self.assertGreater(audio[0][0], screen.columns // 2)
            self.assertNotIn("♪", "".join(screen.cells[-4]) + "".join(screen.cells[-3]))
            self.assertIn("─", "".join(screen.cells[-4]))
            self.assertIn("v" + __version__ + " · Ubuntu 24.04", "".join(screen.cells[1]))
            self.assertNotIn("Cursor &", "".join(screen.cells[-2]))

    def test_back_is_immediate_and_does_not_cancel_active_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = interface(Path(tmp))
            for key in (27,):
                for page, parent in (("fast", "home"), ("select", "home"), ("settings", "home"),
                                     ("list", "home"), ("history", "list"), ("backups", "list")):
                    ui.page, ui.focus, ui.searching, ui.query = page, 1, True, "vim"
                    with patch.object(ui, "items", side_effect=AssertionError("Back should not inspect software")), \
                            patch.object(ui.engine, "detected", side_effect=AssertionError("No rescan on back")), \
                            patch.object(ui, "busy", return_value=True):
                        self.assertTrue(ui.handle(key))
                    self.assertEqual(ui.page, parent)
                    self.assertEqual(ui.focus, 0)
                    self.assertFalse(ui.searching)
                    self.assertEqual(ui.query, "")

    def test_music_starts_before_entrance_and_toggle_replays_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = interface(Path(tmp))
            ui.page, ui.index = "settings", 2
            ui.ctx.save_settings = Mock()
            calls = []
            with patch.object(ui.music, "start", side_effect=lambda: calls.append("music")), \
                    patch.object(ui.background, "start_entrance", side_effect=lambda: calls.append("entrance")):
                ui.handle(10)
                self.assertEqual(calls, ["music", "entrance"])
                ui.handle(10)
                self.assertEqual(calls, ["music", "entrance", "music"])


class MusicTests(unittest.TestCase):
    def test_looping_players_and_stopping_music(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            song = root / "data/music/電脳眠眠猫short.mp3"
            song.parent.mkdir(parents=True)
            song.write_bytes(b"fixture")
            ctx = SimpleNamespace(audio=True, settings={"music": True}, uid=os.getuid())
            loop_options = {"ffplay": ("-loop", "0"), "mpv": ("--loop-file=inf",), "mpg123": ("--loop", "-1")}
            for player, options in loop_options.items():
                process = Mock()
                process.poll.return_value = None
                music = Music(ctx)
                with patch("xdebx.ui.PROJECT", root), \
                        patch("xdebx.ui.shutil.which", side_effect=lambda name: name if name == player else None), \
                        patch("xdebx.ui.subprocess.Popen", return_value=process) as launch:
                    music.start()
                    command = launch.call_args.args[0]
                    self.assertEqual(command[0], player)
                    for option in options:
                        self.assertIn(option, command)
                    self.assertTrue(music.playing())
                    music.stop()
                    process.terminate.assert_called_once()
                    process.wait.assert_called_once_with(2)
                    self.assertFalse(music.playing())

    def test_player_launch_failure_tries_next_player(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            song = root / "data/music/電脳眠眠猫short.mp3"
            song.parent.mkdir(parents=True)
            song.touch()
            ctx = SimpleNamespace(audio=True, settings={"music": True}, uid=os.getuid())
            with patch("xdebx.ui.PROJECT", root), patch("xdebx.ui.shutil.which", side_effect=lambda name: name), \
                    patch("xdebx.ui.subprocess.Popen", side_effect=[OSError("unavailable"), Mock()]) as launch:
                music = Music(ctx)
                music.start()
            self.assertEqual(launch.call_count, 2)
            self.assertEqual(launch.call_args.args[0][0], "mpv")


if __name__ == "__main__":
    unittest.main()
