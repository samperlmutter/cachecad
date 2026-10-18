"""New-file overwrite regressions on reviewed original-derived method ASTs.

No recovered module, account, executable, GUI, or service is initialized. Files
are disposable fixtures. Revision replacement is deliberately outside this fix.
"""
import hashlib
import importlib.util
import logging
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import MethodType, SimpleNamespace
import traceback
import unittest
from unittest.mock import Mock, patch

from recovered_harness import load_function, deny_external_effects, denied

ROOT = Path(__file__).resolve().parents[1]


def load_recovery_module():
    """Load the actual stdlib-only journal; never substitute a test journal."""
    spec = importlib.util.spec_from_file_location(
        'transfer_journal_fixture', ROOT / 'maintained/core/transfer_journal.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bind_recovery_executor(executor, namespace):
    """Attach actual journal/result methods to isolated executor test fixtures."""
    journal = load_recovery_module()
    namespace.update(TransferJournal=journal.TransferJournal,
                     PendingTransferRecovery=journal.PendingTransferRecovery)
    namespace.setdefault('StaleSyncPlan', RuntimeError)
    executor._transfer_journal = None
    executor._result_lock = threading.Lock()
    executor._settled_actions = set()
    executor.action_results = []
    executor.outcome_ready = Signal()
    if not hasattr(executor, 'guard_failed'):
        executor.guard_failed = Signal()
    if not hasattr(executor, 'project_id'):
        executor.project_id = 'project-id'
    for name in ('_prepare_transfer', '_settle_transfer'):
        method = load_function('maintained/core/sync_executor.py', 'SyncExecutor.' + name, namespace)
        setattr(executor, name, MethodType(method, executor))
    return journal


def load_staging_function(qualname, namespace, fixture_root):
    """Permit only three inspected stdlib imports in this isolated function.

The shared harness and interpreter import hook remain unchanged. Temporary
upload directories are also constrained beneath this test's fixture root.
"""
    def temporary_directory(*args, **kwargs):
        kwargs.setdefault('dir', str(fixture_root))
        return tempfile.TemporaryDirectory(*args, **kwargs)
    allowed = {'hashlib': hashlib, 'stat': stat,
               'tempfile': SimpleNamespace(TemporaryDirectory=temporary_directory)}
    def reviewed_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level != 0 or name not in allowed or fromlist:
            return denied()
        return allowed[name]
    method = load_function('maintained/core/sync_executor.py', qualname, namespace)
    method.__globals__['__builtins__']['__import__'] = reviewed_import
    return method


class Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


@unittest.skipUnless((ROOT / 'maintained/core/sync_executor.py').is_file(),
                     'Maintained sources are source-distribution fixtures')
class NewFileTransferTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.project = self.root / 'project'
        self.project.mkdir()
        self.path = self.project / 'part.sldprt'
        self.content = b'CAD fixture content\x00\x01'
        self.md5 = hashlib.md5(self.content).hexdigest()
        self.signals = SimpleNamespace(**{name: Signal() for name in
            ('action_failed', 'action_complete', 'error_occurred')})
        self.namespace = {'signal_manager': self.signals, 'logging': Mock(spec=logging),
                          'Path': Path, 'os': os, 'traceback': traceback}
        self.drive = SimpleNamespace(upload_file=Mock(return_value=('created', self.md5)),
                                     download_file=Mock(side_effect=self.download_bytes))
        self.obj = SimpleNamespace(cancelled=False, working_directory=str(self.root),
                                   project_name='project', drive_client=self.drive,
                                   get_parent_id=Mock(return_value='parent'),
                                   log_sync_action=Mock())
        bind_recovery_executor(self.obj, self.namespace)
        # Compile after adding the actual journal dependencies to the namespace.
        self.upload = load_staging_function('SyncExecutor._upload', self.namespace, self.root)
        self.download = load_staging_function('SyncExecutor._download', self.namespace, self.root)
        self.selected = SimpleNamespace(path='part.sldprt', md5_checksum=self.md5,
                                        size=len(self.content), modified_time=0,
                                        drive_id='remote-source')
        self.action = (7, self.selected)

    def write_source(self):
        self.path.write_bytes(self.content)
        self.selected.modified_time = self.path.stat().st_mtime

    def download_bytes(self, drive_id, requested, idx):
        target = Path(requested).parent / f'~$downloading{idx}.temp'
        target.write_bytes(self.content)
        return target

    def terminal(self, success):
        self.assertEqual(self.signals.action_complete.calls,
                         [(self.selected,)] if success else [])
        self.assertEqual(self.signals.action_failed.calls, [] if success else [()])

    def assert_staging_cleaned(self):
        self.assertEqual(list(self.root.glob('cachecad-upload-*')), [])
        self.assertEqual(list(self.project.glob('.cachecad-download-*')), [])

    def test_baseline_existing_new_download_destination_loses_terminal_event(self):
        original = load_function('recovered/core/sync_executor.py', 'SyncExecutor._download', self.namespace)
        self.obj._make_local_folders = Mock()
        self.path.write_bytes(b'existing CAD file')
        original(self.obj, self.action)
        self.assertEqual(self.signals.action_complete.calls, [])
        self.assertEqual(self.signals.action_failed.calls, [])
        self.assertEqual(self.path.read_bytes(), b'existing CAD file')

    @unittest.skipIf(os.name == 'nt', 'Original os.rename clobber reproduction is POSIX-specific')
    def test_baseline_destination_appears_during_transfer_is_overwritten(self):
        original = load_function('recovered/core/sync_executor.py', 'SyncExecutor._download', self.namespace)
        self.obj._make_local_folders = Mock()
        self.obj.local_client = SimpleNamespace(calculate_md5=Mock(return_value=self.md5))
        def race(*args):
            temp_path = self.download_bytes(*args)
            self.path.write_bytes(b'a concurrent local revision')
            return temp_path
        self.drive.download_file.side_effect = race
        original(self.obj, self.action)
        self.terminal(True)
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_uppercase_upload_checksum_is_accepted_consistently(self):
        self.write_source()
        self.selected.md5_checksum = self.md5.upper()
        self.upload(self.obj, self.action)
        self.terminal(True)

    def test_corrupted_staged_copy_blocks_before_transfer(self):
        self.write_source()
        original_fsync = os.fsync
        def corrupt(fd):
            original_fsync(fd)
            staged = next(self.root.glob('cachecad-upload-*/part.sldprt'))
            staged.write_bytes(b'x' * len(self.content))
        with patch.object(os, 'fsync', side_effect=corrupt):
            self.upload(self.obj, self.action)
        self.terminal(False)
        self.drive.upload_file.assert_not_called()
        self.assert_staging_cleaned()

    def test_upload_uses_private_same_name_snapshot_and_cleans_up(self):
        self.write_source()
        seen = []
        def upload(staged, parent, idx, existing_id):
            copied = Path(staged)
            self.assertNotEqual(copied, self.path)
            self.assertEqual(copied.name, self.path.name)
            self.assertEqual(copied.read_bytes(), self.content)
            self.assertEqual((parent, idx, existing_id), ('parent', 7, None))
            self.assertFalse(copied.stat().st_mode & stat.S_IWUSR)
            seen.append(copied)
            return 'created', self.md5
        self.drive.upload_file.side_effect = upload
        self.upload(self.obj, self.action)
        self.terminal(True)
        self.assertEqual(self.path.read_bytes(), self.content)
        self.assertFalse(seen[0].exists())
        self.obj.log_sync_action.assert_called_once_with(self.action, id='created')
        self.assert_staging_cleaned()

    def test_source_changed_after_snapshot_does_not_change_uploaded_bytes(self):
        self.write_source()
        def upload(staged, *args):
            self.path.write_bytes(b'a later local revision')
            self.assertEqual(Path(staged).read_bytes(), self.content)
            return 'created', self.md5
        self.drive.upload_file.side_effect = upload
        self.upload(self.obj, self.action)
        self.terminal(True)
        self.assertEqual(self.path.read_bytes(), b'a later local revision')
        self.assert_staging_cleaned()

    def test_stale_source_timestamp_blocks_before_network(self):
        self.write_source()
        self.selected.modified_time -= 1
        self.upload(self.obj, self.action)
        self.terminal(False)
        self.drive.upload_file.assert_not_called()

    def test_stale_source_hash_even_same_size_and_mtime_blocks(self):
        self.write_source()
        info = self.path.stat()
        self.path.write_bytes(b'x' * len(self.content))
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.upload(self.obj, self.action)
        self.terminal(False)
        self.drive.upload_file.assert_not_called()
        self.assert_staging_cleaned()

    def test_source_mutation_during_copy_blocks_before_network(self):
        self.write_source()
        actual_fstat = os.fstat
        calls = []
        def mutate_after_read(fd):
            calls.append(fd)
            if len(calls) == 2:
                self.path.write_bytes(b'x' * len(self.content))
            return actual_fstat(fd)
        with patch.object(os, 'fstat', side_effect=mutate_after_read):
            self.upload(self.obj, self.action)
        self.terminal(False)
        self.drive.upload_file.assert_not_called()
        self.assert_staging_cleaned()

    def test_source_replacement_during_copy_blocks_before_network(self):
        self.write_source()
        actual_fstat = os.fstat
        calls = []
        def replace_after_read(fd):
            calls.append(fd)
            if len(calls) == 2:
                replacement = self.project / 'replacement'
                replacement.write_bytes(self.content)
                replacement.replace(self.path)
            return actual_fstat(fd)
        with patch.object(os, 'fstat', side_effect=replace_after_read):
            self.upload(self.obj, self.action)
        self.terminal(False)
        self.drive.upload_file.assert_not_called()

    def test_missing_upload_metadata_fails_closed(self):
        self.write_source()
        del self.selected.modified_time
        self.upload(self.obj, self.action)
        self.terminal(False)
        self.drive.upload_file.assert_not_called()

    def test_upload_exception_cleans_private_snapshot(self):
        self.write_source()
        self.drive.upload_file.side_effect = RuntimeError('fake transfer failure')
        self.upload(self.obj, self.action)
        self.terminal(False)
        self.obj.log_sync_action.assert_not_called()
        self.assert_staging_cleaned()

    def test_upload_cancelled_after_staging_does_not_transfer(self):
        self.write_source()
        original_fsync = os.fsync
        def cancel(fd):
            original_fsync(fd)
            self.obj.cancelled = True
        with patch.object(os, 'fsync', side_effect=cancel):
            self.upload(self.obj, self.action)
        self.terminal(False)
        self.drive.upload_file.assert_not_called()
        self.assert_staging_cleaned()

    def test_download_new_publishes_verified_bytes(self):
        self.download(self.obj, self.action)
        self.terminal(True)
        self.assertEqual(self.path.read_bytes(), self.content)
        self.obj.log_sync_action.assert_called_once_with(self.action)
        self.assert_staging_cleaned()

    def test_existing_destination_blocks_before_download(self):
        self.path.write_bytes(b'existing CAD file')
        self.download(self.obj, self.action)
        self.terminal(False)
        self.drive.download_file.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'existing CAD file')

    def test_destination_appears_at_atomic_publication_is_never_overwritten(self):
        actual_link = os.link
        def racing_link(src, dst, **kwargs):
            if Path(dst) == self.path:
                Path(dst).write_bytes(b'a concurrent local revision')
            return actual_link(src, dst, **kwargs)
        with patch.object(os, 'link', side_effect=racing_link):
            self.download(self.obj, self.action)
        self.terminal(False)
        self.assertEqual(self.path.read_bytes(), b'a concurrent local revision')
        self.obj.log_sync_action.assert_not_called()
        self.assert_staging_cleaned()

    def test_unsupported_hardlinks_fail_without_rename_fallback(self):
        actual_link, actual_rename, actual_replace = os.link, os.rename, os.replace
        def no_publication_link(src, dst, **kwargs):
            if Path(dst) == self.path:
                raise OSError('hardlinks unavailable')
            return actual_link(src, dst, **kwargs)
        def guarded_move(method):
            def move(src, dst, **kwargs):
                if Path(dst) == self.path:
                    raise AssertionError('unsafe destination replacement fallback')
                return method(src, dst, **kwargs)
            return move
        with patch.object(os, 'link', side_effect=no_publication_link), \
                patch.object(os, 'rename', side_effect=guarded_move(actual_rename)), \
                patch.object(os, 'replace', side_effect=guarded_move(actual_replace)):
            self.download(self.obj, self.action)
        self.terminal(False)
        self.assertFalse(self.path.exists())
        self.assert_staging_cleaned()

    def test_download_wrong_hash_or_size_never_publishes(self):
        for field, value in [('md5_checksum', '0' * 32), ('size', len(self.content) + 1)]:
            with self.subTest(field=field):
                saved = getattr(self.selected, field)
                setattr(self.selected, field, value)
                self.signals.action_failed.calls.clear()
                self.obj._settled_actions.clear()
                self.obj.action_results.clear()
                self.download(self.obj, self.action)
                self.terminal(False)
                self.assertFalse(self.path.exists())
                setattr(self.selected, field, saved)
        self.assert_staging_cleaned()

    def test_download_cancellation_after_transfer_never_publishes(self):
        def cancel(*args):
            result = self.download_bytes(*args)
            self.obj.cancelled = True
            return result
        self.drive.download_file.side_effect = cancel
        self.download(self.obj, self.action)
        self.terminal(False)
        self.assertFalse(self.path.exists())
        self.assert_staging_cleaned()

    def test_download_returning_unowned_path_is_not_used_or_deleted(self):
        unowned = self.root / 'unowned'
        unowned.write_bytes(self.content)
        self.drive.download_file.side_effect = None
        self.drive.download_file.return_value = unowned
        self.download(self.obj, self.action)
        self.terminal(False)
        self.assertEqual(unowned.read_bytes(), self.content)
        self.assertFalse(self.path.exists())
        self.assert_staging_cleaned()

    def test_traversal_rejected_for_both_new_file_operations(self):
        self.selected.path = '../outside.sldprt'
        for method in (self.upload, self.download):
            self.signals.action_failed.calls.clear()
            method(self.obj, self.action)
            self.terminal(False)
        self.drive.upload_file.assert_not_called()
        self.drive.download_file.assert_not_called()

    def test_import_guard_does_not_allow_unreviewed_modules(self):
        importer = self.upload.__globals__['__builtins__']['__import__']
        with self.assertRaises(AssertionError):
            importer('socket')
        with self.assertRaises(AssertionError):
            importer('tempfile', level=1)


if __name__ == '__main__':
    unittest.main()
