"""Independent offline review regressions for identified transfer outcomes.

These tests compile unchanged maintained method bodies. Signal and pool adapters
are synchronous fault injectors; they do not establish real Qt delivery behavior.
No account, network, CAD application, or user project data is touched.
"""
from pathlib import Path
import threading
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

from recovered_harness import ROOT, deny_external_effects, load_function
from test_recovered_overwrite import load_recovery_module


SOURCE = 'maintained/core/sync_executor.py'


class Signal:
    def __init__(self):
        self.calls = []
        self.slots = []
        self.error = None

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        if slot in self.slots:
            self.slots.remove(slot)

    def emit(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        for slot in tuple(self.slots):
            slot(*args)


@unittest.skipUnless((ROOT / SOURCE).is_file(), 'Maintained source fixtures absent')
class IndependentTransferSettlementTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())
        journal = load_recovery_module()
        self.signals = SimpleNamespace(**{name: Signal() for name in (
            'dropped_actions_acknowledged', 'action_complete', 'action_failed',
            'sync_cancelled', 'sync_execution_finished')})
        self.action = (17, SimpleNamespace(path='part.step', action='upload_new'))
        self.pool = SimpleNamespace(start=Mock())
        self.executor = SimpleNamespace(
            action_tuple=(self.action,), cancelled=False, _finished=False,
            _started=False, consent=None, _approved_selection=(self.action,),
            _result_lock=threading.Lock(), _settled_actions=set(), _counted_actions=set(),
            _transfer_journal=None, action_results=[], complete_action_count=0,
            failed_action_count=0, cancelled_action_count=0, completed_actions=[],
            outcome_ready=Signal(), guard_failed=Signal(), thread_finished=Signal(),
            deleteLater=Mock(), log_sync_action=Mock(), folders_to_create=[],
            _created_folders={})
        kinds = ('upload_new', 'download_new', 'upload_revision', 'download_revision',
                 'upload_unknown', 'download_unknown', 'delete_local', 'delete_cloud')
        self.namespace = dict(
            signal_manager=self.signals, logging=Mock(),
            PendingTransferRecovery=journal.PendingTransferRecovery,
            StaleSyncPlan=type('StaleSyncPlan', (RuntimeError,), {}),
            SyncActionType=SimpleNamespace(**{kind.upper(): kind for kind in kinds}),
            network_pool=self.pool, thread_pool=self.pool, Path=Path,
            QEventLoop=lambda: SimpleNamespace(quit=Mock(), exec_=Mock()),
            _FunctionRunnable=lambda func: SimpleNamespace(func=func),
            SimpleNamespace=SimpleNamespace, validate_selection=Mock())
        for name in ('_settle_transfer', '_settle_unexpected', 'on_transfer_outcome',
                     'check_if_all_actions_finished', '_finish_execution',
                     'on_sync_cancelled', 'start_sync', 'sync_worker'):
            method = load_function(SOURCE, 'SyncExecutor.' + name, self.namespace)
            setattr(self.executor, name, MethodType(method, self.executor))
        self.executor.outcome_ready.connect(self.executor.on_transfer_outcome)

    def assert_finished_once(self):
        self.assertTrue(self.executor._finished)
        self.assertEqual(self.signals.sync_execution_finished.calls, [()])
        self.assertEqual(len(self.executor.action_results), 1)

    def test_late_cancel_does_not_reclassify_dispatched_unknown_outcome(self):
        self.executor.cancelled = True
        self.executor._settle_transfer(self.action, None, 'unknown',
                                      error=TimeoutError('Remote response lost'))
        result = self.executor.action_results[0]
        self.assertEqual(result['outcome'], 'unknown')
        self.assertFalse(result['cancelled'])
        self.assertEqual(self.executor.failed_action_count, 1)
        self.assertEqual(self.executor.cancelled_action_count, 0)
        self.assert_finished_once()

    def test_late_cancel_does_not_reclassify_independent_pre_dispatch_failure(self):
        self.executor.cancelled = True
        self.executor._settle_transfer(self.action, None, 'not_started',
                                      error=OSError('Journal was unavailable'))
        self.assertFalse(self.executor.action_results[0]['cancelled'])
        self.assertEqual(self.executor.failed_action_count, 1)
        self.assertEqual(self.executor.cancelled_action_count, 0)
        self.assert_finished_once()

    def test_guard_notification_exception_does_not_strand_unknown_action(self):
        self.executor.guard_failed.error = RuntimeError('Notification target closed')
        self.executor._settle_transfer(self.action, None, 'unknown',
                                      error=TimeoutError('Remote response lost'))
        self.assertEqual(self.executor.failed_action_count, 1)
        self.assert_finished_once()

    def test_legacy_success_notification_exception_preserves_commit_and_settlement(self):
        self.signals.action_complete.error = RuntimeError('Notification target closed')
        self.executor._settle_transfer(self.action, None, 'committed', drive_id='created-id')
        self.assertEqual(self.executor.action_results[0]['outcome'], 'committed')
        self.assertEqual(self.executor.complete_action_count, 1)
        self.executor.log_sync_action.assert_called_once_with(self.action, id='created-id')
        self.assert_finished_once()

    def test_legacy_failure_notification_exception_does_not_strand_action(self):
        self.signals.action_failed.error = RuntimeError('Notification target closed')
        self.executor._settle_transfer(self.action, None, 'not_started',
                                      error=OSError('Cannot prepare transfer'))
        self.assertEqual(self.executor.failed_action_count, 1)
        self.assert_finished_once()

    def test_duplicate_settlement_cannot_turn_unknown_into_commit(self):
        self.executor._settle_transfer(self.action, None, 'unknown',
                                      error=TimeoutError('Remote response lost'))
        self.executor._settle_transfer(self.action, None, 'committed', drive_id='invented-id')
        self.assertEqual(self.executor.action_results[0]['outcome'], 'unknown')
        self.executor.log_sync_action.assert_not_called()
        self.assertEqual(self.executor.failed_action_count, 1)
        self.assert_finished_once()

    def test_unexpected_action_body_exception_still_settles_once(self):
        self.executor._upload = Mock(side_effect=RuntimeError('Unexpected action-body failure'))
        self.executor.sync_worker()
        task = self.pool.start.call_args.args[0]
        task.func()
        self.assertEqual(self.executor.failed_action_count, 1)
        self.assert_finished_once()

    def test_unexpected_handler_preserves_unknown_durable_operation(self):
        operation = SimpleNamespace(
            operation_id='dispatched-operation', directory=Path('/fixture/recovery'),
            journal=SimpleNamespace(read=Mock(return_value={'state': {'outcome': 'unknown'}})),
            record_outcome=Mock())
        self.executor._active_operations = {(17, 'part.step'): operation}
        self.executor.cancelled = True
        self.executor._settle_unexpected(self.action, RuntimeError('Unexpected failure after dispatch'))
        self.assertEqual(self.executor.action_results[0]['outcome'], 'unknown')
        self.assertFalse(self.executor.action_results[0]['cancelled'])
        self.executor.log_sync_action.assert_not_called()
        self.assert_finished_once()

    def test_unreadable_active_operation_is_unknown_instead_of_not_started(self):
        operation = SimpleNamespace(
            operation_id='unreadable-operation', directory=Path('/fixture/recovery'),
            journal=SimpleNamespace(read=Mock(side_effect=OSError('Cannot read durable state'))),
            record_outcome=Mock(side_effect=OSError('Cannot write durable state')))
        self.executor._active_operations = {(17, 'part.step'): operation}
        self.executor._settle_unexpected(self.action, RuntimeError('Unexpected failure'))
        self.assertEqual(self.executor.action_results[0]['outcome'], 'unknown')
        self.executor.log_sync_action.assert_not_called()
        self.assert_finished_once()

    def test_unexpected_handler_never_fabricates_baseline_for_partial_commit(self):
        operation = SimpleNamespace(
            operation_id='published-operation', directory=Path('/fixture/recovery'),
            journal=SimpleNamespace(read=Mock(return_value={'state': {
                'outcome': 'committed', 'phase': 'published', 'details': {}}})),
            record_outcome=Mock())
        self.executor._active_operations = {(17, 'part.step'): operation}
        self.executor._settle_unexpected(self.action, RuntimeError('Stopped after publication'))
        self.assertEqual(self.executor.action_results[0]['outcome'], 'committed_recording_failed')
        self.executor.log_sync_action.assert_not_called()
        self.assertFalse(any(call.kwargs.get('phase') == 'completed'
                             for call in operation.record_outcome.call_args_list))
        self.assert_finished_once()

    def test_unexpected_handler_does_not_append_an_already_completed_baseline(self):
        operation = SimpleNamespace(
            operation_id='completed-operation', directory=Path('/fixture/recovery'),
            journal=SimpleNamespace(read=Mock(return_value={'state': {
                'outcome': 'committed', 'phase': 'completed',
                'details': {'drive_id': 'verified-created-id'}}})),
            record_outcome=Mock())
        self.executor._active_operations = {(17, 'part.step'): operation}
        self.executor._settle_unexpected(self.action, RuntimeError('Stopped after recording'))
        self.assertEqual(self.executor.action_results[0]['outcome'], 'committed')
        self.executor.log_sync_action.assert_not_called()
        self.assert_finished_once()


