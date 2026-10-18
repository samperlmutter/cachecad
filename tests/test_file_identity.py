"""Portable identity guards and native-Windows recovery regression fixtures.

All files are disposable. Native Windows checks report metadata tuples on failure,
never user paths, file bytes, credentials, or account data.
"""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from recovered_harness import ROOT, deny_external_effects, file_identity_imports

SOURCE = ROOT / 'maintained/core/file_identity.py'
if not SOURCE.is_file():
    raise unittest.SkipTest('Maintained helper absent from prototype wheel')
SPEC = importlib.util.spec_from_file_location('identity_under_test', SOURCE)
identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(identity)


def stat_fixture(*, windows=False, ctime=90, birthtime=10, **changes):
    fields = dict(st_dev=2, st_ino=17, st_size=4, st_mtime_ns=80, st_ctime_ns=ctime)
    if windows:
        fields.update(st_file_attributes=32, st_birthtime_ns=birthtime)
    fields.update(changes)
    return SimpleNamespace(**fields)


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'maintained/core' / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    with file_identity_imports():
        spec.loader.exec_module(module)
    return module


class FileIdentityTests(unittest.TestCase):
    def test_windows_path_and_handle_use_consistent_birthtime(self):
        path = stat_fixture(windows=True, ctime=10)
        opened = stat_fixture(windows=True, ctime=90)
        self.assertEqual(identity.file_identity(path), identity.file_identity(opened))
        self.assertNotEqual(identity.handle_identity(path), identity.handle_identity(opened))

    def test_windows_same_handle_change_time_remains_guarded(self):
        before = stat_fixture(windows=True, ctime=90)
        after = stat_fixture(windows=True, ctime=91)
        self.assertEqual(identity.file_identity(before), identity.file_identity(after))
        self.assertNotEqual(identity.handle_identity(before), identity.handle_identity(after))

    def test_posix_ctime_is_not_replaced_even_when_birthtime_exists(self):
        before = stat_fixture(st_birthtime_ns=10)
        after = stat_fixture(ctime=91, st_birthtime_ns=10)
        self.assertNotEqual(identity.file_identity(before), identity.file_identity(after))

    def test_windows_missing_or_invalid_birthtime_fails_closed(self):
        for value in (None, -1, True, 10.0, '10'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    identity.file_identity(stat_fixture(windows=True, birthtime=value))
        missing = stat_fixture(windows=True)
        del missing.st_birthtime_ns
        with self.assertRaises(ValueError):
            identity.file_identity(missing)

    def test_inode_size_and_mtime_changes_are_still_distinguished(self):
        original = stat_fixture(windows=True)
        for field, value in (('st_dev', 3), ('st_ino', 18), ('st_size', 5), ('st_mtime_ns', 81), ('st_birthtime_ns', 11)):
            with self.subTest(field=field):
                self.assertNotEqual(identity.file_identity(original),
                    identity.file_identity(stat_fixture(windows=True, **{field: value})))


class CanonicalRecoveryPathTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.folder / 'project'
        self.root.mkdir()
        self.path = self.root / 'part.sldprt'
        self.path.write_bytes(b'approved local')
        self.guard = load_module('identity_guard_fixture', 'sync_consent.py')
        self.journal = load_module('identity_journal_fixture', 'transfer_journal.py')

    def test_nonlink_project_alias_canonicalizes_before_backup_comparison(self):
        alias_root = self.folder / 'project-alias'
        alias_root.mkdir()
        real_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            return self.root if path == alias_root else real_resolve(path, *args, **kwargs)
        observed = self.guard.capture_local_version(self.path)
        with patch.object(Path, 'resolve', resolve):
            store = self.journal.TransferJournal(alias_root, 'project-id')
            self.assertEqual(store.project_root, self.root)
            operation = store.prepare_intent(action_kind='delete_local', path=self.path.name,
                                             approved_local=observed)
            operation.preserve_local_backup(self.path, md5=observed.md5, size=observed.size,
                                            identity=observed.identity)
        self.assertEqual(operation.backup_path.read_bytes(), b'approved local')

    def test_project_symlink_is_rejected_before_canonicalization(self):
        alias_root = self.folder / 'project-link'
        try:
            alias_root.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest('Fixture platform does not permit symlinks')
        with self.assertRaises(self.journal.JournalError):
            self.journal.TransferJournal(alias_root, 'project-id')

    def test_backup_symlink_is_not_accepted_via_resolution(self):
        observed = self.guard.capture_local_version(self.path)
        store = self.journal.TransferJournal(self.root, 'project-id')
        operation = store.prepare_intent(action_kind='delete_local', path=self.path.name,
                                         approved_local=observed)
        alias = self.folder / 'file-link'
        try:
            alias.symlink_to(self.path)
        except OSError:
            self.skipTest('Fixture platform does not permit symlinks')
        with self.assertRaises(self.journal.JournalError):
            operation.preserve_local_backup(alias, md5=observed.md5, size=observed.size)
        self.assertFalse(operation.backup_path.exists())


@unittest.skipUnless(os.name == 'nt', 'Requires native Windows stat/fstat behavior')
class NativeWindowsIdentityTests(unittest.TestCase):
    def test_repeated_capture_copy_and_journal_mutation_after_file_rewrite(self):
        self.enterContext(deny_external_effects())
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        root = folder / 'project'
        root.mkdir()
        guard = load_module('native_windows_guard', 'sync_consent.py')
        journal = load_module('native_windows_journal', 'transfer_journal.py')
        store = journal.TransferJournal(root, 'project-id')
        for index in range(20):
            with self.subTest(iteration=index):
                path = root / f'part{index}.sldprt'
                path.write_bytes(b'before')
                path.write_bytes(b'after!')
                path_stat = path.lstat()
                with path.open('rb') as source:
                    handle_stat = os.fstat(source.fileno())
                fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_birthtime_ns')
                diagnostic = {kind: tuple(getattr(info, field, None) for field in fields)
                              for kind, info in (('path', path_stat), ('handle', handle_stat))}
                try:
                    observed = guard.capture_local_version(path)
                    operation = store.prepare_intent(action_kind='delete_local', path=path.name,
                                                     approved_local=observed)
                    operation.preserve_local_backup(path.resolve(strict=True), md5=observed.md5,
                                                    size=observed.size, identity=observed.identity)
                    operation.verify_recovery_material()
                    operation.record_outcome('unknown', phase='mutation_started')
                    path.unlink()
                    operation.record_outcome('committed', phase='completed')
                    self.assertEqual(operation.backup_path.read_bytes(), b'after!')
                except Exception as error:
                    self.fail(f'{type(error).__name__}: {error}; synthetic metadata={diagnostic}')
        self.assertEqual(store.list_pending(), ())


if __name__ == '__main__':
    unittest.main()
