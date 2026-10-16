import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_core import context
from xdebx.installer import Installer
from xdebx.runtime import InstallError


class MirrorTests(unittest.TestCase):
    pin='a'*40
    upstream='https://github.com/ohmyzsh/ohmyzsh.git'
    mirror='https://gitee.com/mirrors/oh-my-zsh.git'
    def fixture(self, root, mode):
        ctx=context(root);ctx.settings['mirror']=mode;engine=Installer(ctx)
        record={'upstream':self.upstream,'version':self.pin,'member':'oh-my-zsh.sh',
                'sha256':hashlib.sha256(b'known').hexdigest(),'urls':[self.mirror]}
        return engine.configurator, {'git_url':self.upstream,'version':self.pin}, {'resources':{'ohmyzsh':record}}

    def fake_run(self, calls, fail_github=False, corrupt=False):
        def run(args,**kwargs):
            calls.append((args,kwargs))
            if 'clone' in args:
                target=Path(args[-1]);target.mkdir();(target/'partial').write_text('temporary')
                if fail_github and args[-2]==self.upstream:raise InstallError('network timeout')
                (target/'oh-my-zsh.sh').write_bytes(b'changed' if corrupt else b'known')
            return 0,self.pin+'\n' if 'rev-parse' in args else ''
        return run

    def test_cn_clones_verified_gitee_first_and_checks_fixed_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            config,entry,table=self.fixture(Path(tmp),'CN');calls=[];target=Path(tmp)/'checkout'
            with patch('xdebx.configuration.read_json',return_value=table),patch.object(config.runner,'run',side_effect=self.fake_run(calls)):
                self.assertTrue(config.clone_pinned('ohmyzsh',entry,target))
            clone=calls[0][0];self.assertEqual(clone[-2],self.mirror)
            self.assertIn('--depth',clone);self.assertIn('core.hooksPath=/dev/null',clone)
            self.assertFalse(any('fetch' in c[0] for c in calls))
            marker=json.loads((target/'.xdebx-snapshot.json').read_text())
            self.assertEqual(marker['commit'],self.pin);self.assertEqual(marker['source'],self.mirror)

    def test_raw_github_failure_cleans_partial_and_tries_verified_gitee(self):
        with tempfile.TemporaryDirectory() as tmp:
            config,entry,table=self.fixture(Path(tmp),'Raw');calls=[];target=Path(tmp)/'checkout'
            with patch('xdebx.configuration.read_json',return_value=table),patch.object(config.runner,'run',side_effect=self.fake_run(calls,fail_github=True)):
                self.assertTrue(config.clone_pinned('ohmyzsh',entry,target))
                self.assertTrue(config.github_unavailable)
                self.assertEqual(config.git_candidates('ohmyzsh',entry),[self.mirror])
            self.assertEqual([c[0][-2] for c in calls if 'clone' in c[0]],[self.upstream,self.mirror])

    def test_stale_mirror_is_not_used_and_corrupt_mirror_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            config,entry,table=self.fixture(Path(tmp),'CN');config.github_unavailable=True
            table['resources']['ohmyzsh']['version']='b'*40
            with patch('xdebx.configuration.read_json',return_value=table):
                self.assertEqual(config.git_candidates('ohmyzsh',entry),[])
            table['resources']['ohmyzsh']['version']=self.pin;target=Path(tmp)/'checkout';calls=[]
            with patch('xdebx.configuration.read_json',return_value=table),patch.object(config.runner,'run',side_effect=self.fake_run(calls,corrupt=True)):
                self.assertFalse(config.clone_pinned('ohmyzsh',entry,target))
            self.assertFalse(target.exists());self.assertIn(self.mirror,config.git_source_failures)

    def test_explicit_snapshot_preference_never_starts_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            config,entry,_=self.fixture(Path(tmp),'CN');config.resources.prefer_snapshots=True
            with patch.object(config.runner,'run') as run:
                self.assertFalse(config.clone_pinned('ohmyzsh',entry,Path(tmp)/'checkout'))
                run.assert_not_called()
