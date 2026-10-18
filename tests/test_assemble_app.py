"""Source assembly tests never import the application."""
import hashlib
import json
from pathlib import Path
import tempfile
import shutil
import unittest
import zipfile
from unittest.mock import patch
from tooling.assemble_app import assemble, safe_source

ROOT = Path(__file__).resolve().parents[1]


class AssemblyPathTests(unittest.TestCase):
    def test_parent_traversal_rejected(self):
        with self.assertRaises(ValueError):
            safe_source(ROOT, '../outside.py')

    def test_symlink_source_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'target').write_text('fixture')
            (root / 'link').symlink_to(root / 'target')
            with self.assertRaises(ValueError):
                safe_source(root, 'link')

    def test_existing_output_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, 'new directory'):
                assemble(ROOT, Path(temp))

    def test_symlink_output_parent_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'real').mkdir()
            (root / 'link').symlink_to(root / 'real', target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'Symlink output'):
                assemble(ROOT, root / 'link' / 'out')


class AssemblyHelperTests(unittest.TestCase):
    def fixture(self, root, collision=False, bad_hash=False):
        def write(name, raw):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        raw = b'# synthetic fixture, no application execution\n'
        digest = hashlib.sha256(raw).hexdigest()
        write('recovered/core/example.py', raw)
        write('recovered/entrypoint/main.py', raw)
        write('recovered/entrypoint/comparison.json', json.dumps({'source_sha256': digest}).encode())
        helper = 'maintained/core/example.py' if collision else 'maintained/utils/helper.py'
        write(helper, raw)
        write('recovered/recovery-manifest.json', json.dumps({'entries': [{
            'module': 'core.example', 'source': 'recovered/core/example.py',
            'source_sha256': digest, 'status': 'exact_compiled_structure',
            'report': 'recovered/reports/core.example.json'}]}).encode())
        write('maintained/patch-manifest.json', json.dumps({
            'baseline_commit': '1' * 40, 'modules': [], 'new_helpers': [{
                'module': 'utils.helper', 'maintained': helper,
                'maintained_sha256': '0' * 64 if bad_hash else digest}]}).encode())

    def test_helper_collision_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'repo'
            self.fixture(root, collision=True)
            with patch('tooling.assemble_app.subprocess.run'):
                with self.assertRaisesRegex(ValueError, 'collides'):
                    assemble(root, Path(temp) / 'output')

    def test_helper_payload_hash_rechecked_after_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'repo'
            self.fixture(root, bad_hash=True)
            with patch('tooling.assemble_app.subprocess.run'):
                with self.assertRaisesRegex(ValueError, 'helper payload hash differs'):
                    assemble(root, Path(temp) / 'output')


@unittest.skipUnless((ROOT / 'maintained/patch-manifest.json').is_file(),
                     'Application source fixtures absent in replacement wheel tests')
class AssemblyIntegrationTests(unittest.TestCase):
    def test_sanitized_source_hash_change_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'repo'
            root.mkdir()
            for directory in ('recovered', 'maintained', 'tooling'):
                shutil.copytree(ROOT / directory, root / directory,
                                ignore=shutil.ignore_patterns('__pycache__'))
            source = root / 'recovered/utils/oauth_credential_import.py'
            source.write_text(source.read_text() + '\n# unexpected mutation\n')
            with self.assertRaisesRegex(ValueError, 'Baseline source hash differs'):
                assemble(root, Path(temp) / 'output')
            self.assertFalse((Path(temp) / 'output').exists())

    def test_repeated_assembly_identical_and_payload_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / 'one', Path(temp) / 'two'
            a = assemble(ROOT, first)
            b = assemble(ROOT, second)
            self.assertEqual(a, b)
            self.assertEqual((first / 'app-source.zip').read_bytes(),
                             (second / 'app-source.zip').read_bytes())
            patch_manifest = json.loads((ROOT / 'maintained/patch-manifest.json').read_text())
            helper_count = len(patch_manifest.get('new_helpers', []))
            self.assertEqual(len(a['files']), 50 + helper_count)
            self.assertEqual(sum(r['origin'] == 'new-helper' for r in a['files']), helper_count)
            self.assertEqual(sum(r['origin'] == 'maintained' for r in a['files']),
                             len(patch_manifest['modules']))
            self.assertEqual(sum(r['origin'] == 'sanitized' for r in a['files']), 1)
            for record in a['files']:
                raw = (first / record['path']).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), record['sha256'])
                self.assertEqual(raw, (ROOT / record['source']).read_bytes())
            with zipfile.ZipFile(first / 'app-source.zip') as archive:
                self.assertEqual(len(archive.namelist()), 51 + helper_count)
                self.assertEqual(archive.namelist(), sorted(archive.namelist()))
                self.assertFalse(any('cachecad/' in name for name in archive.namelist()))
                self.assertEqual(json.loads(archive.read('assembly-manifest.json')), a)


if __name__ == '__main__':
    unittest.main()
