"""Real recovered scan/analyzer functions, isolated behind in-memory Drive doubles.

The unchanged baseline tests positively reproduce the unsafe result. Maintained
regressions require that no acquired/finished signal or comparison is published
from incomplete remote scans. No module initializer, credentials, real network,
process, GUI, or user files are used.
"""
from collections import deque
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

from recovered_harness import ROOT, deny_external_effects, load_function

BASELINE = 'recovered/core/drive_client.py'
MAINTAINED = 'maintained/core/drive_client.py'
ANALYZER = 'recovered/core/sync_analyzer.py'


class Signal:
    def __init__(self):
        self.calls = []
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        self.calls.append(args)
        for slot in self.slots:
            slot(*args)


class FakeEventLoop:
    def quit(self):
        pass


class FakeTimer:
    @staticmethod
    def singleShot(delay, callback):
        if delay != 0:
            raise AssertionError('Unexpected timer delay')
        callback()


class FakeDriveBatch:
    """Synchronous callbacks match the BatchHttpRequest execute contract."""
    def __init__(self, service):
        self.service = service
        self.requests = []

    def add(self, request, callback):
        self.requests.append((request, callback))

    def execute(self):
        self.service.batches_executed += 1
        for index, (request, callback) in enumerate(self.requests):
            key = (request['q'].split("'")[1], request['pageToken'])
            self.service.executed_keys.append(key)
            response = self.service.responses[key]
            if response == 'batch_error':
                raise RuntimeError('Fixture batch transport failure')
            if response == 'callback_error':
                callback(str(index), None, RuntimeError('Fixture request failure'))
            elif response == 'missing_callback':
                continue
            else:
                callback(str(index), response, None)


class FakeDriveService:
    def __init__(self, responses):
        self.responses = responses
        self.list_requests = []
        self.executed_keys = []
        self.batches_executed = 0
        self.close = Mock()

    def files(self):
        return self

    def list(self, **kwargs):
        self.list_requests.append(kwargs)
        return kwargs

    def new_batch_http_request(self):
        return FakeDriveBatch(self)


def drive_file(name):
    return {'id': 'fixture-' + name, 'name': name, 'md5Checksum': 'fixture-md5'}


def drive_folder(name):
    return {'id': name, 'name': name, 'capabilities': {'canAddChildren': True}}


def failing_tree(failure):
    return {
        ('root', None): {'files': [drive_file('visible.step'), drive_folder('child')]},
        ('child', None): failure,
    }


class ScanFixture:
    def __init__(self, module_path, responses):
        self.signals = SimpleNamespace(
            drive_file_list_acquired=Signal(),
            drive_file_list_finished=Signal(),
            error_occurred=Signal(),
        )
        self.service = FakeDriveService(responses)
        self.logger = Mock()
        self.build_calls = []
        self.drive_client = SimpleNamespace(service=self.service, build_drive_files=self.build_items)
        self.drive_client._new_service = lambda: self.drive_client.service
        self.drive_client._close_service = lambda service: service.close() if service is not None else None
        dependencies = dict(
            deque=deque, logging=self.logger,
            traceback=SimpleNamespace(format_exc=lambda: 'fixture traceback'),
            tic=lambda: None, toc=lambda *args: None,
            QEventLoop=FakeEventLoop, QTimer=FakeTimer,
            signal_manager=self.signals, print=lambda *args: None,
        )
        scan = load_function(module_path, 'DriveClient.get_drive_items_from_project_id', dependencies)
        self.drive_client.get_drive_items_from_project_id = MethodType(scan, self.drive_client)
        self.actions = []
        self.comparisons = 0
        self.written_snapshots = []
        self.analyzer = SimpleNamespace(
            _cancelled=False, _drive_files_ready=False, _local_files_ready=True,
            project_id='root', cached_folders=[], drive_client=self.drive_client,
            db_manager=SimpleNamespace(write_drive_items=self.written_snapshots.append),
            save_drive_as_csv=lambda items: None,
            _compare_worker=self.compare_snapshot,
        )
        for method, namespace in (
            ('drive_worker', {}),
            ('_on_drive_file_list_acquired', {'signal_manager': self.signals}),
            ('_on_drive_task_finished', {'logging': self.logger, 'print': lambda *args: None}),
            ('_check_ready', {
                '_FunctionRunnable': lambda func: func,
                'thread_pool': SimpleNamespace(start=lambda func: func()),
            }),
        ):
            function = load_function(ANALYZER, 'SyncAnalyzer.' + method, namespace)
            setattr(self.analyzer, method, MethodType(function, self.analyzer))
        self.signals.drive_file_list_acquired.connect(self.analyzer._on_drive_file_list_acquired)
        self.signals.drive_file_list_finished.connect(self.analyzer._on_drive_task_finished)
        self.compute = load_function(ANALYZER, 'SyncAnalyzer.compute_sync_actions', {
            'SyncAction': lambda **kwargs: SimpleNamespace(**kwargs),
            'SyncActionType': SimpleNamespace(DELETE_LOCAL='delete_local'),
        })

    def build_items(self, folders, files, project_id):
        self.build_calls.append((list(folders), list(files), project_id))
        return [SimpleNamespace(path=item['name']) for item in files]

    def compare_snapshot(self):
        self.comparisons += 1
        remote_paths = {item.path for item in self.written_snapshots[-1]}
        missing_path = 'child/important.step'
        local_item = SimpleNamespace(path=missing_path, md5_checksum='unchanged', size=10, modified_time='fixture-time')
        state = SimpleNamespace(
            drive_not_local=[], local_not_drive=[] if missing_path in remote_paths else [local_item],
            md5_mismatch=[], sync_log_dict={missing_path: {'md5': 'unchanged'}}, read_only=False,
        )
        self.actions = self.compute(state)

    def run(self):
        with deny_external_effects():
            self.analyzer.drive_worker()
        return self


