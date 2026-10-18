import copy
from pathlib import Path
import tempfile
import unittest
from tooling.manifests import validate


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'src/cachecad').mkdir(parents=True)
        (self.root / 'src/cachecad/example.py').write_text('# synthetic\n')
        self.hash = 'a' * 64
        self.evidence = {'schema_version': 1, 'category': 'static-evidence', 'files': [
            {'path': 'bytecode/app.example.dis.txt', 'bytes': 10, 'sha256': self.hash}]}
        self.reconstruction = {'schema_version': 1, 'original_modules_executed': False,
            'full_fidelity_modules_verified': 0, 'installer_sha256': self.hash,
            'application_sha256': self.hash, 'entries': [{'original_module': 'app.example',
            'status': 'partial_reconstruction', 'equivalence_verified': False,
            'static_evidence': {'relative_to_evidence_root': 'bytecode/app.example.dis.txt',
                                'sha256': self.hash},
            'reconstructed_sources': ['src/cachecad/example.py']}]}

    def test_valid_without_private_evidence(self):
        self.assertEqual(validate(self.root, self.evidence, self.reconstruction), (1, 1))

    def test_bad_hash_rejected(self):
        self.evidence['files'][0]['sha256'] = 'bad'
        with self.assertRaises(ValueError):
            validate(self.root, self.evidence, self.reconstruction)

    def test_duplicate_rejected(self):
        self.evidence['files'] *= 2
        with self.assertRaises(ValueError):
            validate(self.root, self.evidence, self.reconstruction)

    def test_source_escape_rejected(self):
        self.reconstruction['entries'][0]['reconstructed_sources'] = ['../escape.py']
        with self.assertRaises(ValueError):
            validate(self.root, self.evidence, self.reconstruction)

    def test_missing_source_rejected(self):
        (self.root / 'src/cachecad/example.py').unlink()
        with self.assertRaises(ValueError):
            validate(self.root, self.evidence, self.reconstruction)

    def test_conflicting_manifests_rejected(self):
        changed = copy.deepcopy(self.reconstruction)
        changed['entries'][0]['static_evidence']['sha256'] = 'b' * 64
        with self.assertRaises(ValueError):
            validate(self.root, self.evidence, changed)
