"""Synthetic manifest-gate regressions; never import recovered application code."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from check_reports import validate
from compare_bytecode import structural_fingerprint


class RecoveryGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'reports').mkdir()
        self.source = self.root / 'fixture.py'
        self.source.write_text('x = 1\n')
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.structure = structural_fingerprint(compile(self.source.read_bytes(), 'fixture.py', 'exec'))
        self.report_path = self.root / 'reports' / 'fixture.json'
        self.report = {'module': 'fixture', 'match': True,
                       'source_sha256': self.source_hash,
                       'original_structure_sha256': self.structure,
                       'code_objects_compared': 1}
        self.write_report()
        self.manifest = {'entries': [
            {'module': 'fixture', 'status': 'exact_compiled_structure',
             'source_sha256': self.source_hash,
             'report_sha256': hashlib.sha256(self.report_path.read_bytes()).hexdigest(),
             'original_structure_sha256': self.structure, 'original_code_objects': 1},
            *({'module': f'absent_{i}', 'status': 'not_recovered'} for i in range(48))]}
        self.write_manifest()

    def write_report(self):
        self.report_path.write_text(json.dumps(self.report))

    def write_manifest(self):
        (self.root / 'recovery-manifest.json').write_text(json.dumps(self.manifest))

    def test_valid_complete_fixture(self):
        self.assertEqual(validate(self.root), (1, []))

    def test_deleted_report_rejected(self):
        self.report_path.unlink()
        with self.assertRaisesRegex(ValueError, 'report set'):
            validate(self.root)

    def test_toggled_match_rejected(self):
        self.report['match'] = False
        self.write_report()
        with self.assertRaisesRegex(ValueError, 'report set'):
            validate(self.root)

    def test_missing_match_field_rejected(self):
        del self.report['match']
        self.write_report()
        with self.assertRaisesRegex(ValueError, 'report set'):
            validate(self.root)

    def test_changed_source_rejected(self):
        self.source.write_text('x = 2\n')
        count, failures = validate(self.root)
        self.assertEqual(count, 1)
        self.assertTrue(any('source hash' in f for f in failures))
        self.assertTrue(any('structure' in f for f in failures))

    def test_report_hash_change_rejected(self):
        self.report['extra'] = 'unreviewed edit'
        self.write_report()
        self.assertTrue(any('report hash' in f for f in validate(self.root)[1]))

    def test_missing_source_rejected(self):
        self.source.unlink()
        with self.assertRaises(OSError):
            validate(self.root)

    def test_missing_inventory_entry_rejected(self):
        self.manifest['entries'].pop()
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, '49 distinct'):
            validate(self.root)

    def test_duplicate_report_rejected(self):
        (self.root / 'reports' / 'duplicate.json').write_text(json.dumps(self.report))
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            validate(self.root)

    def test_code_object_count_change_rejected(self):
        self.report['code_objects_compared'] = 2
        self.write_report()
        self.assertTrue(any('coverage' in f for f in validate(self.root)[1]))


if __name__ == '__main__':
    unittest.main()
