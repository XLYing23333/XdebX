import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import slim_cache
from xdebx.runtime import sha256


class SlimCacheTests(unittest.TestCase):
    def test_slimming_keeps_download_metadata_and_matching_small_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'data/pkgs';(folder/'provenance').mkdir(parents=True);(root/'docs').mkdir()
            small=folder/'small.tar.gz';small.write_bytes(b'package')
            large=folder/'large.tar.gz';large.write_bytes(b'x'*(slim_cache.LIMIT+1))
            source=folder/'corresponding-source/small.dsc';source.parent.mkdir();source.write_bytes(b'source')
            entries=[{'id':p.stem,'filename':p.name,'size_bytes':p.stat().st_size,'sha256':sha256(p),'url':'https://example.invalid/'+p.name} for p in (small,large)]
            (folder/'manifest.json').write_text(json.dumps({'packages':entries}))
            (folder/'provenance/corresponding-source.json').write_text(json.dumps({'packages':{small.name:{'files':[{'filename':'corresponding-source/small.dsc'}]}}}))
            dry=slim_cache.slim(root);self.assertTrue(large.exists());self.assertGreater(dry['before_bytes'],dry['after_bytes'])
            slim_cache.slim(root,True)
            self.assertTrue(small.exists());self.assertTrue(source.exists());self.assertFalse(large.exists())
            result=json.loads((folder/'manifest.json').read_text())['packages'][1]
            self.assertEqual(result['bundle'],'on-demand');self.assertEqual(result['sha256'],entries[1]['sha256']);self.assertEqual(result['url'],entries[1]['url'])
            self.assertNotEqual(result.get('download_enabled'),False)
            self.assertNotIn(large.name,(folder/'SHA256SUMS').read_text())
