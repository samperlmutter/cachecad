"""Real maintained provider/completion functions with isolated dependency doubles."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from recovered_harness import load_function, deny_external_effects

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless((ROOT / 'maintained/core/drive_client.py').is_file(), 'Actual source absent')
class ProviderLifecycleTests(unittest.TestCase):
    def test_provider_connects_queued_before_scheduling(self):
        events = []
        finished = Mock()
        connection = SimpleNamespace(connect=lambda slot, kind: events.append(('connect', slot, kind)))
        loop = SimpleNamespace(exec=lambda: events.append(('exec',)))
        instance = SimpleNamespace(service_built=connection, _finish=finished,
                                   build_service=Mock(), on_token_refresh=Mock(),
                                   on_sync_finished=Mock(), on_sync_cancelled=Mock())
        function = load_function('maintained/core/drive_client.py', 'DriveClient.__init__', {
            'super': lambda: SimpleNamespace(__init__=lambda: None),
            'QEventLoop': lambda: loop, 'Qt': SimpleNamespace(QueuedConnection='queued'),
            'thread_pool': SimpleNamespace(start=lambda task: events.append(('start',))),
            '_FunctionRunnable': lambda function: function,
            'signal_manager': SimpleNamespace(token_refreshed=Mock(), sync_execution_finished=Mock(), sync_cancelled=Mock()),
            'settings': SimpleNamespace(get=lambda key: False),
        })
        with deny_external_effects():
            function(instance, object())
        self.assertEqual(events, [('connect', finished, 'queued'), ('start',), ('exec',)])

    def test_completion_has_no_vendor_import_or_profile_request(self):
        signal = Mock()
        instance = SimpleNamespace(failed_action_count=1, complete_action_count=2,
                                   cancelled_action_count=0, action_tuple=(1, 2, 3),
                                   cancelled=True, deleteLater=Mock(), drive_client=Mock())
        function = load_function('maintained/core/sync_executor.py', 'SyncExecutor.check_if_all_actions_finished', {
            'logging': Mock(), 'signal_manager': SimpleNamespace(sync_execution_finished=signal),
        })
        with deny_external_effects():
            function(instance)
        signal.emit.assert_called_once_with()
        instance.deleteLater.assert_called_once_with()
        instance.drive_client.get_user_info_for_cloud_logging.assert_not_called()
        self.assertFalse(instance.cancelled)
        self.assertEqual(instance.failed_action_count + instance.complete_action_count + instance.cancelled_action_count, 0)

    def test_pending_actions_do_not_finish(self):
        signal = Mock()
        instance = SimpleNamespace(failed_action_count=0, complete_action_count=1,
                                   cancelled_action_count=0, action_tuple=(1, 2), deleteLater=Mock())
        function = load_function('maintained/core/sync_executor.py', 'SyncExecutor.check_if_all_actions_finished', {
            'logging': Mock(), 'signal_manager': SimpleNamespace(sync_execution_finished=signal),
        })
        with deny_external_effects():
            function(instance)
        signal.emit.assert_not_called()
        instance.deleteLater.assert_not_called()
