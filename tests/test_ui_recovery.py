import curses
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from test_ui import interface
from test_background import Screen


class UIRecoveryTests(unittest.TestCase):
    def test_home_centered_in_left_panel_with_three_buttons(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui=interface(Path(tmp))
            for rows,cols in [(24,120),(12,38),(30,90)]:
                for lang in ('EN','CN'):
                    ui.ctx.settings['language']=lang
                    screen=Screen(rows,cols);ui.draw(screen)
                    self.assertEqual(ui.menu,['FastInstall','InstallList','Settings'])
                    split=cols//2 if cols>=90 else cols-2
                    writes=[(y,x,text) for y,x,text,attr in screen.writes if text==ui.t('home')]
                    self.assertEqual(len(writes),1)
                    y,x,title=writes[0]
                    from xdebx.ui import width
                    self.assertLessEqual(abs(2*x+width(title)-split),1)
                    self.assertEqual(y, 2)
                    left_attr = next(attr for _, _, text, attr in screen.writes if text == title)
                    self.assertTrue(left_attr & curses.A_BOLD)
                    if cols >= 90:
                        right_y, _, _, right_attr = next(write for write in screen.writes if write[2] == ui.t('logs'))
                        self.assertEqual(right_y, y)
                        self.assertEqual(right_attr, left_attr)
                    body='\n'.join(''.join(row) for row in screen.cells)
                    for item in ui.menu:
                        buttons=[(y,text) for y,_,text,_ in screen.writes if ui.t(item) in text and str(ui.menu.index(item)+1) in text]
                        self.assertEqual(len(buttons),1)
                        self.assertLess(buttons[0][0],rows-4)
                    if cols>=90:self.assertTrue(any(ui.t('logs') in text for _,_,text,_ in screen.writes))

    def test_home_single_line_buttons_mask_backdrop_and_keep_equal_widths(self):
        from xdebx.ui import width
        with tempfile.TemporaryDirectory() as tmp:
            ui = interface(Path(tmp))
            ui.home_button_color, ui.selection_color = 256, 512
            ui.background.draw = lambda screen: [screen.addstr(y, 1, "#" * (screen.columns - 2), curses.A_DIM)
                                                 for y in range(2, screen.rows - 4)]
            for language in ('EN', 'CN'):
                ui.ctx.settings['language'] = language
                for rows, columns in ((20, 120), (32, 100), (24, 38)):
                    for selected in range(3):
                        ui.index = selected
                        screen = Screen(rows, columns)
                        ui.draw(screen)
                        buttons = [(y, x, text, attr) for y, x, text, attr in screen.writes
                                   if any(ui.t(item) in text for item in ui.menu)]
                        self.assertEqual(len(buttons), 3)
                        self.assertEqual(len({width(text) for _, _, text, _ in buttons}), 1)
                        self.assertFalse(any(text.startswith(('┌', '└')) for _, _, text, _ in screen.writes))
                        for index, (y, x, text, attr) in enumerate(buttons):
                            self.assertLess(y, rows - 4)
                            split = columns // 2 if columns >= 90 else columns - 2
                            self.assertEqual(x, 3)
                            self.assertEqual(width(text), split - 6)
                            label = "{}  {}".format(index + 1, ui.t(ui.menu[index]))
                            before = text[:text.index(label)]
                            self.assertLessEqual(abs(2 * (x + width(before)) + width(label) - split), 1)
                            self.assertEqual(attr, (ui.selection_color | curses.A_BOLD) if index == selected else ui.home_button_color)
                            self.assertNotIn('#', text)
                            cursor = x
                            for char in text:
                                self.assertEqual(screen.cells[y][cursor], char)
                                cursor += width(char)
                            if index:
                                self.assertEqual(y - buttons[index - 1][0], 2)

    def test_list_can_select_partial_but_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui=interface(Path(tmp));ui.page='list';key=list(ui.states)[0]
            for status,selectable in [('partial',True),('failed',True),('interrupted',True),('installed',False),('unsupported',False)]:
                ui.states={key:{'status':status}};ui.selected=set();ui.handle(ord(' '))
                self.assertEqual(key in ui.selected,selectable)

    def test_cancel_current_task_keeps_ui_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui=interface(Path(tmp));ui.worker=SimpleNamespace(is_alive=lambda:True)
            ui.engine.runner.cancel=threading.Event()
            self.assertTrue(ui.handle(ord('x')));self.assertTrue(ui.engine.runner.cancel.is_set())
            self.assertFalse(ui.exit_requested)

    def test_slow_discovery_does_not_block_ui_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui=interface(Path(tmp));gate=threading.Event();started=threading.Event()
            detector=SimpleNamespace(detected=lambda key:(started.set(),gate.wait(2),{'status':'available'})[-1],close=lambda:None)
            ui.engine.discovery=lambda:detector
            begin=time.monotonic();ui.reload();self.assertLess(time.monotonic()-begin,.2)
            self.assertTrue(started.wait(1));ui.handle(curses.KEY_DOWN)
            self.assertEqual(ui.index,1);gate.set();ui.scanner.join(2)

    def test_delete_requires_two_confirmations_and_cancellation_keeps_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui=interface(Path(tmp));root=Path(tmp)
            ui.ctx.cache_dir=root/'cache';ui.ctx.config_dir=root/'config';ui.ctx.state_dir=root/'state'
            for path in [ui.ctx.cache_dir,ui.ctx.config_dir,ui.ctx.state_dir]:path.mkdir();(path/'keep').write_text('data')
            ui.page='settings';ui.index=4
            ui.handle(10);ui.handle(ord('y'));self.assertTrue(ui.ctx.state_dir.exists())
            ui.handle(ord('n'));self.assertTrue(ui.ctx.state_dir.exists())
            ui.handle(10);ui.handle(ord('y'));ui.handle(ord('y'))
            self.assertFalse(ui.ctx.state_dir.exists())