@unittest.skipUnless(all((ROOT / p).is_file() for p in (BASELINE, MAINTAINED, ANALYZER)),
                     'Recovered/maintained source fixtures are absent from wheel-only validation')
class RecoveredScanTests(unittest.TestCase):
    def assert_fails_closed(self, fixture):
        self.assertEqual(fixture.signals.drive_file_list_acquired.calls, [])
        self.assertEqual(fixture.signals.drive_file_list_finished.calls, [])
        self.assertEqual(fixture.written_snapshots, [])
        self.assertEqual(fixture.build_calls, [])
        self.assertEqual(fixture.actions, [])
        self.assertEqual(fixture.comparisons, 0)
        self.assertFalse(fixture.analyzer._drive_files_ready)
        self.assertEqual(len(fixture.signals.error_occurred.calls), 1)
        self.assertIn('Drive scan incomplete', fixture.signals.error_occurred.calls[0][0])

    def test_baseline_callback_error_publishes_partial_scan_and_plans_local_delete(self):
        fixture = ScanFixture(BASELINE, failing_tree('callback_error')).run()
        self.assertEqual([i.path for i in fixture.signals.drive_file_list_acquired.calls[0][0]], ['visible.step'])
        self.assertEqual(fixture.comparisons, 1)
        self.assertEqual(len(fixture.actions), 1)
        self.assertEqual(fixture.actions[0].action, 'delete_local')
        self.assertEqual(fixture.actions[0].reason, 'missing_cloud')
        self.assertEqual(fixture.signals.error_occurred.calls, [])

    def test_baseline_batch_error_publishes_partial_scan_and_plans_local_delete(self):
        fixture = ScanFixture(BASELINE, failing_tree('batch_error')).run()
        self.assertEqual(len(fixture.signals.drive_file_list_acquired.calls), 1)
        self.assertEqual(fixture.actions[0].action, 'delete_local')
        self.assertEqual(fixture.signals.error_occurred.calls, [])

    def test_maintained_callback_batch_and_missing_callback_fail_closed(self):
        for failure in ('callback_error', 'batch_error', 'missing_callback'):
            with self.subTest(failure=failure):
                self.assert_fails_closed(ScanFixture(MAINTAINED, failing_tree(failure)).run())

    def test_maintained_incomplete_search_flag_fails_closed(self):
        fixture = ScanFixture(MAINTAINED, failing_tree({'files': [], 'incompleteSearch': True})).run()
        self.assert_fails_closed(fixture)
        self.assertTrue(all('incompleteSearch' in request['fields'] for request in fixture.service.list_requests))

    def test_maintained_malformed_callback_response_fails_closed(self):
        self.assert_fails_closed(ScanFixture(MAINTAINED, failing_tree(None)).run())

    def test_maintained_later_page_failure_discards_earlier_page(self):
        responses = {
            ('root', None): {'files': [drive_file('visible.step')], 'nextPageToken': 'page-2'},
            ('root', 'page-2'): 'callback_error',
        }
        fixture = ScanFixture(MAINTAINED, responses).run()
        self.assert_fails_closed(fixture)
        self.assertEqual(fixture.service.executed_keys, [('root', None), ('root', 'page-2')])

    def test_maintained_complete_nested_paginated_scan_emits_once(self):
        responses = {
            ('root', None): {'files': [drive_folder('child')], 'nextPageToken': 'page-2'},
            ('root', 'page-2'): {'files': [drive_file('visible.step')]},
            ('child', None): {'files': [drive_file('child/important.step')]},
        }
        fixture = ScanFixture(MAINTAINED, responses).run()
        self.assertEqual(fixture.signals.error_occurred.calls, [])
        self.assertEqual(len(fixture.signals.drive_file_list_acquired.calls), 1)
        self.assertEqual(len(fixture.signals.drive_file_list_finished.calls), 1)
        self.assertEqual(fixture.comparisons, 1)
        self.assertEqual(fixture.actions, [])
        self.assertEqual(len(fixture.build_calls), 1)
        self.assertEqual(set(fixture.service.executed_keys), set(responses))

    def test_cached_root_initialization_is_fixed_without_changing_baseline(self):
        original = ScanFixture(BASELINE, {('root', None): {'files': []}})
        original.analyzer.cached_folders = ['root']
        with self.assertRaises(UnboundLocalError):
            original.run()
        fixed = ScanFixture(MAINTAINED, {('root', None): {'files': []}})
        fixed.analyzer.cached_folders = ['root', 'root']
        fixed.run()
        self.assertEqual(fixed.service.executed_keys, [('root', None)])
        self.assertEqual(fixed.signals.error_occurred.calls, [])
        self.assertEqual(fixed.signals.drive_file_list_acquired.calls, [([], )])

    def test_maintained_complete_empty_scan_is_valid(self):
        fixture = ScanFixture(MAINTAINED, {('root', None): {'files': []}}).run()
        self.assertEqual(fixture.signals.drive_file_list_acquired.calls, [([], )])
        self.assertEqual(fixture.signals.error_occurred.calls, [])
        self.assertEqual(fixture.comparisons, 1)
        self.assertEqual(fixture.actions[0].action, 'delete_local')


if __name__ == '__main__':
    unittest.main()
