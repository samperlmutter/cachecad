"""Offline fault injections into the actual maintained transfer methods.

Tests named ``test_unsafe_*`` deliberately characterize CURRENT DEFECTS. Their
passing result is evidence that the unsafe outcome is reproducible, not a safety
certification. Replace their assertions with recovery invariants when fixed.
Tests named ``test_safe_*`` cover boundaries already enforced by the application.

Only disposable fixture files, fake provider responses, and unchanged maintained
function bodies run. The actual consent/staging helpers are exercised where
relevant. There is no live account, OAuth, CAD, Qt initialization, or network.
Injected exceptions are not equivalent to a real power-loss durability test.
"""
import errno
import json
import os
from pathlib import Path
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from recovered_harness import ROOT, deny_external_effects, load_function
import test_maintained_sync_consent as consent_fixtures
import test_recovered_transfer_cleanup as provider_fixtures
import test_sync_history_recovery as history_fixtures
from test_recovered_overwrite import bind_recovery_executor


EXECUTOR = 'maintained/core/sync_executor.py'
PROVIDER = 'maintained/core/drive_client.py'
ANALYZER = 'maintained/core/sync_analyzer.py'
ACTION_KINDS = SimpleNamespace(**{name.upper(): name for name in (
    'upload_new', 'download_new', 'upload_revision', 'download_revision',
    'delete_local', 'delete_cloud', 'upload_unknown', 'download_unknown')})


class Signal:
    """Deterministic signal adapter; no claim about Qt delivery/thread behavior."""

    def __init__(self):
        self.calls = []
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)

    def emit(self, *args):
        self.calls.append(args)
        for slot in list(self.slots):
            slot(*args)


class CleanupFailureDirectory:
    """Inject an error on successful context exit, after actual cleanup."""

    def __init__(self, real_directory):
        self.directory = real_directory

    def __enter__(self):
        return self.directory.__enter__()

    def __exit__(self, kind, error, tb):
        result = self.directory.__exit__(kind, error, tb)
        if kind is None:
            raise PermissionError('Injected temporary-directory cleanup failure')
        return result


