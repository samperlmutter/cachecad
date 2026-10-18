"""Actual maintained consent/guard and executor methods, offline disposable files.

No real Qt, CAD, account, Drive transport, or baseline application imports.
The tests make deterministic competing updates at the preflight/staging boundary;
they do not establish atomic check-and-write or live Google compatibility.
"""
import hashlib
import importlib.util
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import traceback

from recovered_harness import deny_external_effects, file_identity_imports, load_function
from test_recovered_overwrite import bind_recovery_executor, load_staging_function

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'maintained/core/sync_consent.py').is_file():
    raise unittest.SkipTest('Actual application sources absent from experimental wheel')
SPEC = importlib.util.spec_from_file_location('maintained_sync_consent_test', ROOT / 'maintained/core/sync_consent.py')
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
with file_identity_imports():
    SPEC.loader.exec_module(guard)


def digest(data):
    return hashlib.md5(data).hexdigest()


class Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class ConsentTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.folder / 'project'
        self.root.mkdir()
        self.path = self.root / 'part.sldprt'
        self.local_bytes = b'approved local bytes'
        self.remote_bytes = b'approved cloud bytes'
        self.path.write_bytes(self.local_bytes)
        self.responses = {
            'project-id': {'id': 'project-id', 'name': 'project', 'trashed': False,
                           'mimeType': 'application/vnd.google-apps.folder'},
            'file-id': {'id': 'file-id', 'name': self.path.name, 'parents': ['project-id'],
                        'trashed': False, 'version': '7', 'md5Checksum': digest(self.remote_bytes),
                        'size': str(len(self.remote_bytes)), 'mimeType': 'application/octet-stream'}}
        self.files = SimpleNamespace(get=Mock(side_effect=self.get), list=Mock(return_value=SimpleNamespace(
            execute=Mock(return_value={'files': [], 'incompleteSearch': False}))))
        self.service = SimpleNamespace(files=Mock(return_value=self.files), close=Mock())
        self.drive = SimpleNamespace(_new_service=Mock(return_value=self.service),
            upload_file=Mock(side_effect=self.upload), download_file=Mock(side_effect=self.download),
            delete_file=Mock(return_value=True))
        self.signals = SimpleNamespace(**{name: Signal() for name in
            ('action_failed', 'action_complete', 'error_occurred')})
        self.trash = Mock(side_effect=lambda path: Path(path).unlink())
        self.namespace = {name: getattr(guard, name) for name in (
            'StaleSyncPlan', 'approved_action', 'revalidate_action', 'staged_revision_upload',
            'validate_selection', 'project_path', 'verify_downloaded_revision')}
        self.namespace.update(signal_manager=self.signals, logging=Mock(spec=logging),
                              os=os, Path=Path, traceback=traceback, send2trash=self.trash)
        self.methods = {name: (load_staging_function('SyncExecutor.' + name, self.namespace, self.folder)
                               if name in ('_upload', '_download') else
                               load_function('maintained/core/sync_executor.py', 'SyncExecutor.' + name, self.namespace))
                        for name in ('_upload', '_download', '_delete_local', '_delete_cloud')}
        self.executor = SimpleNamespace(cancelled=False, working_directory=str(self.folder),
            project_name='project', project_id='project-id', drive_client=self.drive,
            get_parent_id=Mock(return_value='project-id'), log_sync_action=Mock(), guard_failed=Signal())
        bind_recovery_executor(self.executor, self.namespace)

    def get(self, **kwargs):
        return SimpleNamespace(execute=lambda: dict(self.responses[kwargs['fileId']]))

    def upload(self, path, parent, index, existing_id):
        self.assertNotEqual(Path(path), self.path)
        self.assertEqual(Path(path).read_bytes(), self.local_bytes)
        self.assertEqual((parent, index, existing_id), ('project-id', 3, 'file-id'))
        return existing_id, digest(self.local_bytes)

    def download(self, file_id, requested, index):
        path = Path(requested).parent / '~$downloading3.temp'
        path.write_bytes(self.remote_bytes)
        return path

    def prepare(self, kind='upload_revision'):
        # Each approval fixture represents a fresh executor run. Production
        # deduplicates outcomes within a run, so reset only fixture run state.
        self.executor._settled_actions.clear()
        self.executor.action_results.clear()
        local_exists = kind not in ('delete_cloud', 'download_new')
        remote_exists = kind not in ('delete_local', 'upload_new')
        if not local_exists and self.path.exists():
            self.path.unlink()
        local = SimpleNamespace(path=str(self.path.relative_to(self.root)), is_folder=False, size=len(self.local_bytes),
            md5_checksum=digest(self.local_bytes), modified_time=self.path.stat().st_mtime) if local_exists else None
        remote = SimpleNamespace(path=str(self.path.relative_to(self.root)), id='file-id', name=self.path.name,
            parent='project-id', md5_checksum=digest(self.remote_bytes), version='7',
            size=len(self.remote_bytes), is_folder=False)
        source = local if kind.startswith('upload') or kind == 'delete_local' else remote
        self.selected = SimpleNamespace(path=str(self.path.relative_to(self.root)), action=SimpleNamespace(value=kind),
            reason='fixture', md5_checksum=source.md5_checksum, size=source.size,
            parent_id='project-id' if remote_exists else '', modified_by='', modified_time=getattr(source, 'modified_time', ''),
            drive_id='file-id' if remote_exists else None, name=self.path.name, folder=None, parent_name=None)
        self.action = (3, self.selected)
        self.snapshot = guard.capture_scan_snapshot([self.selected], [local] if local else [],
            [remote] if remote_exists else [], self.folder, 'project', 'project-id', {})
        self.executor.consent = guard.approve_sync_actions([self.action], self.snapshot)
        return local, remote

    def run_action(self, kind):
        method = {'upload_new': '_upload', 'download_new': '_download', 'upload_revision': '_upload', 'upload_unknown': '_upload',
                  'download_revision': '_download', 'download_unknown': '_download',
                  'delete_local': '_delete_local', 'delete_cloud': '_delete_cloud'}[kind]
        args = {'revision': kind not in ('upload_new', 'download_new')} if method in ('_upload', '_download') else {}
        self.methods[method](self.executor, self.action, **args)

    def assert_failed(self):
        self.assertEqual(self.signals.action_failed.calls, [()])
        self.assertEqual(self.signals.action_complete.calls, [])
        self.executor.log_sync_action.assert_not_called()

    def assert_no_mutation(self):
        self.drive.upload_file.assert_not_called()
        self.drive.delete_file.assert_not_called()
        self.trash.assert_not_called()

    def test_snapshots_are_immutable_and_independent_of_scanned_models(self):
        local, remote = self.prepare()
        local.md5_checksum = '0' * 32
        remote.version = '8'
        self.assertEqual(self.snapshot.actions[0].local.md5, digest(self.local_bytes))
        self.assertEqual(self.snapshot.actions[0].remote.version, '7')
        with self.assertRaises(AttributeError):
            self.snapshot.actions[0].remote.version = '8'

    def test_changed_action_or_index_requires_new_consent(self):
        self.prepare()
        self.selected.size += 1
        with self.assertRaises(guard.StaleSyncPlan):
            guard.approved_action(self.executor.consent, self.action)
        self.selected.size -= 1
        with self.assertRaises(guard.StaleSyncPlan):
            guard.approved_action(self.executor.consent, (4, self.selected))

    def test_approval_rejects_duplicate_target(self):
        self.prepare()
        with self.assertRaises(guard.StaleSyncPlan):
            guard.approve_sync_actions([self.action, (4, self.selected)], self.snapshot)

    def test_capture_rejects_cached_digest_even_with_restored_mtime(self):
        local, remote = self.prepare()
        old = self.path.stat()
        self.path.write_bytes(b'x' * len(self.local_bytes))
        os.utime(self.path, ns=(old.st_atime_ns, old.st_mtime_ns))
        with self.assertRaises(guard.StaleSyncPlan):
            guard.capture_scan_snapshot([self.selected], [local], [remote], self.folder,
                                        'project', 'project-id', {})

    def test_capture_rejects_missing_drive_version(self):
        local, remote = self.prepare()
        remote.version = None
        with self.assertRaises(guard.StaleSyncPlan):
            guard.capture_scan_snapshot([self.selected], [local], [remote], self.folder,
                                        'project', 'project-id', {})

    def test_same_bytes_replaced_local_identity_blocks(self):
        self.prepare()
        old = self.path.stat()
        replacement = self.root / 'replacement'
        replacement.write_bytes(self.local_bytes)
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        replacement.replace(self.path)
        self.run_action('upload_revision')
        self.assert_failed()
        self.assert_no_mutation()

    def test_all_remote_version_changes_block_revision(self):
        changes = [('version', '8'), ('md5Checksum', '0' * 32), ('size', '999'),
                   ('id', 'another'), ('name', 'renamed.sldprt'), ('parents', ['another']),
                   ('trashed', True)]
        for field, value in changes:
            with self.subTest(field=field):
                self.prepare()
                saved = self.responses['file-id'][field]
                self.responses['file-id'][field] = value
                self.run_action('upload_revision')
                self.assert_failed()
                self.assert_no_mutation()
                self.responses['file-id'][field] = saved
                self.signals.action_failed.calls.clear()

    def test_missing_metadata_and_read_failure_fail_closed(self):
        for failure in ('missing', 'permission'):
            with self.subTest(failure=failure):
                self.prepare()
                saved = self.responses['file-id'].pop('trashed') if failure == 'missing' else None
                if failure == 'permission':
                    self.files.get.side_effect = PermissionError('fixture')
                self.run_action('upload_revision')
                self.assert_failed()
                self.assert_no_mutation()
                self.assertEqual(self.executor.guard_failed.calls[-1], ('stale',))
                if failure == 'missing':
                    self.responses['file-id']['trashed'] = saved
                self.signals.action_failed.calls.clear()
        self.assertGreaterEqual(self.service.close.call_count, 2)

    def test_revision_upload_sends_only_approved_staged_bytes(self):
        self.prepare()
        def upload(staged, *args):
            self.path.write_bytes(b'a concurrent local edit')
            self.assertEqual(Path(staged).read_bytes(), self.local_bytes)
            self.assertFalse(Path(staged).stat().st_mode & 0o200)
            return 'file-id', digest(self.local_bytes)
        self.drive.upload_file.side_effect = upload
        self.run_action('upload_revision')
        self.assertEqual(self.signals.action_failed.calls, [])
        self.assertEqual(self.signals.action_complete.calls, [(self.selected,)])
        self.assertEqual(self.path.read_bytes(), b'a concurrent local edit')
        self.assertFalse(Path(self.drive.upload_file.call_args.args[0]).exists())

    def test_remote_update_during_upload_staging_blocks(self):
        self.prepare()
        real_fsync = os.fsync
        def update(fd):
            real_fsync(fd)
            self.responses['file-id']['version'] = '8'
        with patch.object(os, 'fsync', side_effect=update):
            self.run_action('upload_revision')
        self.assert_failed()
        self.assert_no_mutation()

    def test_local_update_during_download_is_preserved(self):
        self.prepare('download_revision')
        def compete(*args):
            returned = self.download(*args)
            self.path.write_bytes(b'local edit while downloading')
            return returned
        self.drive.download_file.side_effect = compete
        self.run_action('download_revision')
        self.assert_failed()
        self.assert_no_mutation()
        self.assertEqual(self.path.read_bytes(), b'local edit while downloading')
        self.assertEqual(list(self.root.glob('.cachecad-revision-download-*')), [])

    def test_remote_update_during_download_blocks_local_replace(self):
        self.prepare('download_revision')
        def compete(*args):
            returned = self.download(*args)
            self.responses['file-id']['version'] = '8'
            return returned
        self.drive.download_file.side_effect = compete
        self.run_action('download_revision')
        self.assert_failed()
        self.assert_no_mutation()
        self.assertEqual(self.path.read_bytes(), self.local_bytes)

    def test_ordinary_revision_download_still_works(self):
        self.prepare('download_revision')
        self.run_action('download_revision')
        self.assertEqual(self.signals.action_failed.calls, [])
        self.assertEqual(self.signals.action_complete.calls, [(self.selected,)])
        self.assertEqual(self.path.read_bytes(), self.remote_bytes)
        self.assertEqual(self.trash.call_count, 1)
        self.assertEqual(self.service.close.call_count, 2)

    def test_download_publication_never_clobbers_new_arrival(self):
        self.prepare('download_revision')
        def after_trash(path):
            Path(path).unlink()
            Path(path).write_bytes(b'new arrival after trash')
        self.trash.side_effect = after_trash
        self.run_action('download_revision')
        self.assert_failed()
        self.assertEqual(self.path.read_bytes(), b'new arrival after trash')

    def test_wrong_download_bytes_leave_original(self):
        self.prepare('download_revision')
        self.remote_bytes = b'wrong cloud bytes'
        self.run_action('download_revision')
        self.assert_failed()
        self.assert_no_mutation()
        self.assertEqual(self.path.read_bytes(), self.local_bytes)

    def test_local_delete_rechecks_cloud_absence(self):
        self.prepare('delete_local')
        self.files.list.return_value.execute.return_value = {'files': [{'id': 'new-file'}]}
        self.run_action('delete_local')
        self.assert_failed()
        self.assert_no_mutation()
        self.assertTrue(self.path.exists())

    def test_local_delete_incomplete_absence_blocks(self):
        self.prepare('delete_local')
        self.files.list.return_value.execute.return_value = {'files': [], 'incompleteSearch': True}
        self.run_action('delete_local')
        self.assert_failed()
        self.assert_no_mutation()

    def test_local_delete_unchanged_succeeds(self):
        self.prepare('delete_local')
        self.run_action('delete_local')
        self.assertEqual(self.signals.action_complete.calls, [(None,)])
        self.assertFalse(self.path.exists())
        self.assertIn("name = 'part.sldprt'", self.files.list.call_args.kwargs['q'])

    def test_local_delete_failure_has_exactly_one_terminal_event(self):
        self.prepare('delete_local')
        self.trash.side_effect = OSError('fixture locked file')
        self.run_action('delete_local')
        self.assert_failed()
        self.assertTrue(self.path.exists())

    def test_cloud_delete_rechecks_local_absence(self):
        self.prepare('delete_cloud')
        self.path.write_bytes(b'new local arrival')
        self.run_action('delete_cloud')
        self.assert_failed()
        self.assert_no_mutation()

    def test_cloud_delete_stale_remote_blocks(self):
        self.prepare('delete_cloud')
        self.responses['file-id']['version'] = '8'
        self.run_action('delete_cloud')
        self.assert_failed()
        self.assert_no_mutation()

    def test_cloud_delete_unchanged_succeeds(self):
        self.prepare('delete_cloud')
        self.run_action('delete_cloud')
        self.assertEqual(self.signals.action_complete.calls, [(None,)])
        self.drive.delete_file.assert_called_once_with('file-id')

    def test_cloud_delete_unconfirmed_fails_once(self):
        self.prepare('delete_cloud')
        self.drive.delete_file.return_value = False
        self.run_action('delete_cloud')
        self.assert_failed()

    def test_no_consent_blocks_all_destructive_actions(self):
        for kind in guard.DESTRUCTIVE_ACTIONS:
            with self.subTest(kind=kind):
                self.path.write_bytes(self.local_bytes)
                self.prepare(kind)
                self.executor.consent = None
                self.run_action(kind)
                self.assert_failed()
                self.assert_no_mutation()
                self.drive._new_service.assert_not_called()
                self.signals.action_failed.calls.clear()

    def test_cancelled_before_work_does_not_read_or_write(self):
        self.prepare()
        self.executor.cancelled = True
        self.run_action('upload_revision')
        self.assert_failed()
        self.drive._new_service.assert_not_called()
        self.assert_no_mutation()

    def test_all_guarded_actions_honor_cancel_during_preflight(self):
        for kind in sorted(guard.DESTRUCTIVE_ACTIONS | {'upload_new', 'download_new'}):
            with self.subTest(kind=kind):
                self.path.write_bytes(self.local_bytes)
                self.prepare(kind)
                self.executor.cancelled = False
                def cancel(**kwargs):
                    self.executor.cancelled = True
                    return self.get(**kwargs)
                self.files.get.side_effect = cancel
                self.run_action(kind)
                self.assert_failed()
                self.assert_no_mutation()
                self.drive.download_file.assert_not_called()
                self.signals.action_failed.calls.clear()

    def test_new_upload_checks_remote_absence(self):
        self.prepare('upload_new')
        self.files.list.return_value.execute.return_value = {'files': [{'id': 'competing-file'}]}
        self.run_action('upload_new')
        self.assert_failed()
        self.assert_no_mutation()
        self.assertEqual(self.executor.guard_failed.calls, [('stale',)])

    def test_new_upload_unchanged_still_works_with_consent(self):
        self.prepare('upload_new')
        self.drive.upload_file.side_effect = lambda *args: ('new-file', digest(self.local_bytes))
        self.run_action('upload_new')
        self.assertEqual(self.signals.action_complete.calls, [(self.selected,)])
        self.assertEqual(self.drive.upload_file.call_args.args[3], None)
        self.assertEqual(self.files.list.call_count, 1)

    def test_new_download_checks_source_version_after_transfer(self):
        self.prepare('download_new')
        def compete(*args):
            returned = self.download(*args)
            self.responses['file-id']['version'] = '8'
            return returned
        self.drive.download_file.side_effect = compete
        self.run_action('download_new')
        self.assert_failed()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.executor.guard_failed.calls, [('stale',)])

    def test_new_download_unchanged_still_works_with_consent(self):
        self.prepare('download_new')
        self.run_action('download_new')
        self.assertEqual(self.signals.action_complete.calls, [(self.selected,)])
        self.assertEqual(self.path.read_bytes(), self.remote_bytes)
        self.assertEqual(self.service.close.call_count, 2)

    def test_new_upload_accepts_only_its_verified_created_ancestors(self):
        self.path.unlink()
        self.path = self.root / 'parts' / 'nested' / 'part.sldprt'
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(self.local_bytes)
        self.prepare('upload_new')
        created = {}
        self.assertEqual(guard.verify_folder_creation(self.executor.consent, Path('parts'),
                                                     self.drive, created), 'project-id')
        self.responses['folder1'] = {'id': 'folder1', 'name': 'parts', 'parents': ['project-id'],
            'trashed': False, 'mimeType': 'application/vnd.google-apps.folder'}
        created['parts'] = 'folder1'
        self.assertEqual(guard.verify_folder_creation(self.executor.consent, Path('parts/nested'),
                                                     self.drive, created), 'folder1')
        self.responses['folder2'] = {'id': 'folder2', 'name': 'nested', 'parents': ['folder1'],
            'trashed': False, 'mimeType': 'application/vnd.google-apps.folder'}
        created[str(Path('parts/nested'))] = 'folder2'
        self.executor._created_folders = created
        self.executor.get_parent_id.return_value = 'folder2'
        self.drive.upload_file.side_effect = lambda *args: ('new-file', digest(self.local_bytes))
        self.run_action('upload_new')
        self.assertEqual(self.signals.action_complete.calls, [(self.selected,)])
        self.assertEqual(self.drive.upload_file.call_args.args[1], 'folder2')
        self.assertIn("'folder2' in parents", self.files.list.call_args.kwargs['q'])

    def test_new_upload_cannot_use_unapproved_parent_mapping(self):
        self.prepare('upload_new')
        self.executor.get_parent_id.return_value = 'another-folder'
        self.run_action('upload_new')
        self.assert_failed()
        self.assert_no_mutation()

    def test_upload_log_failure_settles_once_after_remote_success(self):
        self.prepare()
        self.executor.log_sync_action.side_effect = OSError('fixture log error')
        self.run_action('upload_revision')
        self.assertEqual(self.signals.action_failed.calls, [])
        self.assertEqual(self.signals.action_complete.calls, [(self.selected,)])
        self.assertEqual(self.executor.guard_failed.calls, [('committed_recording_failed',)])
        self.assertEqual(self.executor.action_results[0]['outcome'], 'committed_recording_failed')

    def test_download_publication_failure_uses_safe_recovery_code(self):
        self.prepare('download_revision')
        actual_link = os.link
        def fail_publication(source, destination, **kwargs):
            if Path(destination) == self.path:
                raise OSError('fixture unsupported filesystem')
            return actual_link(source, destination, **kwargs)
        with patch.object(os, 'link', side_effect=fail_publication):
            self.run_action('download_revision')
        self.assert_failed()
        self.assertEqual(self.executor.guard_failed.calls, [('outcome_unknown',)])
        self.assertEqual(self.executor.action_results[0]['outcome'], 'unknown')
        recovery = Path(self.executor.action_results[0]['recovery_dir'])
        self.assertEqual((recovery / 'backup.bin').read_bytes(), self.local_bytes)
        self.assertEqual((recovery / 'incoming.bin').read_bytes(), self.remote_bytes)
        self.assertEqual(self.signals.error_occurred.calls, [])


if __name__ == '__main__':
    unittest.main()
