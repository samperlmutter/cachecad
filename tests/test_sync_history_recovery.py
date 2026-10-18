"""Strict legacy baseline and internal recovery namespace regression tests."""
import csv
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest

from recovered_harness import file_identity_imports, load_function
from test_recovered_scan_outcomes import LocalFixture

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'maintained/core/sync_history.py'
if not SOURCE.is_file():
    raise unittest.SkipTest('Actual maintained source absent')
spec = importlib.util.spec_from_file_location('history_under_test', SOURCE)
history = importlib.util.module_from_spec(spec)
with file_identity_imports():
    spec.loader.exec_module(history)


class HistoryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / 'sync_log.csv'
        self.row = ['id', 'part.step', 'part.step', hashlib.md5(b'fixture').hexdigest(),
                    '123.0', '7', '124.0', 'upload_new']

    def write(self, rows, fields=history.FIELDS):
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(fields)
        writer.writerows(rows)
        self.path.write_text(stream.getvalue(), newline='')

    def test_hardlinked_history_is_rejected_without_modification(self):
        self.write([self.row])
        outside = self.root / 'other.csv'
        os.link(self.path, outside)
        before = outside.read_bytes()
        with self.assertRaises(history.SyncHistoryError):
            history.read_sync_history(self.path)
        self.assertEqual(outside.read_bytes(), before)

    def test_valid_repeated_path_last_committed_row_wins(self):
        later = list(self.row); later[-1] = 'download_revision'; later[-2] = '125'
        self.write([self.row, later])
        self.assertEqual(history.read_sync_history(self.path)['part.step']['action'], 'download_revision')

    def test_missing_history_and_header_only_are_empty(self):
        self.assertEqual(history.read_sync_history(self.path), {})
        self.write([])
        self.assertEqual(history.read_sync_history(self.path), {})

    def test_torn_missing_extra_fields_and_bad_header_fail_closed(self):
        for rows, fields in (([self.row[:-1]], history.FIELDS),
                             ([self.row + ['extra']], history.FIELDS),
                             ([self.row], tuple(reversed(history.FIELDS)))):
            self.write(rows, fields)
            with self.assertRaises(history.SyncHistoryError):
                history.read_sync_history(self.path)
        self.write([self.row]); self.path.write_bytes(self.path.read_bytes().rstrip(b'\r\n'))
        with self.assertRaises(history.SyncHistoryError):
            history.read_sync_history(self.path)

    def test_invalid_hash_action_size_timestamp_and_path_rejected(self):
        for index, value in ((3, 'not-a-checksum'), (7, 'made-up'), (5, '-1'),
                             (6, 'nan'), (4, 'inf'), (2, '../part.step'),
                             (2, r'C:\part.step'), (2, '.cachecad/part.step')):
            with self.subTest(index=index, value=value):
                row = list(self.row); row[index] = value
                self.write([row])
                with self.assertRaises(history.SyncHistoryError):
                    history.read_sync_history(self.path)

    def test_analyzer_clears_previous_history_before_corrupt_read(self):
        from types import SimpleNamespace
        self.write([self.row[:-1]])
        analyzer = SimpleNamespace(sync_log_path=self.path, sync_log_dict={'old': {'md5': 'old'}})
        method = load_function('maintained/core/sync_analyzer.py', 'SyncAnalyzer.read_sync_log',
                               {'read_sync_history': history.read_sync_history})
        with self.assertRaises(history.SyncHistoryError):
            method(analyzer)
        self.assertEqual(analyzer.sync_log_dict, {})

    def test_nested_recovery_material_never_reaches_local_scan(self):
        project = self.root / 'project'; project.mkdir()
        recovery = project / '.cachecad/recovery/op'; recovery.mkdir(parents=True)
        (recovery / 'backup.step').write_bytes(b'private recovery')
        (project / 'part.step').write_bytes(b'live CAD fixture')
        fixture = LocalFixture(self.root)
        fixture.start('project', 1)
        self.assertEqual([job.rel_path for job in fixture.jobs], ['part.step'])
        fixture.jobs[0].succeed()
        self.assertEqual([item.path for item in fixture.successes[0][1]], ['part.step'])
        self.assertEqual(fixture.errors, [])


if __name__ == '__main__':
    unittest.main()
