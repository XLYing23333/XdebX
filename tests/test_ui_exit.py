"""Exit choices must never erase assets while an installation is active."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from xdebx.ui import Interface
from test_background import Screen


class ExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        ctx = SimpleNamespace(settings={'language': 'EN', 'mirror': 'Raw', 'music': False},
                              target='ubuntu24', desktop=False, cache_dir=base/'cache',
                              state_dir=base/'state', config_dir=base/'config', backups=base/'backups')
        for folder in (ctx.cache_dir, ctx.state_dir, ctx.config_dir, ctx.backups):
            folder.mkdir()
            (folder/'keep.txt').write_text('evidence')
        runner = SimpleNamespace(cancel=threading.Event())
        self.ui = Interface(SimpleNamespace(ctx=ctx, runner=runner, detected=lambda _: {'status': 'available'}))
        self.ui.words = json.loads((ROOT/'src/locales/EN.json').read_text())
        self.alive = [False]
        self.ui.worker = SimpleNamespace(is_alive=lambda: self.alive[0])
        self.ui.task_uses_cache = True

    def test_non_install_task_exits_without_cache_prompt_after_safe_stop(self):
        ui = self.ui
        self.alive[0] = True
        ui.task_uses_cache = False
        self.assertTrue(ui.handle(ord('q')))
        self.assertFalse(ui.quit_confirm)
        self.assertTrue(ui.engine.runner.cancel.is_set())
        self.alive[0] = False
        self.assertTrue(ui.finish_exit())
        self.assertTrue(ui.ctx.cache_dir.exists())

    def test_exit_choices_are_visible_in_small_terminal_in_both_languages(self):
        ui = self.ui
        ui.quit_confirm = True
        for language in ('EN', 'CN'):
            ui.ctx.settings['language'] = language
            screen = Screen(12, 38)
            ui.draw(screen)
            choices = ''.join(screen.cells[-2])
            for key in ('Y', 'N', 'Esc'):
                self.assertIn(key, choices)

    def test_busy_q_prompts_without_cancelling_and_escape_keeps_installing(self):
        ui = self.ui
        self.alive[0] = True
        ui.page = 'select'
        self.assertTrue(ui.handle(ord('Q')))
        self.assertEqual(ui.page, 'select')
        self.assertTrue(ui.quit_confirm)
        self.assertFalse(ui.engine.runner.cancel.is_set())
        self.assertIn('Installing', ui.events.get_nowait())
        self.assertTrue(ui.handle(27))
        self.assertFalse(ui.quit_confirm)
        self.assertFalse(ui.engine.runner.cancel.is_set())

    def test_clean_waits_for_worker_and_preserves_settings_history_backups(self):
        ui = self.ui
        self.alive[0] = True
        ui.handle(ord('q'))
        self.assertTrue(ui.handle(ord('Y')))
        self.assertTrue(ui.engine.runner.cancel.is_set())
        self.assertFalse(ui.finish_exit())
        self.assertTrue((ui.ctx.cache_dir/'keep.txt').exists())
        self.alive[0] = False
        self.assertTrue(ui.finish_exit())
        self.assertFalse(ui.ctx.cache_dir.exists())
        for directory in (ui.ctx.state_dir, ui.ctx.config_dir, ui.ctx.backups):
            self.assertTrue((directory/'keep.txt').exists())

    def test_idle_quit_never_prompts_even_after_a_completed_install(self):
        ui = self.ui
        for total in (0, 18):
            ui.progress_done = ui.progress_total = total
            self.assertFalse(ui.handle(ord('Q')))
            self.assertFalse(ui.quit_confirm)
            self.assertTrue((ui.ctx.cache_dir/'keep.txt').exists())
        self.assertTrue(ui.events.empty())

    def test_settings_cache_cleanup_requires_confirmation_and_keeps_other_data(self):
        ui = self.ui
        ui.page, ui.index = 'settings', 3
        self.assertTrue(ui.handle(10))
        self.assertTrue(ui.confirm_action)
        self.assertTrue(ui.ctx.cache_dir.exists())
        ui.handle(ord('n'))
        self.assertTrue(ui.ctx.cache_dir.exists())
        ui.handle(10)
        self.assertTrue(ui.handle(ord('y')))
        self.assertFalse(ui.ctx.cache_dir.exists())
        self.assertEqual(ui.page, 'settings')
        for directory in (ui.ctx.state_dir, ui.ctx.config_dir, ui.ctx.backups):
            self.assertTrue((directory/'keep.txt').exists())

    def test_settings_cannot_clear_cache_during_an_active_task(self):
        ui = self.ui
        ui.page, ui.index = 'settings', 3
        self.alive[0] = True
        self.assertTrue(ui.handle(10))
        self.assertFalse(ui.confirm_action)
        self.assertTrue(ui.ctx.cache_dir.exists())
        self.assertFalse(ui.engine.runner.cancel.is_set())

    def test_settings_cleanup_rechecks_busy_state_when_confirmed(self):
        ui = self.ui
        ui.page, ui.index = 'settings', 3
        ui.handle(10)
        self.alive[0] = True
        ui.handle(ord('y'))
        self.assertTrue(ui.ctx.cache_dir.exists())

    def test_busy_keep_waits_without_cleaning(self):
        ui = self.ui
        self.alive[0] = True
        ui.handle(ord('q'))
        self.assertTrue(ui.handle(ord('n')))
        self.assertTrue(ui.engine.runner.cancel.is_set())
        self.alive[0] = False
        self.assertTrue(ui.finish_exit())
        self.assertTrue(ui.ctx.cache_dir.exists())

    def test_symlink_cleanup_failure_stays_in_ui_and_keeps_target(self):
        ui = self.ui
        real = ui.ctx.cache_dir.with_name('real-cache')
        ui.ctx.cache_dir.rename(real)
        ui.ctx.cache_dir.symlink_to(real, target_is_directory=True)
        self.assertFalse(ui.clean_cache())
        self.assertFalse(ui.exit_requested)
        self.assertTrue((real/'keep.txt').exists())
        self.assertIn('symlink', ui.events.get_nowait())


if __name__ == '__main__':
    unittest.main()
