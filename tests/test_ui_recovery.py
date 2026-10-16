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
                    self.assertLessEqual(abs((y+3)-(2+(rows-6)/2)),1)
                    body='\n'.join(''.join(row) for row in screen.cells)
                    for item in ui.menu:
                        buttons=[(y,text) for y,_,text,_ in screen.writes if ui.t(item) in text and str(ui.menu.index(item)+1) in text]
                        self.assertEqual(len(buttons),1)
                        self.assertLess(buttons[0][0],rows-4)
                    if cols>=90:self.assertTrue(any(ui.t('logs') in text for _,_,text,_ in screen.writes))

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