@unittest.skipUnless((ROOT / EXECUTOR).is_file(), 'Maintained source fixtures absent')
class TransferPublicationRecoveryTests(unittest.TestCase):
    def fixture(self, kind):
        # Compose, rather than inherit, the existing disposable fixture. This
        # avoids rerunning its unrelated tests while retaining the REAL guards.
        fixture = consent_fixtures.ConsentTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare(kind)
        fixture.trash_path = fixture.folder / 'simulated-recycle-bin' / fixture.path.name
        fixture.trash_path.parent.mkdir()
        fixture.trash.side_effect = lambda path: Path(path).rename(fixture.trash_path)
        return fixture

    def assert_failed_once(self, fixture):
        self.assertEqual(fixture.signals.action_failed.calls, [()])
        self.assertEqual(fixture.signals.action_complete.calls, [])

    def test_safe_revision_link_failure_retains_both_verified_recovery_copies(self):
        for code in (errno.ENOSPC, errno.EXDEV, errno.EACCES, errno.EINTR):
            with self.subTest(errno=code):
                fixture = self.fixture('download_revision')
                observed_staging = []
                actual_link = os.link

                def fail_link(source, destination, **kwargs):
                    if Path(destination) != fixture.path:
                        return actual_link(source, destination, **kwargs)
                    source = Path(source)
                    self.assertEqual(source.read_bytes(), fixture.remote_bytes)
                    self.assertFalse(Path(destination).exists())
                    self.assertEqual(fixture.trash_path.read_bytes(), fixture.local_bytes)
                    observed_staging.append(source)
                    raise OSError(code, 'Injected publication failure')

                with patch.object(os, 'link', side_effect=fail_link):
                    fixture.run_action('download_revision')
                self.assert_failed_once(fixture)
                self.assertFalse(fixture.path.exists())
                self.assertEqual(fixture.trash_path.read_bytes(), fixture.local_bytes)
                self.assertEqual(len(observed_staging), 1)
                self.assertEqual(observed_staging[0].read_bytes(), fixture.remote_bytes)
                result = fixture.executor.action_results[0]
                self.assertEqual(result['outcome'], 'unknown')
                recovery = Path(result['recovery_dir'])
                self.assertEqual((recovery / 'backup.bin').read_bytes(), fixture.local_bytes)
                self.assertEqual((recovery / 'incoming.bin').read_bytes(), fixture.remote_bytes)
                fixture.executor.log_sync_action.assert_not_called()

    def test_safe_revision_destination_race_preserves_competitor_and_recovery(self):
        fixture = self.fixture('download_revision')

        def competing_writer(path):
            Path(path).rename(fixture.trash_path)
            Path(path).write_bytes(b'competing writer must be preserved')

        fixture.trash.side_effect = competing_writer
        fixture.run_action('download_revision')
        self.assert_failed_once(fixture)
        self.assertEqual(fixture.path.read_bytes(), b'competing writer must be preserved')
        self.assertEqual(fixture.trash_path.read_bytes(), fixture.local_bytes)
        self.assertEqual(list(fixture.root.glob('.cachecad-revision-download-*')), [])
        recovery = Path(fixture.executor.action_results[0]['recovery_dir'])
        self.assertEqual((recovery / 'incoming.bin').read_bytes(), fixture.remote_bytes)
        self.assertEqual((recovery / 'backup.bin').read_bytes(), fixture.local_bytes)

    def test_safe_published_download_retains_committed_outcome_when_sync_log_fails(self):
        for kind in ('download_new', 'download_revision'):
            with self.subTest(kind=kind):
                fixture = self.fixture(kind)
                fixture.executor.log_sync_action.side_effect = OSError(errno.ENOSPC, 'Log full')
                fixture.run_action(kind)
                self.assertEqual(fixture.path.read_bytes(), fixture.remote_bytes)
                self.assertEqual(fixture.signals.action_failed.calls, [])
                self.assertEqual(fixture.signals.action_complete.calls, [(fixture.selected,)])
                self.assertEqual(fixture.executor.action_results[0]['outcome'],
                                 'committed_recording_failed')
                fixture.executor.log_sync_action.assert_called_once_with(fixture.action)

    def test_safe_published_download_retains_commit_and_warning_when_cleanup_fails(self):
        real_directory = tempfile.TemporaryDirectory
        for kind in ('download_new', 'download_revision'):
            with self.subTest(kind=kind):
                fixture = self.fixture(kind)

                def cleanup_failure(*args, **kwargs):
                    kwargs.setdefault('dir', str(fixture.folder))
                    return CleanupFailureDirectory(real_directory(*args, **kwargs))

                with patch.object(tempfile, 'TemporaryDirectory', side_effect=cleanup_failure):
                    fixture.run_action(kind)
                self.assertEqual(fixture.path.read_bytes(), fixture.remote_bytes)
                self.assertEqual(fixture.signals.action_failed.calls, [])
                self.assertEqual(fixture.signals.action_complete.calls, [(fixture.selected,)])
                self.assertEqual(fixture.executor.action_results[0]['outcome'], 'committed')
                self.assertTrue(fixture.executor.action_results[0]['warnings'])
                fixture.executor.log_sync_action.assert_called_once_with(fixture.action)

    def test_safe_confirmed_upload_retains_committed_outcome_when_sync_log_fails(self):
        for kind in ('upload_new', 'upload_revision'):
            with self.subTest(kind=kind):
                fixture = self.fixture(kind)
                committed = []

                def commit(path, parent, index, existing_id):
                    committed.append(Path(path).read_bytes())
                    return existing_id or 'created-id', consent_fixtures.digest(fixture.local_bytes)

                fixture.drive.upload_file.side_effect = commit
                fixture.executor.log_sync_action.side_effect = OSError(errno.ENOSPC, 'Log full')
                fixture.run_action(kind)
                self.assertEqual(committed, [fixture.local_bytes])
                self.assertEqual(fixture.path.read_bytes(), fixture.local_bytes)
                self.assertEqual(fixture.signals.action_failed.calls, [])
                self.assertEqual(fixture.signals.action_complete.calls, [(fixture.selected,)])
                self.assertEqual(fixture.executor.action_results[0]['outcome'],
                                 'committed_recording_failed')

    def test_safe_confirmed_upload_retains_commit_and_warning_when_staging_cleanup_fails(self):
        real_directory = tempfile.TemporaryDirectory
        for kind in ('upload_new', 'upload_revision'):
            with self.subTest(kind=kind):
                fixture = self.fixture(kind)
                committed = []

                def commit(path, parent, index, existing_id):
                    committed.append(Path(path).read_bytes())
                    return existing_id or 'created-id', consent_fixtures.digest(fixture.local_bytes)

                def cleanup_failure(*args, **kwargs):
                    kwargs.setdefault('dir', str(fixture.folder))
                    return CleanupFailureDirectory(real_directory(*args, **kwargs))

                fixture.drive.upload_file.side_effect = commit
                with patch.object(tempfile, 'TemporaryDirectory', side_effect=cleanup_failure):
                    fixture.run_action(kind)
                self.assertEqual(committed, [fixture.local_bytes])
                self.assertEqual(fixture.signals.action_failed.calls, [])
                self.assertEqual(fixture.signals.action_complete.calls, [(fixture.selected,)])
                self.assertEqual(fixture.executor.action_results[0]['outcome'], 'committed')
                self.assertTrue(fixture.executor.action_results[0]['warnings'])
                self.assertEqual(fixture.executor.log_sync_action.call_count, 1)

    def test_safe_actual_provider_lost_create_ack_is_unknown_and_new_run_cannot_replay(self):
        fixture = self.fixture('upload_new')
        provider = provider_fixtures.TransferFixture()
        committed = []

        def commit_then_lose_response():
            committed.append(fixture.local_bytes)
            raise TimeoutError('Server committed; acknowledgment lost')

        provider.request.next_chunk.side_effect = commit_then_lose_response
        fixture.drive.upload_file.side_effect = lambda *args: provider.upload(provider.client, *args)
        fixture.run_action('upload_new')
        self.assert_failed_once(fixture)
        self.assertEqual(fixture.executor.action_results[0]['outcome'], 'unknown')
        self.assertEqual(committed, [fixture.local_bytes])
        fixture.executor.log_sync_action.assert_not_called()
        # A fresh executor still sees the durable pending record on this path.
        bind_recovery_executor(fixture.executor, fixture.namespace)
        fixture.signals.action_failed.calls.clear()
        fixture.run_action('upload_new')
        self.assert_failed_once(fixture)
        self.assertEqual(fixture.executor.action_results[0]['outcome'], 'not_started')
        self.assertEqual(fixture.executor.action_results[0]['error_type'], 'PendingTransferRecovery')
        self.assertEqual(committed, [fixture.local_bytes])
        self.assertEqual(provider.files.create.call_count, 1)

    def test_safe_partial_revision_download_never_trashes_or_succeeds(self):
        fixture = self.fixture('download_revision')

        def partial(*args):
            staged = fixture.download(*args)
            staged.write_bytes(fixture.remote_bytes[:5])
            return staged

        fixture.drive.download_file.side_effect = partial
        fixture.run_action('download_revision')
        self.assertEqual(fixture.path.read_bytes(), fixture.local_bytes)
        self.assert_failed_once(fixture)
        fixture.trash.assert_not_called()

    def test_safe_cancelled_revision_download_never_trashes_or_succeeds(self):
        fixture = self.fixture('download_revision')

        def cancel_after_download(*args):
            staged = fixture.download(*args)
            fixture.executor.cancelled = True
            return staged

        fixture.drive.download_file.side_effect = cancel_after_download
        fixture.run_action('download_revision')
        self.assertEqual(fixture.path.read_bytes(), fixture.local_bytes)
        self.assert_failed_once(fixture)
        fixture.trash.assert_not_called()

    def test_safe_new_download_link_failure_never_replaces_existing_data(self):
        fixture = self.fixture('download_new')
        actual_link = os.link
        def fail_publication(source, destination, **kwargs):
            if Path(destination) == fixture.path:
                raise OSError(errno.ENOSPC, 'Injected full disk')
            return actual_link(source, destination, **kwargs)
        with patch.object(os, 'link', side_effect=fail_publication):
            fixture.run_action('download_new')
        self.assertFalse(fixture.path.exists())
        self.assert_failed_once(fixture)
        fixture.trash.assert_not_called()


