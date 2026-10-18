"""Actual executor + durable journal, synthetic providers and disposable files.

These fault injections establish handling of specific returned/errors and retained
bytes. They are not live Drive, Windows installed-application, power-loss, or
compare-and-swap guarantees. No original application initialization executes.
"""
import errno
import ast
import csv
import hashlib
import logging
import os
from pathlib import Path
import stat
import tempfile
import threading
import traceback
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_maintained_sync_consent as fixtures
from test_recovered_overwrite import bind_recovery_executor
from test_recovered_overwrite import load_recovery_module
from recovered_harness import ROOT, deny_external_effects, load_function


class CommittedTransferRecoveryTests(unittest.TestCase):
    def fixture(self, kind='download_revision'):
        fixture = fixtures.ConsentTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare(kind)
        fixture.journal_globals = fixture.namespace['TransferJournal'].prepare_intent.__globals__
        fixture.operation_class = fixture.journal_globals['TransferOperation']
        return fixture

    def result(self, fixture, outcome):
        self.assertEqual(len(fixture.executor.action_results), 1)
        result = fixture.executor.action_results[0]
        self.assertEqual(result['outcome'], outcome)
        self.assertEqual(result['path'], fixture.selected.path)
        complete = outcome.startswith('committed')
        self.assertEqual(len(fixture.signals.action_complete.calls), int(complete))
        self.assertEqual(len(fixture.signals.action_failed.calls), int(not complete))
        return result

    def copies(self, fixture, result):
        directory = Path(result['recovery_dir'])
        self.assertEqual((directory / 'backup.bin').read_bytes(), fixture.local_bytes)
        self.assertEqual((directory / 'incoming.bin').read_bytes(), fixture.remote_bytes)
        return directory

    def test_durable_intent_failure_blocks_before_download_or_trash(self):
        fixture = self.fixture()
        with patch.object(fixture.namespace['TransferJournal'], 'prepare_intent', side_effect=OSError('disk full')):
            fixture.run_action('download_revision')
        self.result(fixture, 'not_started')
        self.assertEqual(fixture.path.read_bytes(), fixture.local_bytes)
        fixture.drive.download_file.assert_not_called()
        fixture.trash.assert_not_called()

    def test_incoming_copy_failure_leaves_original_untouched(self):
        fixture = self.fixture()
        with patch.object(fixture.operation_class, 'preserve_incoming', side_effect=OSError('disk full')):
            fixture.run_action('download_revision')
        self.result(fixture, 'not_started')
        fixture.trash.assert_not_called()
        self.assertEqual(fixture.path.read_bytes(), fixture.local_bytes)

    def test_backup_copy_failure_leaves_original_and_verified_incoming(self):
        fixture = self.fixture()
        with patch.object(fixture.operation_class, 'preserve_local_backup', side_effect=OSError('disk full')):
            fixture.run_action('download_revision')
        result = self.result(fixture, 'not_started')
        fixture.trash.assert_not_called()
        self.assertEqual(fixture.path.read_bytes(), fixture.local_bytes)
        self.assertEqual((Path(result['recovery_dir']) / 'incoming.bin').read_bytes(), fixture.remote_bytes)

    def test_unknown_intent_record_failure_prevents_trash(self):
        fixture = self.fixture()
        original = fixture.operation_class.record_outcome
        def fail_unknown(operation, outcome, **details):
            if outcome == 'unknown':
                raise OSError('injected journal write failure')
            return original(operation, outcome, **details)
        with patch.object(fixture.operation_class, 'record_outcome', fail_unknown):
            fixture.run_action('download_revision')
        result = self.result(fixture, 'not_started')
        self.copies(fixture, result)
        fixture.trash.assert_not_called()
        self.assertEqual(fixture.path.read_bytes(), fixture.local_bytes)

    def test_fsync_failure_prevents_target_mutation(self):
        fixture = self.fixture()
        with patch.object(os, 'fsync', side_effect=OSError('injected fsync failure')):
            fixture.run_action('download_revision')
        self.result(fixture, 'not_started')
        fixture.trash.assert_not_called()
        self.assertEqual(fixture.path.read_bytes(), fixture.local_bytes)

    def test_both_independent_readonly_copies_exist_before_trash(self):
        fixture = self.fixture()
        def trash(path):
            pending = fixture.executor._transfer_journal.list_pending()
            self.assertEqual(len(pending), 1)
            record = pending[0]
            self.assertEqual(record['state']['outcome'], 'unknown')
            self.assertEqual(record['state']['phase'], 'mutation_started')
            operation = fixture.executor._transfer_journal.open_operation(record['intent']['operation_id'])
            self.assertTrue(operation.verify_recovery_material())
            self.assertEqual(operation.backup_path.read_bytes(), fixture.local_bytes)
            self.assertEqual(operation.incoming_path.read_bytes(), fixture.remote_bytes)
            self.assertFalse(operation.backup_path.stat().st_mode & stat.S_IWUSR)
            self.assertFalse(operation.incoming_path.stat().st_mode & stat.S_IWUSR)
            self.assertNotEqual(operation.incoming_path.stat().st_ino, operation.publication_path.stat().st_ino)
            Path(path).unlink()
        fixture.trash.side_effect = trash
        fixture.run_action('download_revision')
        self.result(fixture, 'committed')
        self.assertEqual(fixture.path.read_bytes(), fixture.remote_bytes)

    def test_post_trash_link_errors_retain_both_versions(self):
        for code in (errno.ENOSPC, errno.EXDEV, errno.EACCES, errno.EINTR):
            with self.subTest(code=code):
                fixture = self.fixture()
                real_link = os.link
                def fail_publication(source, destination, **kwargs):
                    if Path(destination) == fixture.path:
                        raise OSError(code, 'injected publication failure')
                    return real_link(source, destination, **kwargs)
                with patch.object(os, 'link', side_effect=fail_publication):
                    fixture.run_action('download_revision')
                result = self.result(fixture, 'unknown')
                self.copies(fixture, result)
                self.assertFalse(fixture.path.exists())
                fixture.executor.log_sync_action.assert_not_called()
                self.assertEqual(fixture.executor.guard_failed.calls, [('outcome_unknown',)])

    def test_throwing_trash_before_or_after_move_retains_evidence(self):
        for moved in (False, True):
            with self.subTest(moved=moved):
                fixture = self.fixture()
                def trash(path):
                    if moved:
                        Path(path).unlink()
                    raise OSError('injected Trash failure')
                fixture.trash.side_effect = trash
                fixture.run_action('download_revision')
                self.copies(fixture, self.result(fixture, 'unknown'))
                self.assertEqual(fixture.path.exists(), not moved)

    def test_new_competitor_is_never_replaced_by_recovery(self):
        fixture = self.fixture()
        def trash(path):
            Path(path).unlink()
            Path(path).write_bytes(b'new competing CAD bytes')
        fixture.trash.side_effect = trash
        fixture.run_action('download_revision')
        self.copies(fixture, self.result(fixture, 'unknown'))
        self.assertEqual(fixture.path.read_bytes(), b'new competing CAD bytes')

    def test_confirmed_publication_survives_baseline_log_failure(self):
        fixture = self.fixture()
        fixture.executor.log_sync_action.side_effect = OSError('injected CSV failure')
        fixture.run_action('download_revision')
        result = self.result(fixture, 'committed_recording_failed')
        self.copies(fixture, result)
        self.assertEqual(fixture.path.read_bytes(), fixture.remote_bytes)
        self.assertEqual(fixture.executor.guard_failed.calls, [('committed_recording_failed',)])
        self.assertEqual(fixture.executor._transfer_journal.read(result['operation_id'])['state']['outcome'],
                         'committed_recording_failed')

    def test_completion_journal_failure_never_reports_mutation_failed(self):
        fixture = self.fixture()
        original = fixture.operation_class.record_outcome
        def fail_completion(operation, outcome, **details):
            if outcome.startswith('committed'):
                raise OSError('injected completion journal failure')
            return original(operation, outcome, **details)
        with patch.object(fixture.operation_class, 'record_outcome', fail_completion):
            fixture.run_action('download_revision')
        result = self.result(fixture, 'committed_recording_failed')
        self.copies(fixture, result)
        self.assertEqual(fixture.path.read_bytes(), fixture.remote_bytes)
        self.assertEqual(fixture.executor._transfer_journal.read(result['operation_id'])['state']['outcome'], 'unknown')

    def test_destination_edit_does_not_modify_retained_incoming(self):
        fixture = self.fixture()
        fixture.run_action('download_revision')
        result = self.result(fixture, 'committed')
        fixture.path.write_bytes(b'a later edit to the working CAD file')
        self.copies(fixture, result)
        self.assertEqual(fixture.executor._transfer_journal.list_pending(), ())

    def test_directory_flush_after_link_still_reports_committed(self):
        fixture = self.fixture()
        original = fixture.journal_globals['fsync_directory']
        def fail_published_directory(path):
            if Path(path) == fixture.root and fixture.path.exists() and fixture.path.read_bytes() == fixture.remote_bytes:
                raise OSError('injected post-link directory flush failure')
            return original(path)
        with patch.dict(fixture.journal_globals, fsync_directory=fail_published_directory):
            fixture.run_action('download_revision')
        result = self.result(fixture, 'committed')
        self.assertTrue(result['warnings'])
        self.copies(fixture, result)
        self.assertEqual(fixture.path.read_bytes(), fixture.remote_bytes)

    def test_cleanup_failure_does_not_discard_confirmed_commit(self):
        fixture = self.fixture()
        real_directory = tempfile.TemporaryDirectory
        class CleanupFailure:
            def __init__(self, *args, **kwargs):
                self.original = real_directory(*args, **kwargs)
            def __enter__(self):
                return self.original.__enter__()
            def __exit__(self, kind, error, tb):
                self.original.__exit__(kind, error, tb)
                if kind is None:
                    raise PermissionError('injected cleanup failure')
        with patch.object(tempfile, 'TemporaryDirectory', CleanupFailure):
            fixture.run_action('download_revision')
        result = self.result(fixture, 'committed')
        self.assertTrue(result['warnings'])
        self.copies(fixture, result)

    def test_lost_upload_response_is_unknown_and_not_retried(self):
        fixture = self.fixture('upload_revision')
        fixture.drive.upload_file.return_value = (None, None)
        fixture.drive.upload_file.side_effect = None
        fixture.run_action('upload_revision')
        result = self.result(fixture, 'unknown')
        self.assertEqual((Path(result['recovery_dir']) / 'incoming.bin').read_bytes(), fixture.local_bytes)
        self.assertEqual(fixture.drive.upload_file.call_count, 1)
        # A fresh executor/consent run may read versions, but cannot replay the
        # mutation while the previous same-target operation remains unresolved.
        bind_recovery_executor(fixture.executor, fixture.namespace)
        fixture.signals.action_failed.calls.clear()
        fixture.run_action('upload_revision')
        self.result(fixture, 'not_started')
        self.assertEqual(fixture.drive.upload_file.call_count, 1)
        self.assertEqual(fixture.executor.guard_failed.calls[-1], ('recovery_required',))

    def test_wrong_revision_response_identity_never_confirms_commit(self):
        fixture = self.fixture('upload_revision')
        fixture.drive.upload_file.side_effect = None
        fixture.drive.upload_file.return_value = ('another-id', fixtures.digest(fixture.local_bytes))
        fixture.run_action('upload_revision')
        self.result(fixture, 'unknown')
        fixture.executor.log_sync_action.assert_not_called()

    def test_unknown_new_upload_retains_exact_dispatched_parent(self):
        fixture = self.fixture('upload_new')
        fixture.drive.upload_file.side_effect = None
        fixture.drive.upload_file.return_value = (None, None)
        fixture.run_action('upload_new')
        result = self.result(fixture, 'unknown')
        record = fixture.executor._transfer_journal.read(result['operation_id'])
        dispatch = next(entry for entry in record['state']['history']
                        if entry['phase'] == 'mutation_started' and 'parent_id' in entry['details'])
        self.assertEqual(dispatch['details']['parent_id'], fixture.drive.upload_file.call_args.args[1])
        self.assertEqual(dispatch['details']['parent_id'], 'project-id')
        self.assertIsNone(dispatch['details']['requested_drive_id'])

    def test_confirmed_upload_log_failure_is_recording_failure(self):
        fixture = self.fixture('upload_revision')
        fixture.executor.log_sync_action.side_effect = OSError('injected CSV failure')
        fixture.run_action('upload_revision')
        result = self.result(fixture, 'committed_recording_failed')
        self.assertEqual((Path(result['recovery_dir']) / 'incoming.bin').read_bytes(), fixture.local_bytes)
        self.assertEqual(fixture.drive.upload_file.call_count, 1)

    def test_local_delete_keeps_backup_and_reports_commit_despite_log_failure(self):
        fixture = self.fixture('delete_local')
        fixture.executor.log_sync_action.side_effect = OSError('injected CSV failure')
        fixture.run_action('delete_local')
        result = self.result(fixture, 'committed_recording_failed')
        self.assertEqual((Path(result['recovery_dir']) / 'backup.bin').read_bytes(), fixture.local_bytes)
        self.assertFalse(fixture.path.exists())

    def test_cloud_delete_unconfirmed_result_is_unknown(self):
        fixture = self.fixture('delete_cloud')
        fixture.drive.delete_file.return_value = False
        fixture.run_action('delete_cloud')
        self.result(fixture, 'unknown')
        self.assertEqual(fixture.drive.delete_file.call_count, 1)
        fixture.executor.log_sync_action.assert_not_called()

    def test_result_is_available_before_terminal_callback(self):
        fixture = self.fixture()
        original = fixture.signals.action_complete.emit
        def observe(*args):
            self.assertEqual(len(fixture.executor.action_results), 1)
            self.assertEqual(fixture.executor.action_results[0]['outcome'], 'committed')
            original(*args)
        fixture.signals.action_complete.emit = observe
        fixture.run_action('download_revision')
        self.result(fixture, 'committed')

    def bind_real_log(self, fixture):
        fixture.executor.sync_log_lock = threading.Lock()
        fixture.executor.sync_log_path = fixture.root / '.cachecad' / 'sync_log.csv'
        fixture.executor.sync_log_path.parent.mkdir(exist_ok=True)
        namespace = dict(fixture.namespace, csv=csv, stat=stat,
                         fsync_directory=fixture.journal_globals['fsync_directory'])
        method = load_function('maintained/core/sync_executor.py', 'SyncExecutor.log_sync_action', namespace)
        fixture.executor.log_sync_action = MethodType(method, fixture.executor)
        fixture.selected.get_log_tuple = lambda: (fixture.selected.drive_id, fixture.selected.name,
            fixture.selected.path, fixture.selected.md5_checksum, 0, fixture.selected.size,
            '2026-01-01T00:00:00', fixture.selected.action.value)
        return fixture.executor.sync_log_path

    def test_hardlinked_sync_log_is_not_appended_after_confirmed_commit(self):
        fixture = self.fixture()
        log_path = self.bind_real_log(fixture)
        external = fixture.folder / 'unrelated.csv'
        external.write_bytes(b'unrelated external CSV data\n')
        os.link(external, log_path)
        fixture.run_action('download_revision')
        self.result(fixture, 'committed_recording_failed')
        self.assertEqual(external.read_bytes(), b'unrelated external CSV data\n')
        self.assertEqual(fixture.path.read_bytes(), fixture.remote_bytes)

    def test_symlinked_sync_log_is_not_followed(self):
        fixture = self.fixture()
        log_path = self.bind_real_log(fixture)
        external = fixture.folder / 'unrelated.csv'
        external.write_bytes(b'unrelated external CSV data\n')
        try:
            log_path.symlink_to(external)
        except OSError:
            self.skipTest('This fixture filesystem does not permit symlinks')
        fixture.run_action('download_revision')
        self.result(fixture, 'committed_recording_failed')
        self.assertEqual(external.read_bytes(), b'unrelated external CSV data\n')

    def test_log_swap_before_descriptor_open_is_rejected_without_append(self):
        fixture = self.fixture()
        log_path = self.bind_real_log(fixture)
        log_path.write_bytes(b'original log\n')
        saved = fixture.folder / 'saved-log.csv'
        real_open = os.open
        def swap_before_open(path, flags, *args, **kwargs):
            if Path(path) == log_path and flags & os.O_APPEND:
                log_path.rename(saved)
                log_path.write_bytes(b'competing log\n')
            return real_open(path, flags, *args, **kwargs)
        with patch.object(os, 'open', side_effect=swap_before_open):
            fixture.run_action('download_revision')
        self.result(fixture, 'committed_recording_failed')
        self.assertEqual(saved.read_bytes(), b'original log\n')
        self.assertEqual(log_path.read_bytes(), b'competing log\n')

    def test_plain_log_create_and_append_use_validated_descriptor(self):
        fixture = self.fixture()
        log_path = self.bind_real_log(fixture)
        fixture.run_action('download_revision')
        self.result(fixture, 'committed')
        fixture.executor.log_sync_action(fixture.action)
        with log_path.open(newline='', encoding='utf-8') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], 'drive_id')
        self.assertEqual(rows[1], rows[2])

    def prepare_open_file_precheck(self, fixture, partial=False):
        kinds = SimpleNamespace(**{value.upper(): value for value in (
            'upload_new', 'download_new', 'upload_revision', 'download_revision',
            'upload_unknown', 'download_unknown', 'delete_local', 'delete_cloud')})
        fixture.selected.action = kinds.DOWNLOAD_REVISION
        scheduled = SimpleNamespace(path='other.sldprt', action=kinds.DOWNLOAD_NEW)
        fixture.executor.action_tuple = [fixture.action] + ([(4, scheduled)] if partial else [])
        fixture.executor.dropped_actions = []
        fixture.executor.folders_to_create = []
        fixture.executor.local_client = SimpleNamespace(is_file_open=Mock(return_value=True))
        fixture.executor._finished = False
        fixture.executor._started = False
        fixture.executor._counted_actions = set()
        fixture.executor.complete_action_count = 0
        fixture.executor.failed_action_count = 0
        fixture.executor.cancelled_action_count = 0
        fixture.executor.completed_actions = []
        fixture.executor._finish_execution = Mock()
        namespace = dict(fixture.namespace, SyncActionType=kinds)
        for name in ('pre_sync_checks', 'on_transfer_outcome', 'check_if_all_actions_finished', 'start_sync'):
            method = load_function('maintained/core/sync_executor.py', 'SyncExecutor.' + name, namespace)
            setattr(fixture.executor, name, MethodType(method, fixture.executor))
        fixture.executor.pre_sync_checks()
        return scheduled

    def test_all_open_file_drops_retain_not_started_outcomes_and_finish(self):
        fixture = self.fixture()
        self.prepare_open_file_precheck(fixture)
        fixture.executor.consent = None
        fixture.executor.start_sync()
        self.assertEqual(fixture.executor.action_tuple, [])
        self.assertEqual(len(fixture.executor.action_results), 1)
        result = fixture.executor.action_results[0]
        self.assertEqual((result['action_index'], result['path'], result['outcome']),
                         (3, fixture.selected.path, 'not_started'))
        self.assertIn('Local file is open', result['warnings'][0])
        fixture.executor._finish_execution.assert_called_once_with()
        self.assertEqual(fixture.signals.action_failed.calls, [])
        self.assertEqual(fixture.executor.failed_action_count, 0)

    def test_partial_open_file_drop_does_not_count_against_scheduled_workers(self):
        fixture = self.fixture()
        scheduled = self.prepare_open_file_precheck(fixture, partial=True)
        fixture.executor.on_transfer_outcome(fixture.executor.action_results[0])
        self.assertEqual(fixture.executor.failed_action_count, 0)
        fixture.executor._finish_execution.assert_not_called()
        result = fixture.executor._settle_transfer((4, scheduled), None, 'committed', write_log=False)
        fixture.executor.on_transfer_outcome(result)
        self.assertEqual(len(fixture.executor.action_results), 2)
        self.assertEqual(fixture.executor.complete_action_count, 1)
        fixture.executor._finish_execution.assert_called_once_with()


