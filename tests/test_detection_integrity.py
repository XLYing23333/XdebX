"""A working version command alone must not hide interrupted components."""
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from test_core import context
from xdebx.installer import Installer
from xdebx.runtime import Runner,Cancelled


class DetectionIntegrityTests(unittest.TestCase):
    def test_cancelled_install_does_not_disable_read_only_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner=Runner(context(Path(tmp)))
            runner.cancel.set()
            code,text=runner.probe([sys.executable,'-c','print(42)'])
            self.assertEqual((code,text.strip()),(0,'42'))
            self.assertTrue(runner.cancel.is_set())
            with self.assertRaises(Cancelled):runner.run([sys.executable,'-c','print(1)'])

    def test_binaries_without_configs_or_plugin_entry_points_are_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=Installer(context(Path(tmp)))
            versions={'vim9':'VIM - Vi IMproved 9.1','zsh-omz':'zsh 5.9','tmux-tmuxp':'tmux 3.5a','claude-code':'2.1 (Claude Code)'}
            for key,version in versions.items():
                with patch.object(engine,'command',return_value='tool'),patch.object(engine.runner,'probe',return_value=(0,version)):
                    self.assertEqual(engine.detected(key)['status'],'partial')
                    self.assertEqual(engine.detected(key,configuration=False)['status'],'installed')
            empty=engine.ctx.home/'.oh-my-zsh/custom/plugins/zsh-autosuggestions'
            empty.mkdir(parents=True)
            self.assertFalse(engine.configurator.snapshot_ready('zsh-autosuggestions',empty))

    def test_docker_cli_does_not_imply_a_usable_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=Installer(context(Path(tmp)))
            def probe(args):
                return (1,'') if 'info' in args else (0,'Docker version 29')
            with patch.object(engine,'command',return_value='docker'),patch.object(engine.runner,'probe',side_effect=probe):
                self.assertEqual(engine.detected('docker')['status'],'partial')

    def test_install_configures_before_final_component_verification(self):
        for completed in (False,True):
            with tempfile.TemporaryDirectory() as tmp:
                engine=Installer(context(Path(tmp)));state={'installed':False,'configured':False}
                def detected(key,configuration=True):
                    if key=='git':return {'status':'installed','detail':'version'}
                    status='available' if not state['installed'] else 'installed' if not configuration or state['configured'] else 'partial'
                    return {'status':status,'detail':'configuration missing' if status=='partial' else 'version'}
                def strategy(*args):state['installed']=True
                def configure(*args):state['configured']=completed
                with patch.object(engine,'detected',side_effect=detected),patch.object(engine,'strategy',side_effect=strategy) as install,patch.object(engine.configurator,'apply',side_effect=configure):
                    results=engine.execute(['vim9'])
                self.assertEqual(next(r for r in results if r['id']=='vim9')['status'],'success' if completed else 'partial')
                # Existing Git is skipped; missing configuration never retries the Vim binary.
                self.assertEqual(install.call_count,1)

    def test_configuration_cancel_is_not_recorded_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=Installer(context(Path(tmp)));installed=[False]
            def detected(key,configuration=True):return {'status':'installed' if installed[0] else 'available','detail':'version'}
            def strategy(*args):installed[0]=True
            with patch.object(engine,'detected',side_effect=detected),patch.object(engine,'strategy',side_effect=strategy),patch.object(engine.configurator,'apply',side_effect=Cancelled('stopped')):
                self.assertEqual(engine.execute(['btop'])[0]['status'],'cancelled')

    def test_invalid_claude_json_and_missing_nvm_default_are_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=Installer(context(Path(tmp)));h=engine.ctx.home
            (h/'.claude').mkdir();(h/'.claude/settings.json').write_text('{broken')
            self.assertTrue(engine.configurator.missing('claude-code'))
            (h/'.nvm').mkdir();(h/'.nvm/nvm.sh').write_text(':')
            with patch.object(engine,'command',return_value='node'),patch.object(engine.runner,'probe',return_value=(0,'v22.1.0')):
                self.assertEqual(engine.detected('nodejs22-nvm')['status'],'partial')


if __name__=='__main__':unittest.main()
