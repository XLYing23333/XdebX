"""Pruning must never infer one distro/version's safety from another."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import prune_apt_cache


class PruningTests(unittest.TestCase):
    def test_only_exact_native_apt_acceptance_authorizes_retirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = root / 'data/pkgs/ubuntu20/chafa.deb'
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b'package fixture')
            entry = {'id': 'chafa', 'kind': 'deb', 'source_package': 'chafa', 'targets': ['ubuntu20'],
                     'filename': 'ubuntu20/chafa.deb', 'version': '1.2.1-1',
                     'sha256': hashlib.sha256(asset.read_bytes()).hexdigest()}
            directory = root / 'results'
            evidence = directory / 'ubuntu20-chafa-proof'
            evidence.mkdir(parents=True)
            case = {'target': 'ubuntu24', 'kind': 'strategy', 'method': 'official', 'software': 'chafa'}
            result = {'status': 'passed', 'package_versions': {'chafa': '1.2.1-1'}}
            def save():
                (evidence / 'case.json').write_text(json.dumps(case))
                (evidence / 'result.json').write_text(json.dumps(result))
            save()
            with patch.object(prune_apt_cache, 'ROOT', root):
                self.assertEqual(prune_apt_cache.candidates({'packages': [entry]}, directory, 'ubuntu20'), [])
                case['target'] = 'ubuntu20'
                result['status'] = 'failed'
                save()
                self.assertEqual(prune_apt_cache.candidates({'packages': [entry]}, directory, 'ubuntu20'), [])
                result.update(status='passed', package_versions={'chafa': '1.2.2-1'})
                save()
                self.assertEqual(prune_apt_cache.candidates({'packages': [entry]}, directory, 'ubuntu20'), [])
                result['package_versions']['chafa'] = entry['version']
                save()
                self.assertEqual(len(prune_apt_cache.candidates({'packages': [entry]}, directory, 'ubuntu20')), 1)
                # The same passing proof still cannot retire a shared fallback.
                entry['targets'].append('ubuntu24')
                self.assertEqual(prune_apt_cache.candidates({'packages': [entry]}, directory, 'ubuntu20'), [])
                self.assertTrue(asset.exists())

    def test_backup_path_cannot_escape_project_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / 'results/ubuntu20-chafa-proof'
            evidence.mkdir(parents=True)
            (evidence / 'case.json').write_text(json.dumps({'target': 'ubuntu20', 'kind': 'strategy', 'method': 'official', 'software': 'chafa'}))
            (evidence / 'result.json').write_text(json.dumps({'status': 'passed', 'package_versions': {'chafa': '1.2.1-1'}}))
            entry = {'id': 'chafa', 'kind': 'deb', 'source_package': 'chafa', 'targets': ['ubuntu20'],
                     'filename': 'ubuntu20/../../outside.deb', 'version': '1.2.1-1', 'sha256': '0' * 64}
            with patch.object(prune_apt_cache, 'ROOT', root), self.assertRaises(ValueError):
                prune_apt_cache.candidates({'packages': [entry]}, root / 'results', 'ubuntu20')


if __name__ == '__main__':
    unittest.main()