@unittest.skipUnless((ROOT / PROVIDER).is_file(), 'Maintained source fixtures absent')
class RemoteMutationOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / 'part.sldprt'
        self.path.write_bytes(b'approved fixture bytes')

    def test_unsafe_lost_create_response_is_none_and_manual_retry_creates_duplicate(self):
        fixture = provider_fixtures.TransferFixture()
        remote_objects = []

        def commit_then_lose_first_response():
            remote_objects.append(dict(id=f'created-{len(remote_objects) + 1}',
                                       bytes=self.path.read_bytes()))
            if len(remote_objects) == 1:
                raise TimeoutError('Response lost after the server committed')
            return None, dict(id=remote_objects[-1]['id'], md5Checksum='fixture-md5')

        fixture.request.next_chunk.side_effect = commit_then_lose_first_response
        first = fixture.upload(fixture.client, str(self.path), 'parent')
        self.assertIsNone(first)
        self.assertEqual(len(remote_objects), 1)
        self.assertEqual(fixture.files.create.call_count, 1)  # No automatic app retry.
        second = fixture.upload(fixture.client, str(self.path), 'parent')
        self.assertEqual(second, ('created-2', 'fixture-md5'))
        self.assertEqual(len(remote_objects), 2)
        for call in fixture.files.create.call_args_list:
            self.assertEqual(call.kwargs['body'], {'name': self.path.name,
                'parents': ['parent'], 'keepRevisionForever': True})

    def test_unsafe_lost_revision_response_is_none_and_retry_commits_another_revision(self):
        fixture = provider_fixtures.TransferFixture()
        committed_revisions = []

        def commit_then_timeout():
            committed_revisions.append(self.path.read_bytes())
            raise TimeoutError('Revision committed; response lost')

        fixture.request.next_chunk.side_effect = commit_then_timeout
        for _ in range(2):
            self.assertIsNone(fixture.upload(fixture.client, str(self.path), 'parent',
                                             existing_drive_id='existing-id'))
        self.assertEqual(committed_revisions, [self.path.read_bytes()] * 2)
        self.assertEqual(fixture.files.update.call_count, 2)

    def test_unsafe_precommit_and_postcommit_transport_errors_have_same_result(self):
        observed = []
        for commits in (False, True):
            fixture = provider_fixtures.TransferFixture()
            remote = []

            def fail():
                if commits:
                    remote.append('committed')
                raise TimeoutError('Transport interrupted')

            fixture.request.next_chunk.side_effect = fail
            result = fixture.upload(fixture.client, str(self.path), 'parent')
            observed.append((result, list(remote)))
        self.assertEqual(observed, [(None, []), (None, ['committed'])])

    def test_unsafe_lost_folder_create_response_and_retry_make_two_folders(self):
        fixture = provider_fixtures.TransferFixture()
        folders = []

        def commit_then_timeout():
            folders.append('parts')
            raise TimeoutError('Folder created; response lost')

        fixture.files.create.return_value = SimpleNamespace(execute=commit_then_timeout)
        create = load_function(PROVIDER, 'DriveClient.create_folder', dict(
            logging=Mock(), traceback=SimpleNamespace(format_exc=lambda: 'fixture'),
            json=json, HttpError=provider_fixtures.FakeHttpError, signal_manager=fixture.signals))
        for _ in range(2):
            self.assertIsNone(create(fixture.client, 'parts', 'parent'))
        self.assertEqual(folders, ['parts', 'parts'])
        self.assertEqual(fixture.files.create.call_count, 2)

    def test_baseline_progress_callback_error_hides_confirmed_remote_upload_result(self):
        fixture = provider_fixtures.TransferFixture(provider_fixtures.BASELINE)
        fixture.signals.file_progress.emit = Mock(side_effect=RuntimeError('UI callback failed'))
        self.assertIsNone(fixture.upload(fixture.client, str(self.path), 'parent', idx=3))
        fixture.request.next_chunk.assert_called_once_with()
        self.assertEqual(fixture.request.next_chunk.return_value[1]['id'], 'created')

    def test_safe_progress_callback_error_preserves_confirmed_remote_upload_result(self):
        fixture = provider_fixtures.TransferFixture()
        fixture.signals.file_progress.emit = Mock(side_effect=RuntimeError('UI callback failed'))
        self.assertEqual(fixture.upload(fixture.client, str(self.path), 'parent', idx=3),
                         ('created', 'fixture-md5'))
        fixture.request.next_chunk.assert_called_once_with()
        self.assertEqual(fixture.stream.close_calls, 1)
        self.assertEqual(fixture.signals.error_occurred.calls, [])
        fixture.logger.exception.assert_called_once()

    def test_safe_intermediate_progress_callback_failure_does_not_abort_or_retry_chunks(self):
        fixture = provider_fixtures.TransferFixture()
        fixture.request.next_chunk.side_effect = [
            (SimpleNamespace(progress=lambda: 0.5), None),
            (None, {'id': 'created', 'md5Checksum': 'fixture-md5'}),
        ]
        fixture.signals.file_progress.emit = Mock(side_effect=RuntimeError('Progress UI failed'))
        self.assertEqual(fixture.upload(fixture.client, str(self.path), 'parent', idx=3),
                         ('created', 'fixture-md5'))
        self.assertEqual(fixture.request.next_chunk.call_count, 2)
        self.assertEqual(fixture.files.create.call_count, 1)
        self.assertEqual(fixture.signals.error_occurred.calls, [])
        self.assertEqual(fixture.stream.close_calls, 1)

    def test_safe_progress_status_error_cannot_hide_response_from_same_chunk(self):
        fixture = provider_fixtures.TransferFixture()
        fixture.request.next_chunk.return_value = (
            SimpleNamespace(progress=Mock(side_effect=ValueError('Bad progress status'))),
            {'id': 'created', 'md5Checksum': 'fixture-md5'})
        self.assertEqual(fixture.upload(fixture.client, str(self.path), 'parent', idx=3),
                         ('created', 'fixture-md5'))
        fixture.request.next_chunk.assert_called_once_with()
        self.assertEqual(fixture.signals.file_progress.calls, [(3, 100.0)])
        self.assertEqual(fixture.signals.error_occurred.calls, [])

    def test_safe_cancel_requested_by_completion_callback_does_not_erase_commit(self):
        fixture = provider_fixtures.TransferFixture()

        def cancel_and_raise(*args):
            fixture.client.cancelled = True
            raise RuntimeError('Completion UI failed while cancelling')

        fixture.signals.file_progress.emit = Mock(side_effect=cancel_and_raise)
        self.assertEqual(fixture.upload(fixture.client, str(self.path), 'parent', idx=3),
                         ('created', 'fixture-md5'))
        fixture.request.next_chunk.assert_called_once_with()
        self.assertTrue(fixture.client.cancelled)
        self.assertEqual(fixture.signals.error_occurred.calls, [])

    def test_safe_provider_stream_cleanup_error_preserves_confirmed_upload_result(self):
        fixture = provider_fixtures.TransferFixture(close_error=True)
        self.assertEqual(fixture.upload(fixture.client, str(self.path), 'parent'),
                         ('created', 'fixture-md5'))
        fixture.logger.exception.assert_called_once()

    def test_safe_provider_transport_cleanup_error_preserves_confirmed_upload_result(self):
        fixture = provider_fixtures.TransferFixture()
        close = load_function(PROVIDER, 'DriveClient._close_service', dict(logging=fixture.logger))
        fixture.client._close_service = MethodType(close, fixture.client)
        fixture.service.close.side_effect = OSError('Injected transport cleanup failure')
        self.assertEqual(fixture.upload(fixture.client, str(self.path), 'parent'),
                         ('created', 'fixture-md5'))
        fixture.logger.exception.assert_called_once()