@unittest.skipUnless((ROOT / 'maintained/app/app_controller.py').is_file(),
                     'Maintained source fixtures absent')
class IndependentTransferReportContextTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())
        self.signals = SimpleNamespace(page_change=Signal(), sync_cancelled=Signal())
        self.namespace = dict(Path=Path, signal_manager=self.signals,
                              Page=SimpleNamespace(PROJECT='project', HOME='home'))

    def test_report_freezes_executor_context_instead_of_current_selection(self):
        original = {'path': 'part.step', 'action': 'upload_new', 'outcome': 'unknown'}
        executor = SimpleNamespace(project_name='Original project', project_id='original-id',
                                   working_directory='/fixture/original-root',
                                   action_results=[original])
        controller = SimpleNamespace(sync_executor=executor, signed_in=True,
                                     project_name='Different project', project_id='different-id',
                                     working_directory='/fixture/current-root',
                                     show_transfer_results=Mock(), _begin_sync_analysis=Mock())
        finished = load_function('maintained/app/app_controller.py',
                                 'AppController.on_sync_execution_finished', self.namespace)
        finished(controller)
        self.assertEqual(controller.last_transfer_context, {
            'project_name': 'Original project', 'project_id': 'original-id',
            'project_root': str(Path('/fixture/original-root') / 'Original project')})
        original['outcome'] = 'committed'
        executor.project_name = 'Later executor change'
        self.assertEqual(controller.last_transfer_results[0]['outcome'], 'unknown')
        self.assertEqual(controller.last_transfer_context['project_name'], 'Original project')
        controller.show_transfer_results.assert_called_once_with()

    def test_signout_clears_report_and_handles_previously_deleted_qt_dialog(self):
        dialog = SimpleNamespace(close=Mock(side_effect=RuntimeError('Qt object already deleted')))
        controller = SimpleNamespace(
            last_transfer_results=({'path': 'prior.step'},),
            last_transfer_context={'project_name': 'Prior account project'},
            _transfer_results_dialog=dialog, sync_executing=False,
            auth_manager=SimpleNamespace(last_invalidation=None),
            _retire_sync_clients=Mock(), _clear_sync_plan=Mock(), disable_buttons=Mock(),
            main_window=SimpleNamespace(settings_btn=SimpleNamespace(setEnabled=Mock())))
        signed_out = load_function('maintained/app/app_controller.py',
                                   'AppController.on_session_signed_out', self.namespace)
        signed_out(controller)
        self.assertEqual(controller.last_transfer_results, ())
        self.assertEqual(controller.last_transfer_context, {})
        self.assertIsNone(controller._transfer_results_dialog)
        dialog.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
