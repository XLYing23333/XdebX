"""Log presentation preserves raw evidence and works without terminal colors."""
import curses
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from test_ui import interface
from test_background import Screen
from xdebx.ui import clean_log_line, log_severity


class LogColorTests(unittest.TestCase):
    def test_palette_initializes_for_eight_and_256_color_terminals(self):
        with tempfile.TemporaryDirectory() as tmp:
            for colors, pairs in ((8, 8), (256, 64)):
                ui = interface(Path(tmp))
                ui.logs.append('Error: failed')
                screen = Screen(20, 120)
                screen.keypad = lambda _: None
                screen.timeout = lambda _: None
                keys = iter((ord('q'), ord('n')))
                screen.getch = lambda: next(keys)
                with patch('xdebx.ui.curses.curs_set'), patch('xdebx.ui.curses.has_colors', return_value=True), \
                        patch('xdebx.ui.curses.start_color'), patch('xdebx.ui.curses.use_default_colors'), \
                        patch('xdebx.ui.curses.COLORS', colors, create=True), \
                        patch('xdebx.ui.curses.COLOR_PAIRS', pairs, create=True), \
                        patch('xdebx.ui.curses.init_pair') as init, \
                        patch('xdebx.ui.curses.color_pair', side_effect=lambda pair: pair << 8), \
                        patch.dict('os.environ', {}, clear=True):
                    ui.run(screen)
                self.assertTrue(ui.colors_enabled)
                self.assertNotEqual(ui.log_colors['error'], ui.log_colors['success'])
                for call in init.call_args_list:
                    pair, foreground, _ = call.args
                    self.assertLess(pair, pairs)
                    self.assertLess(foreground, colors)

    def test_english_and_chinese_log_levels_and_exit_status(self):
        examples = {
            'command': ['$ apt install liberror-dev', '$ echo failed'],
            'error': ['[exit 100]', 'E: Unable to locate package', 'Traceback (most recent call last):', '安装失败: 缺少依赖', 'SHA-256 mismatch'],
            'warning': ['W: repository warning', 'tool: skipped', 'Stop requested; waiting for active APT transaction', '忽略校验失败的缓存 x', 'Ignoring invalid cached asset x: mismatch', 'tool: components missing'],
            'success': ['[exit 0]', 'tool: success', 'Configured: /home/test/.vimrc', 'vim9: 成功'],
            'info': ['Download: https://example.invalid/file', 'Setting up curl', '下载: https://example.invalid/file'],
            'output': ['make[1]: entering directory', '0 errors'],
        }
        for severity, lines in examples.items():
            for line in lines:
                with self.subTest(line=line):
                    self.assertEqual(log_severity(line), severity)

    def test_ansi_and_carriage_returns_are_cleaned_only_for_display(self):
        raw = '\x1b[31mError: failed\x1b[0m\r'
        self.assertEqual(clean_log_line(raw), 'Error: failed')
        self.assertEqual(clean_log_line('10%\r20%\r'), '20%')
        self.assertEqual(clean_log_line('\x1b]8;;https://example.invalid\x07link\x1b]8;;\x07'), 'link')
        with tempfile.TemporaryDirectory() as tmp:
            ui = interface(Path(tmp))
            ui.logs.append(raw)
            ui.draw(Screen())
            self.assertEqual(ui.logs[0], raw)

    def test_subtitles_center_and_legend_stays_at_log_panel_bottom_right(self):
        from xdebx.ui import width
        with tempfile.TemporaryDirectory() as tmp:
            ui = interface(Path(tmp))
            ui.colors_enabled = True
            ui.logs.extend('entry ' + str(i) for i in range(100))
            for language in ('EN', 'CN'):
                ui.ctx.settings['language'] = language
                for rows, columns in ((12, 90), (24, 120), (20, 70)):
                    ui.focus = 1 if columns < 90 else 0
                    ui.page = 'settings'
                    screen = Screen(rows, columns)
                    ui.draw(screen)
                    split = columns // 2
                    title = next((y, x, text) for y, x, text, _ in screen.writes if text == ui.t('logs'))
                    center_twice = split + columns if columns >= 90 else columns
                    self.assertEqual(title[0], 2)
                    self.assertLessEqual(abs(2 * title[1] + width(title[2]) - center_twice), 1)
                    if columns >= 90:
                        left = next((x, text) for y, x, text, _ in screen.writes if y == 2 and text == ui.t('settings'))
                        self.assertLessEqual(abs(2 * left[0] + width(left[1]) - split), 1)
                    legend = [(y, x, text) for y, x, text, _ in screen.writes
                              if text in [ui.t('log_' + key) for key in ('success', 'warning', 'error', 'command')]]
                    self.assertEqual(len(legend), 4)
                    self.assertTrue(all(y == rows - 5 for y, _, _ in legend))
                    self.assertEqual(legend[-1][1] + width(legend[-1][2]), columns - 3)
                    self.assertTrue(all(y < rows - 5 for y, _, text, _ in screen.writes if text.startswith('entry ')))

    def test_log_panel_uses_colors_and_keeps_lite_and_monochrome_plain(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = interface(Path(tmp))
            ui.logs.extend(['$ echo test', '[exit 0]', 'warning: deprecated', '安装失败'])
            ui.log_colors = {'command': 256, 'success': 512, 'warning': 768, 'error': 1024}
            ui.colors_enabled = True
            for lite, colored in ((False, True), (True, True), (False, False)):
                ui.lite, ui.colors_enabled = lite, colored
                screen = Screen(20, 120)
                ui.draw(screen)
                writes = {text: attr for _, x, text, attr in screen.writes if x == 62}
                for line in ui.logs:
                    self.assertIn(line, writes)
                    expected = ui.log_colors[log_severity(line)]
                    if colored and not lite:
                        self.assertTrue(writes[line] & expected)
                        if log_severity(line) != 'success':
                            self.assertTrue(writes[line] & curses.A_BOLD)
                    else:
                        self.assertEqual(writes[line], 0)


if __name__ == '__main__':
    unittest.main()
