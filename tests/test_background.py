"""Backdrop geometry, terminal fallbacks and foreground compositing checks."""
import curses
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from xdebx.background import Background, COLOR_COUNT, LEVELS, PALETTE, BRAKE_AT, STOP_AT, FADE_DURATION, assemble_frame, fade_frame, fade_opacity, motion_pose, render_frame, rotation
from xdebx.ui import Interface, width


class Screen:
    def __init__(self, rows=32, columns=100):
        self.rows, self.columns = rows, columns
        self.erase()

    def getmaxyx(self):
        return self.rows, self.columns

    def erase(self):
        self.cells = [[" "] * self.columns for _ in range(self.rows)]
        self.writes = []

    def addstr(self, y, x, text, attribute=0):
        if not (0 <= y < self.rows and 0 <= x < self.columns and x + width(text) <= self.columns):
            raise AssertionError("Out-of-bounds terminal write")
        self.writes.append((y, x, text, attribute))
        for char in text:
            self.cells[y][x] = char
            x += width(char)

    def box(self):
        pass

    def attron(self, *_):
        pass

    def attroff(self, *_):
        pass

    def refresh(self):
        pass


class BackgroundTests(unittest.TestCase):
    def test_parked_logo_fades_smoothly_to_zero_in_five_seconds(self):
        self.assertEqual(FADE_DURATION, 5)
        self.assertEqual(fade_opacity(STOP_AT), 1)
        self.assertAlmostEqual(fade_opacity(STOP_AT + 2.5), 0.5)
        self.assertEqual(fade_opacity(STOP_AT + 5), 0)
        samples = [fade_opacity(STOP_AT + n / 10) for n in range(51)]
        self.assertTrue(all(a >= b for a, b in zip(samples, samples[1:])))
        self.assertLess(1 - fade_opacity(STOP_AT + 0.01), 0.0001)
        self.assertLess(fade_opacity(STOP_AT + 4.99), 0.0001)

    def test_fade_keeps_pixels_stationary_and_does_not_bring_them_back(self):
        spans = render_frame(100, 25, STOP_AT)
        pixels = lambda frame: {(y, x + i) for y, x, text, _ in frame for i in range(len(text))}
        high, low = fade_frame(spans, 0.8), fade_frame(spans, 0.3)
        self.assertTrue(pixels(low) < pixels(high) < pixels(spans))
        self.assertEqual(fade_frame(spans, 0.3), low)
        self.assertEqual(fade_frame(spans, 1), spans)
        self.assertEqual(fade_frame(spans, 0), [])

    def test_faded_logo_stops_rendering_and_can_restart(self):
        with patch('xdebx.background.time.monotonic', return_value=0):
            background = Background()
        screen = Screen()
        with patch('xdebx.background.render_frame', wraps=render_frame) as render:
            with patch('xdebx.background.time.monotonic', return_value=STOP_AT + FADE_DURATION):
                background.draw(screen)
                self.assertEqual(screen.writes, [])
                render.assert_not_called()
                background.start_entrance()
                background.draw(screen)
                render.assert_called_once()
                self.assertTrue(screen.writes)

    def test_logo_brakes_smoothly_and_parks_centered_before_twenty_seconds(self):
        step = 0.001
        speed = lambda t: (motion_pose(t + step)[0] - motion_pose(t)[0]) / step
        self.assertAlmostEqual(speed(3), speed(9), places=5)
        self.assertAlmostEqual(speed(BRAKE_AT - step), speed(BRAKE_AT), places=3)
        speeds = [speed(t) for t in (10, 12, 14, 16, 18)]
        self.assertTrue(all(a > b > 0 for a, b in zip(speeds, speeds[1:])))
        self.assertLess(speed(STOP_AT - step), 0.00001)
        self.assertLess(STOP_AT, 20)
        yaw, pitch, roll, dx, dy = motion_pose(STOP_AT)
        self.assertEqual((pitch, roll, dx, dy), (0, 0, 0, 0))
        self.assertAlmostEqual(yaw % (2 * math.pi), 0)
        point = (11, -2, 0.85)
        for actual, expected in zip(rotation(STOP_AT)(point), point):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(render_frame(100, 25, STOP_AT), render_frame(100, 25, 100))

    def test_parked_frame_is_cached_resizes_and_replays_entrance(self):
        with patch("xdebx.background.time.monotonic", return_value=0):
            background = Background()
            background.start_entrance()
        screen = Screen()
        with patch("xdebx.background.render_frame", wraps=render_frame) as render:
            for t in (19, 20, 21):
                with patch("xdebx.background.time.monotonic", return_value=t):
                    background.draw(screen)
            self.assertEqual(render.call_count, 1)
            screen.columns = 80
            screen.erase()
            with patch("xdebx.background.time.monotonic", return_value=22):
                background.draw(screen)
                self.assertEqual(render.call_count, 2)
                background.start_entrance()
                background.draw(screen)
                self.assertEqual(render.call_count, 3)
                self.assertEqual(render.call_args.args[-1], 0)

    def test_fragment_entrance_is_bounded_and_finishes_in_one_second(self):
        spans = render_frame(100, 25, 0)
        for progress in (0, 0.25, 0.5, 0.75):
            fragments = assemble_frame(spans, 100, 25, progress)
            self.assertTrue(fragments)
            self.assertNotEqual(fragments, spans)
            for y, x, text, material in fragments:
                self.assertTrue(0 <= y < 25)
                self.assertTrue(0 <= x < 100)
                self.assertLessEqual(x + len(text), 100)
        self.assertEqual(assemble_frame(spans, 100, 25, 1), spans)
        with patch("xdebx.background.time.monotonic", return_value=0):
            background = Background()
            background.start_entrance()
        with patch("xdebx.background.assemble_frame", wraps=assemble_frame) as assemble:
            with patch("xdebx.background.time.monotonic", return_value=0.5):
                background.draw(Screen())
            self.assertEqual(assemble.call_count, 1)
            with patch("xdebx.background.time.monotonic", return_value=1):
                background.draw(Screen())
            self.assertEqual(assemble.call_count, 1)

    def test_frames_remain_bounded_and_animate(self):
        for columns, rows in ((1, 1), (34, 5), (76, 17), (160, 48)):
            for t in (0, 3, 8, 15):
                spans = render_frame(columns, rows, t)
                for y, x, text, hue in spans:
                    self.assertTrue(0 <= y < rows)
                    self.assertTrue(0 <= x < columns)
                    self.assertLessEqual(x + width(text), columns)
                    self.assertTrue(0 <= hue < COLOR_COUNT)
                    self.assertTrue(set(text) <= set("░▒▓█"))
        first = render_frame(100, 25, 0)
        self.assertTrue(first)
        self.assertNotEqual(first, render_frame(100, 25, 5))
        self.assertEqual(render_frame(0, 0, 0), [])

    def test_cache_refreshes_on_tick_and_resize(self):
        screen = Screen()
        with patch("xdebx.background.time.monotonic", return_value=0):
            background = Background()
        with patch("xdebx.background.render_frame", wraps=render_frame) as render:
            with patch("xdebx.background.time.monotonic", return_value=1):
                background.draw(screen)
                background.draw(screen)
                self.assertEqual(render.call_count, 1)
                screen.columns = 80
                screen.erase()
                background.draw(screen)
                self.assertEqual(render.call_count, 2)
            with patch("xdebx.background.time.monotonic", return_value=1.2):
                background.draw(screen)
                self.assertEqual(render.call_count, 3)

    def test_large_and_small_terminal(self):
        background = Background()
        for rows, columns in ((11, 100), (32, 37)):
            screen = Screen(rows, columns)
            background.draw(screen)
            self.assertEqual(screen.writes, [])
        screen = Screen(100, 300)
        with patch("xdebx.background.render_frame", wraps=render_frame) as render:
            background.draw(screen)
            self.assertEqual(render.call_args.args[:2], (160, 48))
        for y, x, text, _ in screen.writes:
            self.assertTrue(3 <= y < screen.rows - 4)
            self.assertTrue(2 <= x and x + width(text) <= screen.columns - 2)

    def test_color_capabilities_and_no_color(self):
        for colors, pairs in ((256, 256), (8, 8)):
            background = Background()
            with patch("xdebx.background.curses.has_colors", return_value=True), \
                    patch("xdebx.background.curses.COLORS", colors, create=True), \
                    patch("xdebx.background.curses.COLOR_PAIRS", pairs, create=True), \
                    patch("xdebx.background.curses.init_pair") as init, \
                    patch("xdebx.background.curses.color_pair", side_effect=lambda pair: pair << 8), \
                    patch.dict("os.environ", {}, clear=True):
                background.configure_colors()
            self.assertEqual(init.call_count, COLOR_COUNT if colors == 256 else 3)
            for call in init.call_args_list:
                pair, color, _ = call.args
                self.assertTrue(5 <= pair < pairs)
                self.assertTrue(0 <= color < colors)
        for colored, environment in ((False, {}), (True, {"NO_COLOR": "1"})):
            background = Background()
            with patch("xdebx.background.curses.has_colors", return_value=colored), \
                    patch("xdebx.background.curses.init_pair") as init, \
                    patch.dict("os.environ", environment, clear=True):
                background.configure_colors()
                init.assert_not_called()
            self.assertEqual(background.attributes, [curses.A_DIM] * COLOR_COUNT)

    def test_silver_faces_and_blue_sides_keep_lighting(self):
        # Silver dominates the readable front; perspective can expose a few
        # sides even at zero yaw. Rotation retains both materials and lighting.
        front = render_frame(160, 48, 0)
        silver_pixels = sum(len(text) for _, _, text, material in front if material < LEVELS)
        self.assertGreater(silver_pixels / sum(len(text) for _, _, text, _ in front), 0.5)
        materials = {material for _, _, _, material in render_frame(160, 48, 3)}
        self.assertTrue(any(material < LEVELS for material in materials))
        self.assertTrue(any(material >= LEVELS for material in materials))
        self.assertGreater(len(materials), 2)
        self.assertTrue(all(232 <= color <= 255 for color in PALETTE[:LEVELS]))
        self.assertEqual(PALETTE[LEVELS:], (17, 18, 25, 26, 33, 39))

    def test_optional_curses_errors_do_not_abort_drawing(self):
        background = Background()
        screen = Screen()
        with patch.object(screen, "addstr", side_effect=curses.error):
            background.draw(screen)

    def test_monochrome_ui_runs_without_color_or_cursor_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = SimpleNamespace(settings={"language": "EN", "mirror": "Raw", "music": False},
                                  target="ubuntu24", desktop=False, audio=False,
                                  state_dir=Path(tmp), backups=Path(tmp))
            engine = SimpleNamespace(ctx=ctx, runner=SimpleNamespace(),
                                     detected=lambda _: {"status": "available"})
            ui, screen = Interface(engine), Screen()
            screen.keypad = lambda _: None
            screen.timeout = lambda _: None
            quit_keys = iter((ord("q"), ord("n")))
            screen.getch = lambda: next(quit_keys)
            with patch("xdebx.ui.curses.curs_set", side_effect=curses.error), \
                    patch("xdebx.ui.curses.has_colors", return_value=False), \
                    patch("xdebx.ui.curses.color_pair", side_effect=AssertionError("No color support")):
                ui.run(screen)
            self.assertIn("Fast Install", "\n".join("".join(row) for row in screen.cells))
            background_text = "".join(text for _, _, text, attr in screen.writes if attr == curses.A_DIM and not set(text) & set("│─"))
            self.assertTrue(background_text)
            self.assertTrue(set(background_text) <= set("·░▒"))

    def test_foreground_remains_readable_across_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = SimpleNamespace(settings={"language": "EN", "mirror": "Raw", "music": False},
                                  target="ubuntu24", desktop=False, state_dir=Path(tmp), backups=Path(tmp))
            engine = SimpleNamespace(ctx=ctx, runner=SimpleNamespace(),
                                     detected=lambda _: {"status": "available"})
            ui = Interface(engine)
            ui.logs.extend(["Installing package...", "Fallback: verified local package"])
            with patch("xdebx.background.time.monotonic", return_value=ui.background.started):
                for columns, focus in ((100, 0), (70, 0), (70, 1)):
                    for page in ("home", "fast", "select", "list", "settings", "history", "backups"):
                        ui.page, ui.focus, ui.index = page, focus, 0
                        screen = Screen(columns=columns)
                        ui.draw(screen)
                        self.assertTrue(any(attr == curses.A_DIM for _, _, _, attr in screen.writes))
                        # Every foreground write survives exactly as drawn,
                        # including spaces that mask the animated backdrop.
                        foreground = [(y, x, text) for y, x, text, attr in screen.writes if attr != curses.A_DIM]
                        for y, x, text in foreground:
                            self.assertEqual("".join(screen.cells[y][x:x + len(text)]), text)
                ui.page, ui.focus = "home", 0
                ui.confirm_action = lambda: None
                screen = Screen()
                ui.draw(screen)
                self.assertIn(ui.t("confirm"), "".join(screen.cells[-3]))


if __name__ == "__main__":
    unittest.main()
