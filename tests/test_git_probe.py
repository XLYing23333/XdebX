import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import probe_git


class GitProbeTests(unittest.TestCase):
    item={'id':'plugin','provider':'Gitee candidate','url':'https://gitee.com/example/plugin.git',
          'version':'a'*40,'member':'plugin.vim','sha256':hashlib.sha256(b'known').hexdigest()}
    def test_existing_pinned_commit_skips_network_fetch_and_checks_content(self):
        calls=[]
        def run(args,*_,**kwargs):
            calls.append(args)
            output=b'known' if 'show' in args else b'a'*40+b'\n' if 'rev-parse' in args else b''
            return {'exit_code':0,'error':None,'peak_bytes':100},output
        with tempfile.TemporaryDirectory() as tmp,patch.object(probe_git,'run',side_effect=run):
            result=probe_git.probe(self.item,Path(tmp),probe_git.TOTAL_LIMIT)
        self.assertEqual(result['status'],'passed');self.assertTrue(result['file_verified'])
        self.assertFalse(any('fetch' in args for args in calls))
        self.assertTrue(any('core.hooksPath=/dev/null' in args for args in calls))

    def test_matching_repository_name_does_not_accept_different_file(self):
        def run(args,*_,**kwargs):
            output=b'changed' if 'show' in args else b'a'*40+b'\n' if 'rev-parse' in args else b''
            return {'exit_code':0,'error':None,'peak_bytes':100},output
        with tempfile.TemporaryDirectory() as tmp,patch.object(probe_git,'run',side_effect=run):
            self.assertEqual(probe_git.probe(self.item,Path(tmp),probe_git.TOTAL_LIMIT)['status'],'failed')

    def test_timeout_terminates_only_owned_process_and_home_is_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            result,_=probe_git.run([sys.executable,'-c','import time;time.sleep(10)'],Path(tmp),timeout=.1)
            self.assertEqual(result['error'],'timeout')
            self.assertNotEqual(result['exit_code'],0)
            self.assertLess(result['elapsed_seconds'],3)

    def test_worker_removes_clone_and_private_cache_on_failure(self):
        import contextlib,io,json
        created=[]
        def probe(item,home,remaining):
            created.append(home.parent);(home/'repo').mkdir();(home/'cache').mkdir()
            return dict(item,status='failed',steps=[],peak_bytes=100)
        output=io.StringIO()
        with patch.object(probe_git,'probe',side_effect=probe),patch.object(probe_git.signal,'signal'),contextlib.redirect_stdout(output):
            probe_git.worker([self.item])
        self.assertFalse(created[0].exists())
        self.assertTrue(json.loads(output.getvalue().splitlines()[-1])['cleanup_verified'])
