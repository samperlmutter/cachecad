"""Selected actual analyzer/controller methods, with deterministic in-memory I/O.

No Qt event loop, real threads, files, credentials or network are exercised.
The maintained comparison is serialized in its outcome slot; Qt delivery and UI
latency still require platform integration validation.
"""
import logging
from pathlib import Path, PurePosixPath
from types import SimpleNamespace, MethodType
import traceback
import unittest
from unittest.mock import Mock
from recovered_harness import load_function, deny_external_effects

ROOT = Path(__file__).resolve().parents[1]


class Signal:
    def __init__(self):
        self.calls = []
        self.slots = []

    def connect(self, callback, *connection_options):
        self.slots.append(callback)

    def emit(self, *args):
        self.calls.append(args)
        for callback in tuple(self.slots):
            callback(*args)


class Pool:
    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)

    def drain(self):
        while self.tasks:
            self.tasks.pop(0).func()


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        if not (ROOT / 'maintained/core/sync_analyzer.py').is_file():
            self.skipTest('Maintained sources are source-distribution fixtures')
        self.enterContext(deny_external_effects())
        self.signals = SimpleNamespace(**{name: Signal() for name in (
            'sync_analysis_started', 'sync_count_updated', 'error_occurred',
            'update_sync_selection_treeview', 'enable_buttons', 'disable_buttons',
            'page_change', 'project_read_only')})
        self.pool = Pool()
        self.namespace = {'Qt': SimpleNamespace(QueuedConnection='queued'), 'signal_manager': self.signals, 'logging': Mock(spec=logging),
            'QMessageBox': SimpleNamespace(warning=Mock()), 'traceback': traceback, 'Path': PurePosixPath, 'thread_pool': self.pool,
            '_FunctionRunnable': lambda func: SimpleNamespace(func=func),
            'get_project_app_folder': lambda working, project: PurePosixPath('/fake/cache'),
            'super': lambda: SimpleNamespace(__init__=lambda: None)}

    def analyzer(self):
        obj = SimpleNamespace(**{name: Signal() for name in ('scan_outcome',
            'analysis_started', 'analysis_succeeded', 'analysis_failed', 'analysis_cancelled')})
        names = ('__init__', 'start_sync_analysis', 'local_worker', 'drive_worker',
            '_on_scan_outcome', '_fail_analysis', '_check_ready', '_compare_worker',
            'cancel_sync_analysis', 'is_background_running', '_on_local_file_list_acquired',
            '_on_drive_file_list_acquired', '_on_local_task_finished', '_on_drive_task_finished')
        for name in names:
            method = load_function('maintained/core/sync_analyzer.py', 'SyncAnalyzer.' + name, self.namespace)
            setattr(obj, name, MethodType(method, obj))
        local = SimpleNamespace(get_local_items_from_path=Mock())
        drive = SimpleNamespace(get_drive_items_from_project_id=Mock())
        db = SimpleNamespace(reset=Mock(), write_local_items=Mock(), write_drive_items=Mock(),
            compare_local_and_drive=Mock(return_value=([], [], [], {'folder': 'id'})))
        obj.__init__(drive, local, db)
        obj._load_cached_folders = Mock(return_value=['cached'])
        obj.save_local_as_csv = Mock()
        obj.save_drive_as_csv = Mock()
        obj.read_sync_log = Mock()
        obj.compute_sync_actions = Mock(return_value=['fresh action'])
        obj.write_folder_dict_csv = Mock()
        obj.sort_actions = Mock(return_value=(['download'], ['upload']))
        return obj

    def start(self, obj, **kwargs):
        obj.start_sync_analysis('project', 'project-id', '/fake/work', **kwargs)
        self.pool.drain()
        return obj._generation

    def controller(self, analyzer):
        obj = SimpleNamespace(sync_analyzer=analyzer, signed_in=True, project_name='project',
            project_id='project-id', working_directory=PurePosixPath('/fake/work'),
            project_read_only=True, buttons_disabled=False, sync_executing=False,
            analysis_ready=True, download_actions=['old'], upload_actions=['old'],
            path_dict={'old': 'id'}, main_window=None, auth_manager=SimpleNamespace(check_creds=lambda: True))
        namespace = dict(self.namespace, CaseInsensitivePathDict=dict,
            confirm_project_location=lambda path, parent: True,
            Page=SimpleNamespace(PROJECT='project', SELECTION='selection'),
            Qt=SimpleNamespace(UserRole=1), SyncExecutor=Mock())
        for name in ('_clear_sync_plan', '_begin_sync_analysis', '_scan_is_current', '_settle_scan',
            'on_scan_started', 'on_scan_succeeded', 'on_scan_failed', 'on_scan_cancelled',
            'on_sync_analysis_finished', 'on_refresh_clicked', 'enable_buttons', 'disable_buttons',
            'on_sync_execution_finished', 'on_upload_card_clicked', 'on_download_card_clicked',
            'on_execute_sync', 'project_selected', 'on_back_button_clicked'):
            method = load_function('maintained/app/app_controller.py', 'AppController.' + name, namespace)
            setattr(obj, name, MethodType(method, obj))
        analyzer.analysis_started.connect(lambda generation: obj.on_scan_started(analyzer, generation))
        analyzer.analysis_succeeded.connect(lambda generation, downloads, uploads, paths: obj.on_scan_succeeded(analyzer, generation, downloads, uploads, paths))
        analyzer.analysis_failed.connect(lambda generation, message: obj.on_scan_failed(analyzer, generation, message))
        analyzer.analysis_cancelled.connect(lambda generation: obj.on_scan_cancelled(analyzer, generation))
        return obj

    def test_baseline_refresh_does_nothing(self):
        obj = SimpleNamespace(sync_analyzer=SimpleNamespace(start_sync_analysis=Mock()))
        method = load_function('recovered/app/app_controller.py', 'AppController.on_refresh_clicked', self.namespace)
        method(obj)
        obj.sync_analyzer.start_sync_analysis.assert_not_called()

    def test_failure_retry_stale_result_and_exact_current_success(self):
        analyzer = self.analyzer()
        controller = self.controller(analyzer)
        controller.on_refresh_clicked()
        self.pool.drain()
        old = analyzer._generation
        self.assertEqual(controller.download_actions, [])
        self.assertFalse(controller.analysis_ready)
        analyzer.scan_outcome.emit(old, 'drive', None, 'fake scan failed')
        analyzer.scan_outcome.emit(old, 'local', ['late old local'], '')
        analyzer.scan_outcome.emit(old, 'drive', None, 'duplicate failure')
        self.assertEqual(len(analyzer.analysis_failed.calls), 1)
        self.assertEqual(len(self.signals.error_occurred.calls), 1)
        self.namespace['QMessageBox'].warning.assert_called_once()
        self.assertEqual(len(self.signals.enable_buttons.calls), 1)
        analyzer.db_manager.reset.assert_not_called()
        self.assertFalse(controller.buttons_disabled)
        controller.on_refresh_clicked()
        self.pool.drain()
        current = analyzer._generation
        self.assertGreater(current, old)
        self.assertTrue(analyzer.read_only)
        analyzer.scan_outcome.emit(old, 'drive', ['stale'], '')
        analyzer.scan_outcome.emit(old, 'local', None, 'old failure')
        analyzer.scan_outcome.emit(current, 'local', ['fresh local'], '')
        analyzer.scan_outcome.emit(current, 'drive', ['fresh drive'], '')
        analyzer.scan_outcome.emit(current, 'drive', ['duplicate'], '')
        self.assertEqual(len(analyzer.analysis_succeeded.calls), 1)
        analyzer.db_manager.reset.assert_called_once()
        analyzer.db_manager.write_local_items.assert_called_once_with(['fresh local'])
        analyzer.db_manager.write_drive_items.assert_called_once_with(['fresh drive'])
        self.assertEqual(controller.download_actions, ['download'])
        self.assertTrue(controller.analysis_ready)
        self.assertEqual(len(self.signals.enable_buttons.calls), 2)
        controller.on_scan_succeeded(analyzer, current, ['duplicate'], [], {})
        self.assertEqual(len(self.signals.enable_buttons.calls), 2)
        self.assertEqual(controller.download_actions, ['download'])

    def test_duplicate_side_error_after_success_is_ignored(self):
        obj = self.analyzer(); generation = self.start(obj)
        obj.scan_outcome.emit(generation, 'local', ['local'], '')
        obj.scan_outcome.emit(generation, 'local', None, 'duplicate local failure')
        self.assertEqual(obj.analysis_failed.calls, [])
        obj.scan_outcome.emit(generation, 'drive', [], '')
        self.assertEqual(len(obj.analysis_succeeded.calls), 1)

    def test_cancel_then_new_scan_rejects_old_error_and_compare(self):
        obj = self.analyzer(); old = self.start(obj)
        obj.cancel_sync_analysis()
        current = self.start(obj)
        obj.scan_outcome.emit(old, 'local', None, 'cancelled old failure')
        obj._compare_worker(old)
        obj.db_manager.reset.assert_not_called()
        obj.scan_outcome.emit(current, 'drive', [], '')
        obj.scan_outcome.emit(current, 'local', [], '')
        self.assertEqual(obj.analysis_failed.calls, [])
        self.assertEqual(len(obj.analysis_succeeded.calls), 1)

    def test_malformed_result_settles_error_and_blocks_compare(self):
        obj = self.analyzer(); generation = self.start(obj)
        obj.scan_outcome.emit(generation, 'local', 42, '')
        obj.scan_outcome.emit(generation, 'drive', [], '')
        self.assertEqual(len(obj.analysis_failed.calls), 1)
        obj.db_manager.reset.assert_not_called()

    def test_compare_failure_does_not_publish_prior_plan(self):
        obj = self.analyzer(); controller = self.controller(obj)
        generation = self.start(obj)
        obj.db_manager.compare_local_and_drive.side_effect = RuntimeError('fake DB error')
        obj.scan_outcome.emit(generation, 'local', [], '')
        obj.scan_outcome.emit(generation, 'drive', [], '')
        self.assertEqual(len(obj.analysis_failed.calls), 1)
        self.assertEqual(obj.analysis_succeeded.calls, [])
        self.assertEqual(controller.download_actions, [])
        self.assertFalse(controller.analysis_ready)
        self.assertFalse(controller.buttons_disabled)

    def test_callbacks_capture_generation_and_initial_exception_is_failure(self):
        obj = self.analyzer(); old = self.start(obj)
        old_callback = obj.local_client.get_local_items_from_path.call_args.kwargs['on_success']
        current = self.start(obj)
        old_callback(old, ['stale local'])
        self.assertFalse(obj._local_files_ready)
        obj.drive_client.get_drive_items_from_project_id.side_effect = RuntimeError('initial failure')
        obj.drive_worker(current, 'project-id', [])
        self.assertEqual(obj.analysis_failed.calls, [(current, 'initial failure')])

    def test_synchronous_adapter_callbacks_settle_once(self):
        obj = self.analyzer()
        obj.local_client.get_local_items_from_path.side_effect = lambda *args, **kwargs: kwargs['on_success'](kwargs['scan_id'], [])
        obj.drive_client.get_drive_items_from_project_id.side_effect = lambda *args, **kwargs: kwargs['on_success'](kwargs['scan_id'], [])
        self.start(obj)
        self.assertEqual(len(obj.analysis_succeeded.calls), 1)
        obj.db_manager.reset.assert_called_once()

    def test_old_analyzer_instance_cannot_repopulate_controller(self):
        old = self.analyzer(); controller = self.controller(old)
        old_generation = self.start(old)
        current = self.analyzer(); controller.sync_analyzer = current
        generation = self.start(current)
        self.assertEqual(generation, old_generation)
        controller.on_scan_succeeded(old, generation, ['stale'], [], {})
        controller.on_scan_failed(old, generation, 'stale error')
        self.assertEqual(controller.download_actions, [])
        self.assertEqual(self.signals.error_occurred.calls, [])

    def test_post_execution_rescan_preserves_read_only(self):
        obj = self.analyzer(); controller = self.controller(obj)
        controller.sync_executing = True
        controller.on_sync_execution_finished()
        self.assertTrue(obj.read_only)
        self.assertFalse(controller.sync_executing)
        self.assertFalse(controller.analysis_ready)

    def test_failed_scan_cannot_open_or_execute_stale_selection(self):
        obj = self.analyzer(); controller = self.controller(obj)
        generation = self.start(obj)
        obj.scan_outcome.emit(generation, 'drive', None, 'failure')
        controller.on_upload_card_clicked()
        controller.on_download_card_clicked()
        controller.on_execute_sync(['old action'])
        self.assertFalse(controller.sync_executing)
        self.assertEqual(self.signals.page_change.calls, [])

    def test_back_navigation_cancels_pending_analysis(self):
        obj = self.analyzer(); controller = self.controller(obj)
        generation = self.start(obj)
        controller.on_back_button_clicked()
        obj.scan_outcome.emit(generation, 'local', [], '')
        obj.scan_outcome.emit(generation, 'drive', [], '')
        obj.db_manager.reset.assert_not_called()
        self.assertFalse(controller.analysis_ready)
        self.assertFalse(controller.buttons_disabled)
        self.assertEqual(obj.analysis_succeeded.calls, [])

    def test_cache_read_failure_settles_before_scanners_launch(self):
        obj = self.analyzer(); controller = self.controller(obj)
        obj._load_cached_folders.side_effect = OSError('fake cache read error')
        controller.on_refresh_clicked()
        self.assertEqual(self.pool.tasks, [])
        self.assertEqual(len(obj.analysis_failed.calls), 1)
        self.assertFalse(controller.buttons_disabled)
        self.assertFalse(controller.analysis_ready)

    def test_actual_folder_write_failure_is_generation_bound(self):
        obj = self.analyzer(); self.controller(obj)
        generation = self.start(obj)
        method = load_function('maintained/core/sync_analyzer.py', 'SyncAnalyzer.write_folder_dict_csv', self.namespace)
        obj.write_folder_dict_csv = MethodType(method, obj)
        obj.folder_dict_path = SimpleNamespace(exists=lambda: True, unlink=Mock(side_effect=OSError('fake cache write error')))
        obj.scan_outcome.emit(generation, 'local', [], '')
        obj.scan_outcome.emit(generation, 'drive', [], '')
        self.assertEqual(len(obj.analysis_failed.calls), 1)
        self.assertEqual(obj.analysis_succeeded.calls, [])
        self.assertEqual(len(self.signals.error_occurred.calls), 1)

    def test_declined_project_advisory_preserves_current_selection(self):
        obj = self.analyzer(); controller = self.controller(obj)
        namespace = dict(self.namespace, confirm_project_location=lambda *args: False,
            Qt=SimpleNamespace(UserRole=1), Page=SimpleNamespace(PROJECT='project'))
        method = load_function('maintained/app/app_controller.py', 'AppController.project_selected', namespace)
        method(controller, SimpleNamespace(data=lambda role: {'name': 'other', 'id': 'other-id'}))
        self.assertEqual(controller.project_name, 'project')
        self.assertEqual(controller.download_actions, ['old'])
        self.assertEqual(self.pool.tasks, [])

    def test_untagged_legacy_results_cannot_write_database(self):
        obj = self.analyzer(); self.start(obj)
        obj._on_local_file_list_acquired(['untagged'])
        obj._on_drive_file_list_acquired(['untagged'])
        obj._on_local_task_finished(); obj._on_drive_task_finished()
        obj.db_manager.reset.assert_not_called()
        self.assertFalse(obj._drive_files_ready)
        self.assertFalse(obj._local_files_ready)


if __name__ == '__main__':
    unittest.main()
