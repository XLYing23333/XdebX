"""Retry, ownership and configuration preservation are behavior contracts."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_core import context
from xdebx.installer import Installer
from xdebx.recovery import Journal, recover
from xdebx.runtime import Runner, Cancelled, InstallError, atomic_json


class RecoveryTests(unittest.TestCase):
    def test_journal_exists_before_strategy_and_retry_preserves_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(Path(tmp)); engine = Installer(ctx)
            dest = ctx.home / '.vimrc'; dest.write_bytes(b'user custom settings\n')
            state = {'binary': False, 'configured': False}
            def detect(key, configuration=True):
                if key == 'git': return {'status': 'installed', 'detail': 'git'}
                return {'status': 'installed' if state['configured'] or state['binary'] and not configuration else 'partial' if state['binary'] else 'available', 'detail': 'vim'}
            def install(*_):
                record = json.loads(next((ctx.state_dir/'runs').glob('*.json')).read_text())
                self.assertTrue(record['active']); self.assertEqual(record['current'], 'vim9')
                state['binary'] = True
            def configure(*_):
                engine.configurator.write(dest, 'template')
                state['configured'] = True
            with patch.object(engine, 'detected', side_effect=detect), patch.object(engine, 'strategy', side_effect=install), patch.object(engine.configurator, 'apply', side_effect=Cancelled('interrupt')):
                self.assertEqual(engine.execute(['vim9'])[-1]['status'], 'cancelled')
            original = dest.read_bytes()
            with patch.object(engine, 'detected', side_effect=detect), patch.object(engine, 'strategy') as strategy, patch.object(engine.configurator, 'apply', side_effect=configure):
                self.assertEqual(engine.execute(['vim9'])[-1]['status'], 'success')
                strategy.assert_not_called()
                self.assertEqual(dest.read_bytes(), original)

    def test_forced_exit_cleans_recorded_work_and_quarantines_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(Path(tmp)); runner = Runner(ctx)
            ctx.state_dir.mkdir(); journal = Journal(ctx, runner, ['nodejs22-nvm'], '123', ctx.state_dir/'log')
            journal.phase('nodejs22-nvm', 'official')
            work = journal.work/'unpack'; work.mkdir(parents=True); (work/'partial').write_text('bytes')
            target = ctx.home/'.nvm'; target.mkdir(); (target/'custom').write_text('user edit')
            atomic_json(target/'.xdebx-owner.json', {'run_id': '123', 'complete': False})
            journal.record['artifacts'] = [{'path': str(work), 'kind': 'temporary'}, {'path': str(target), 'kind': 'component'}]
            journal.save()
            recover(ctx, lambda _: None)
            self.assertFalse(work.exists()); self.assertFalse(target.exists())
            self.assertEqual(next(ctx.backups.glob('recovery-*/incomplete-component/custom')).read_text(), 'user edit')
            record = json.loads(journal.path.read_text()); self.assertEqual(record['results'][0]['status'], 'interrupted')
            self.assertFalse(record['active'])

    def test_unknown_component_and_symlink_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx=context(Path(tmp));ctx.state_dir.mkdir();runner=Runner(ctx)
            j=Journal(ctx,runner,[],'124',ctx.state_dir/'log')
            unknown=ctx.home/'.nvm';unknown.mkdir();(unknown/'custom').write_text('keep')
            outside=ctx.home/'outside';outside.mkdir();(outside/'data').write_text('keep')
            link=j.work/'link';link.parent.mkdir(parents=True);link.symlink_to(outside)
            j.record['artifacts']=[{'path':str(unknown),'kind':'component'},{'path':str(link),'kind':'temporary'}];j.save()
            recover(ctx,lambda _:None)
            self.assertEqual((unknown/'custom').read_text(),'keep');self.assertTrue(link.is_symlink());self.assertTrue((outside/'data').exists())

    def test_timeout_after_stdout_closes_is_bounded_and_does_not_cancel_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner=Runner(context(Path(tmp)));start=time.monotonic()
            with self.assertRaises(InstallError):
                runner.run([sys.executable,'-c','import os,time;os.close(1);os.close(2);time.sleep(5)'],timeout=.05)
            self.assertLess(time.monotonic()-start,2)
            self.assertFalse(runner.cancel.is_set())
            self.assertEqual(runner.run([sys.executable,'-c','print(42)'])[0],0)

    def test_transaction_timeout_waits_safely_without_cancelling_later_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner=Runner(context(Path(tmp)))
            with self.assertRaises(InstallError):
                runner.run([sys.executable,'-c','import time;time.sleep(.15)'],timeout=.03,transaction=True)
            self.assertFalse(runner.cancel.is_set())
            self.assertEqual(runner.run([sys.executable,'-c','print(42)'])[0],0)

    def test_partial_dependency_does_not_start_dependents(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=Installer(context(Path(tmp)))
            def detect(key,configuration=True):return {'status':'partial','detail':'missing'}
            with patch.object(engine,'detected',side_effect=detect),patch.object(engine,'strategy',side_effect=InstallError('broken')) as install:
                results=engine.execute(['claude-code'],configure=False)
                self.assertEqual([r['status'] for r in results],['failed','skipped'])
                self.assertTrue(all(c.args[0].id=='nodejs22-nvm' for c in install.call_args_list))

    def test_missing_nvm_default_is_repaired_using_existing_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx=context(Path(tmp));engine=Installer(ctx)
            target=ctx.home/'.nvm/versions/node/v22.1.0/bin';target.mkdir(parents=True)
            (target/'node').write_text('binary')
            with patch.object(engine.runner,'probe',return_value=(0,'v22.1.0')),patch.object(engine,'node') as install:
                engine.configurator.preserve_existing=True
                engine.repair_node_alias()
                install.assert_not_called()
                self.assertEqual((ctx.home/'.nvm/alias/default').read_text(),'v22.1.0\n')
                (ctx.home/'.nvm/alias/default').write_text('user-choice')
                engine.repair_node_alias()
                self.assertEqual((ctx.home/'.nvm/alias/default').read_text(),'user-choice')

    def test_crashed_download_part_cleans_only_recorded_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx=context(Path(tmp));ctx.state_dir.mkdir()
            j=Journal(ctx,Runner(ctx),['htop'],'125',ctx.state_dir/'log')
            cache=ctx.cache_dir/'downloads';cache.mkdir(parents=True)
            part=cache/'known.part';part.write_text('interrupted')
            unknown=cache/'unknown.part';unknown.write_text('preserve')
            j.artifact(part,'download-part')
            recover(ctx,lambda _:None)
            self.assertFalse(part.exists());self.assertEqual(unknown.read_text(),'preserve')

    def test_unexpected_single_item_error_does_not_abort_independent_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=Installer(context(Path(tmp)));ready=set()
            def detect(key,configuration=True):return {'status':'installed' if key in ready else 'available','detail':'version'}
            def strategy(package,*_):
                if package.id=='htop':raise RuntimeError('unexpected plugin error')
                ready.add(package.id)
            with patch.object(engine,'detected',side_effect=detect),patch.object(engine,'strategy',side_effect=strategy):
                results=engine.execute(['htop','curl'],configure=False)
            self.assertEqual([r['status'] for r in results],['failed','success'])