class RealQtExecutorOutcomeTests(unittest.TestCase):
    def test_worker_outcomes_settle_on_qt_owner_thread(self):
        try:
            from PySide6 import QtCore
        except ImportError:
            self.skipTest('Real Qt is verified separately with the locked application runtime')
        self.enterContext(deny_external_effects())
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        root = folder / 'project'
        root.mkdir()
        app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
        class Manager(QtCore.QObject):
            dropped_actions = QtCore.Signal(object)
            dropped_actions_acknowledged = QtCore.Signal()
            action_complete = QtCore.Signal(object)
            action_failed = QtCore.Signal()
            sync_cancelled = QtCore.Signal()
            sync_execution_finished = QtCore.Signal()
        manager = Manager()
        pool = QtCore.QThreadPool()
        pool.setMaxThreadCount(4)
        journal = load_recovery_module()
        kinds = SimpleNamespace(**{value.upper(): value for value in (
            'upload_new', 'download_new', 'upload_revision', 'download_revision',
            'upload_unknown', 'download_unknown', 'delete_local', 'delete_cloud')})
        namespace = {name: getattr(QtCore, name) for name in (
            'QObject', 'Signal', 'QRunnable', 'QEventLoop', 'Qt', 'QTimer')}
        namespace.update(vars(fixtures.guard))
        namespace.update(signal_manager=manager, SyncActionType=kinds,
            CaseInsensitivePathDict=dict,
            get_project_app_folder=lambda working, project: Path(working) / project / '.cachecad',
            TransferJournal=journal.TransferJournal, PendingTransferRecovery=journal.PendingTransferRecovery,
            fsync_directory=journal.fsync_directory,
            network_pool=pool, thread_pool=pool, logging=logging, Path=Path, threading=threading,
            os=os, csv=csv, traceback=traceback, SimpleNamespace=SimpleNamespace)
        source = ROOT / 'maintained/core/sync_executor.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name in ('SyncExecutor', '_FunctionRunnable')]
        # Execute only the reviewed actual classes with explicit local adapters,
        # not the recovered application's module imports or initialization.
        exec(compile(ast.Module(body=classes, type_ignores=[]), str(source), 'exec'), namespace)
        contents = {0: b'first synthetic part', 1: b'second synthetic part'}
        def download(file_id, requested, index):
            destination = Path(requested)
            destination.write_bytes(contents[index])
            return destination
        selected = []
        for index in contents:
            item = SimpleNamespace(action=kinds.DOWNLOAD_NEW, path=f'part{index}.sldprt',
                name=f'part{index}.sldprt', drive_id=f'file-{index}', folder=None,
                size=len(contents[index]), md5_checksum=hashlib.md5(contents[index]).hexdigest())
            item.get_log_tuple = lambda item=item: (item.drive_id, item.name, item.path,
                item.md5_checksum, 0, item.size, '2026-01-01T00:00:00', 'download_new')
            selected.append((index, item))
        executor = namespace['SyncExecutor'](SimpleNamespace(download_file=download),
            SimpleNamespace(), 'project', 'project-id', str(folder), selected, {})
        finished = []
        def capture_finish():
            finished.append((QtCore.QThread.currentThread() == app.thread(),
                list(executor.action_results), executor.complete_action_count,
                executor.failed_action_count, executor.cancelled_action_count))
            app.quit()
        manager.sync_execution_finished.connect(capture_finish)
        manager.dropped_actions.connect(lambda dropped: manager.dropped_actions_acknowledged.emit())
        timer = QtCore.QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(app.quit)
        timer.start(5000)
        app.exec()
        timer.stop()
        self.assertTrue(pool.waitForDone(5000))
        self.assertEqual(len(finished), 1, 'Real Qt executor did not settle its batch')
        owner_thread, results, complete, failed, cancelled = finished[0]
        self.assertTrue(owner_thread)
        self.assertEqual((complete, failed, cancelled), (2, 0, 0))
        self.assertEqual(sorted(result['action_index'] for result in results), [0, 1])
        self.assertTrue(all(result['outcome'] == 'committed' for result in results))
        for index, content in contents.items():
            self.assertEqual((root / f'part{index}.sldprt').read_bytes(), content)


if __name__ == '__main__':
    unittest.main()