@unittest.skipUnless((ROOT / ANALYZER).is_file(), 'Maintained source fixtures absent')
class SyncBaselineRecoveryTests(unittest.TestCase):
    HEADER = 'drive_id,name,path,md5,modified_time,size,timestamp,action\n'

    def setUp(self):
        self.enterContext(deny_external_effects())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / 'sync_log.csv'
        self.md5 = 'a' * 32
        self.item = SimpleNamespace(path='part.sldprt', md5_checksum=self.md5,
            size=123, modified_time=123.0, modified_by='fixture', parent='parent', id='file-id')
        self.analyzer = SimpleNamespace(sync_log_path=self.path, sync_log_dict={},
            drive_not_local=[], local_not_drive=[], md5_mismatch=[], read_only=False)
        self.read = load_function(ANALYZER, 'SyncAnalyzer.read_sync_log',
                                 dict(read_sync_history=history_fixtures.history.read_sync_history))
        self.compute = load_function(ANALYZER, 'SyncAnalyzer.compute_sync_actions',
            dict(SyncAction=SimpleNamespace, SyncActionType=ACTION_KINDS,
                 baseline_entry=history_fixtures.history.baseline_entry))

    def inferred_action(self, text, side):
        self.path.write_text(text, encoding='utf-8')
        self.read(self.analyzer)
        self.analyzer.drive_not_local = [self.item] if side == 'cloud' else []
        self.analyzer.local_not_drive = [self.item] if side == 'local' else []
        actions = self.compute(self.analyzer)
        self.assertEqual(len(actions), 1)
        return actions[0].action

    def assert_invalid_history(self, text):
        self.path.write_text(text, encoding='utf-8')
        self.analyzer.sync_log_dict = {'stale-entry': {'md5': self.md5}}
        with self.assertRaises(history_fixtures.history.SyncHistoryError):
            self.read(self.analyzer)
        self.assertEqual(self.analyzer.sync_log_dict, {})

    def test_safe_torn_csv_row_with_only_path_and_md5_rejects_baseline(self):
        torn = self.HEADER + f'file-id,part.sldprt,part.sldprt,{self.md5}'
        self.assert_invalid_history(torn)

    def test_safe_missing_csv_schema_columns_reject_baseline(self):
        malformed = f'path,md5\npart.sldprt,{self.md5}\n'
        self.assert_invalid_history(malformed)

    def test_safe_invalid_action_rejects_baseline(self):
        for operation in ('failed', 'cancelled', 'pending', 'not-an-action'):
            with self.subTest(operation=operation):
                malformed = self.HEADER + (
                    f'wrong-drive-id,part.sldprt,part.sldprt,{self.md5},123,123,123,{operation}\n')
                self.assert_invalid_history(malformed)

    def test_safe_later_torn_row_rejects_entire_baseline(self):
        valid = f'file-id,part.sldprt,part.sldprt,{"b" * 32},123,123,123,upload_new\n'
        torn = f'file-id,part.sldprt,part.sldprt,{self.md5}'
        self.assert_invalid_history(self.HEADER + valid + torn)

    def test_safe_wrong_remote_identity_does_not_authorize_delete_candidate(self):
        valid_but_unrelated = self.HEADER + (
            f'wrong-drive-id,part.sldprt,part.sldprt,{self.md5},123,123,123,upload_new\n')
        self.assertEqual(self.inferred_action(valid_but_unrelated, 'cloud'), 'download_new')

    def test_safe_valid_matching_remote_identity_preserves_delete_inference(self):
        valid = self.HEADER + (
            f'file-id,part.sldprt,part.sldprt,{self.md5},123,123,123,upload_new\n')
        self.assertEqual(self.inferred_action(valid, 'cloud'), 'delete_cloud')

    def test_safe_missing_sync_log_does_not_infer_deletion(self):
        self.read(self.analyzer)
        self.analyzer.local_not_drive = [self.item]
        self.assertEqual([item.action for item in self.compute(self.analyzer)], ['upload_new'])
        self.analyzer.local_not_drive = []
        self.analyzer.drive_not_local = [self.item]
        self.assertEqual([item.action for item in self.compute(self.analyzer)], ['download_new'])


