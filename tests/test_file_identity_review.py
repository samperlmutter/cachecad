"""Independent synthetic Windows timestamp checks on actual maintained helpers.

Only timestamp observations are injected. Reads and copies use disposable real
files. These tests do not substitute for the native Windows integration gate.
"""
from contextlib import contextmanager
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from recovered_harness import deny_external_effects


ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'maintained/core/file_identity.py').is_file():
    raise unittest.SkipTest('Maintained file identity helper absent')


def load_actual(stem):
    name = 'independent_file_identity_review_' + stem
    spec = importlib.util.spec_from_file_location(name, ROOT / f'maintained/core/{stem}.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


identity = load_actual('file_identity')
with patch.dict(sys.modules, {'core.file_identity': identity}):
    consent = load_actual('sync_consent')
    journal = load_actual('transfer_journal')
    history = load_actual('sync_history')


class StatObservation:
    def __init__(self, actual, **overrides):
        self.actual = actual
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self.actual, name)


@contextmanager
def windows_observations(path, *, change_during_read=False):
    """Model CPython's creation-time path stat and change-time handle stat."""
    original_lstat, original_fstat = Path.lstat, os.fstat
    source = original_lstat(path)
    birthtime = getattr(source, 'st_birthtime_ns', source.st_ctime_ns)
    handle_samples = 0

    def path_stat(target, *args, **kwargs):
        actual = original_lstat(target, *args, **kwargs)
        if target != path:
            return actual
        return StatObservation(actual, st_file_attributes=0,
                               st_birthtime_ns=birthtime, st_ctime_ns=birthtime)

    def handle_stat(fd):
        nonlocal handle_samples
        actual = original_fstat(fd)
        if (actual.st_dev, actual.st_ino) != (source.st_dev, source.st_ino):
            return actual
        handle_samples += 1
        change_time = birthtime + 100
        if change_during_read and handle_samples > 1:
            change_time += 1
        return StatObservation(actual, st_file_attributes=0,
                               st_birthtime_ns=birthtime, st_ctime_ns=change_time)

    with patch.object(Path, 'lstat', path_stat), patch.object(os, 'fstat', handle_stat):
        yield


class IndependentFileIdentityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / 'source.bin'
        self.path.write_bytes(b'{"fixture":true}\n')

    def test_windows_cross_api_identity_normalizes_birthtime_without_weakening_handle_check(self):
        path = SimpleNamespace(st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4,
                               st_ctime_ns=5, st_birthtime_ns=5, st_file_attributes=0)
        handle = SimpleNamespace(**dict(vars(path), st_ctime_ns=99))
        changed = SimpleNamespace(**dict(vars(handle), st_ctime_ns=100))
        self.assertEqual(identity.file_identity(path), identity.file_identity(handle))
        self.assertNotEqual(identity.handle_identity(handle), identity.handle_identity(changed))

    def test_posix_keeps_ctime_even_when_birthtime_is_present(self):
        first = SimpleNamespace(st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4,
                                st_ctime_ns=5, st_birthtime_ns=999)
        changed = SimpleNamespace(**dict(vars(first), st_ctime_ns=6))
        self.assertNotEqual(identity.file_identity(first), identity.file_identity(changed))
        self.assertEqual(identity.file_identity(first)[-1], 5)

    def test_missing_invalid_windows_birthtime_never_falls_back_to_ctime(self):
        for value in (None, -1, True, 1.5, '10'):
            with self.subTest(value=value):
                info = SimpleNamespace(st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4,
                                       st_ctime_ns=5, st_file_attributes=0)
                if value is not None:
                    info.st_birthtime_ns = value
                with self.assertRaises(ValueError):
                    identity.file_identity(info)

    def test_actual_consent_hash_and_json_reads_accept_stable_windows_api_difference(self):
        for operation in (consent.capture_local_version, journal._hash, journal._read_json):
            with self.subTest(operation=operation.__name__), windows_observations(self.path):
                self.assertIsNotNone(operation(self.path))

    def test_actual_consent_hash_and_json_reads_reject_windows_handle_change_only(self):
        for operation, error in ((consent.capture_local_version, consent.StaleSyncPlan),
                                 (journal._hash, journal.JournalError),
                                 (journal._read_json, journal.JournalError)):
            with self.subTest(operation=operation.__name__), windows_observations(
                    self.path, change_during_read=True), self.assertRaises(error):
                operation(self.path)

    def test_actual_recovery_copy_rejects_windows_handle_change_only(self):
        content = self.path.read_bytes()
        destination = self.root / 'copied.bin'
        with windows_observations(self.path, change_during_read=True), self.assertRaises(journal.JournalError):
            journal._copy_verified(self.path, destination, hashlib.md5(content).hexdigest(), len(content))
        self.assertEqual(self.path.read_bytes(), content)

    def test_actual_history_read_rejects_windows_handle_change_only(self):
        self.path.write_text(','.join(history.FIELDS) + '\n', encoding='utf-8')
        with windows_observations(self.path):
            self.assertEqual(history.read_sync_history(self.path), {})
        with windows_observations(self.path, change_during_read=True), self.assertRaises(history.SyncHistoryError):
            history.read_sync_history(self.path)


if __name__ == '__main__':
    unittest.main()