@unittest.skipUnless((ROOT / EXECUTOR).is_file(), 'Maintained source fixtures absent')
class TransferBatchSettlementTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())
        names = ('dropped_actions_acknowledged', 'action_complete', 'action_failed',
                 'sync_cancelled', 'sync_execution_finished')
        self.signals = SimpleNamespace(**{name: Signal() for name in names})
        self.network_pool = SimpleNamespace(start=Mock())
        self.thread_pool = SimpleNamespace(start=Mock())
        self.loop = SimpleNamespace(quit=Mock(), exec_=Mock())
        self.namespace = dict(signal_manager=self.signals, logging=Mock(),
            network_pool=self.network_pool, thread_pool=self.thread_pool,
            _FunctionRunnable=lambda func: SimpleNamespace(func=func),
            validate_selection=Mock(), StaleSyncPlan=RuntimeError,
            QEventLoop=lambda: self.loop, SyncActionType=ACTION_KINDS,
            traceback=SimpleNamespace(format_exc=lambda: 'fixture traceback'))
        self.actions = tuple((index, SimpleNamespace(action='upload_new', path=f'part-{index}'))
                             for index in range(2))
        self.executor = SimpleNamespace(_started=False, _finished=False, cancelled=False,
            consent=None, action_tuple=self.actions, _approved_selection=self.actions,
            folders_to_create=[], _created_folders={}, deleteLater=Mock(),
            thread_finished=Signal(), guard_failed=Signal(), drive_client=Mock(),
            log_sync_action=Mock(), complete_action_count=0, cancelled_action_count=0,
            failed_action_count=0, completed_actions=[])
        bind_recovery_executor(self.executor, self.namespace)
        self.executor._counted_actions = set()
        self.executor.outcome_ready = Signal()
        for name in ('start_sync', 'sync_worker', '_finish_execution', 'on_action_complete',
                     'on_action_failed', 'on_sync_cancelled', 'check_if_all_actions_finished',
                     '_upload', 'on_transfer_outcome'):
            function = load_function(EXECUTOR, 'SyncExecutor.' + name, self.namespace)
            setattr(self.executor, name, MethodType(function, self.executor))
        for signal, slot in (('dropped_actions_acknowledged', 'start_sync'),
                             ('sync_cancelled', 'on_sync_cancelled')):
            getattr(self.signals, signal).connect(getattr(self.executor, slot))
        self.executor.outcome_ready.connect(self.executor.on_transfer_outcome)
        self.run = load_function(EXECUTOR, '_FunctionRunnable.run', self.namespace)

    def assert_unsettled(self):
        self.assertFalse(self.executor._finished)
        self.assertEqual(self.signals.sync_execution_finished.calls, [])

    def test_safe_initial_worker_schedule_error_settles_started_batch(self):
        self.network_pool.start.side_effect = RuntimeError('Thread pool rejected initial task')
        self.executor.start_sync()
        self.assertTrue(self.executor._started)
        self.assertTrue(self.executor._finished)
        self.assertEqual(self.executor.failed_action_count, len(self.actions))
        self.assertEqual(self.signals.sync_execution_finished.calls, [()])
        self.executor.start_sync()  # _started suppresses a second start attempt.
        self.assertEqual(self.network_pool.start.call_count, 1)

    def test_safe_mid_batch_schedule_error_settles_unscheduled_actions_without_resubmission(self):
        scheduled = []

        def schedule(task):
            if scheduled:
                raise RuntimeError('Thread pool rejected the second action')
            scheduled.append(task)

        self.network_pool.start.side_effect = schedule
        self.run(SimpleNamespace(func=self.executor.sync_worker))
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(self.executor.failed_action_count, 1)
        self.assert_unsettled()
        # Settle the accepted task using the real upload cancellation boundary.
        self.executor.cancelled = True
        self.run(scheduled[0])
        self.assertEqual(self.executor.cancelled_action_count, 1)
        self.assertEqual(self.executor.failed_action_count, 1)
        self.assertTrue(self.executor._finished)
        self.assertEqual(self.signals.sync_execution_finished.calls, [()])
        self.executor.drive_client.upload_file.assert_not_called()

    def test_safe_folder_worker_schedule_error_settles_entire_batch(self):
        self.executor.folders_to_create = [Path('parts')]
        self.thread_pool.start.side_effect = RuntimeError('Folder worker rejected')
        self.run(SimpleNamespace(func=self.executor.sync_worker))
        self.network_pool.start.assert_not_called()
        self.assertEqual(self.signals.action_failed.calls, [(), ()])
        self.assertEqual(self.executor.failed_action_count, len(self.actions))
        self.assertTrue(self.executor._finished)
        self.assertEqual(self.signals.sync_execution_finished.calls, [()])

    def test_safe_duplicate_identified_completion_cannot_finish_before_second_action(self):
        first = self.actions[0][1]
        result = dict(action_index=0, path=first.path, action='upload_new',
                      outcome='committed', cancelled=False)
        self.executor.outcome_ready.emit(result)
        self.executor.outcome_ready.emit(result)
        self.assertEqual(self.executor.complete_action_count, 1)
        self.assertEqual(self.executor.completed_actions, [first])
        self.assert_unsettled()
        # Legacy untagged UI notifications cannot finish a batch either.
        self.signals.action_complete.emit(first)
        self.signals.action_complete.emit(first)
        self.assert_unsettled()
        second = self.actions[1][1]
        self.executor.outcome_ready.emit(dict(result, action_index=1, path=second.path))
        self.assertTrue(self.executor._finished)
        self.assertEqual(self.executor.completed_actions, [first, second])
        self.assertEqual(self.signals.sync_execution_finished.calls, [()])

    def test_safe_failure_recorded_before_cancel_preserves_outcome_when_delivered_later(self):
        self.executor.on_sync_cancelled()
        self.executor.outcome_ready.emit(dict(action_index=0, path=self.actions[0][1].path,
            action='upload_new', outcome='unknown', cancelled=False))
        # An untagged late notification has no authority over batch accounting.
        self.signals.action_failed.emit()
        self.assertEqual(self.executor.failed_action_count, 1)
        self.assertEqual(self.executor.cancelled_action_count, 0)
        self.assert_unsettled()

    def test_safe_unknown_action_kind_has_explicit_terminal_outcome(self):
        self.executor.action_tuple = ((0, SimpleNamespace(action='unknown-kind', path='part')),)
        scheduled = []
        self.network_pool.start.side_effect = scheduled.append
        self.executor.sync_worker()
        self.run(scheduled[0])
        self.assertEqual(self.signals.action_failed.calls, [()])
        self.assertEqual(self.signals.action_complete.calls, [])
        self.assertEqual(self.executor.failed_action_count, 1)
        self.assertTrue(self.executor._finished)

    def test_safe_queued_uploads_cancel_before_any_provider_call_and_settle_once(self):
        scheduled = []
        self.network_pool.start.side_effect = scheduled.append
        self.executor.sync_worker()
        self.executor.on_sync_cancelled()
        for task in scheduled:
            self.run(task)
        self.assertEqual(self.executor.cancelled_action_count, len(self.actions))
        self.assertEqual(self.executor.failed_action_count, 0)
        self.assertEqual(self.executor.complete_action_count, 0)
        self.assertTrue(self.executor._finished)
        self.assertEqual(self.signals.sync_execution_finished.calls, [()])
        self.executor.drive_client.upload_file.assert_not_called()


if __name__ == '__main__':
    unittest.main()
