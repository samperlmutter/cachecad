"""Generation-bound scanner outcomes using real selected maintained functions.

No application import or class initializer executes. Drive and Qt are explicit
in-memory doubles; filesystem operations stay in one disposable test directory.
"""
from collections import deque
from pathlib import Path, PureWindowsPath
from threading import RLock
from types import MethodType, SimpleNamespace
import os
import re
import tempfile
import unittest
from unittest.mock import Mock

from recovered_harness import ROOT, deny_external_effects, load_function
from test_recovered_scan import FakeDriveService, FakeEventLoop, FakeTimer, Signal, drive_file, failing_tree

DRIVE = 'maintained/core/drive_client.py'
LOCAL = 'maintained/core/local_client.py'
ORIGINAL_LOCAL = 'recovered/core/local_client.py'
DIRECT_CONNECTION = object()


class HashSignal(Signal):
    def __init__(self):
        super().__init__()
        self.connection_types = []

    def connect(self, slot, connection_type=None):
        self.connection_types.append(connection_type)
        super().connect(slot)


class HashSignals:
    def __init__(self):
        self.finished = HashSignal()
        self.error = HashSignal()


class HashJob:
    def __init__(self, path, rel_path, signals):
        self.path = path
        self.rel_path = rel_path
        self.signals = signals

    def succeed(self, checksum='fixture-md5'):
        self.signals.finished.emit(self.rel_path, checksum)

    def fail(self):
        self.signals.error.emit(self.rel_path, 'Fixture hash read failed')


class LocalFixture:
    def __init__(self, root, source=LOCAL, walker=os.walk, start=None):
        self.root = Path(root)
        self.jobs = []
        self.successes = []
        self.errors = []
        self.globals = SimpleNamespace(local_file_list_acquired=Signal(), error_occurred=Signal())
        self.client = SimpleNamespace(
            _scan_states={}, _active_signals=[], _pending_md5=0, _file_dict={},
            md5_cache={'unchanged-cache-entry': {'md5': 'old-cache'}}, cache_path=None,
            _save_md5_cache=Mock(),
        )
        self.namespace = dict(
            Path=Path, os=SimpleNamespace(walk=walker), RLock=RLock,
            Qt=SimpleNamespace(ConnectionType=SimpleNamespace(DirectConnection=DIRECT_CONNECTION)),
            LocalFileInfo=SimpleNamespace(from_path=self.from_path),
            HashWorkerSignals=HashSignals, HashWorker=HashJob,
            thread_pool=SimpleNamespace(start=start or self.jobs.append),
            signal_manager=self.globals, logging=Mock(), print=lambda *args: None,
        )
        method = load_function(source, 'LocalClient.get_local_items_from_path', self.namespace)
        self.client.get_local_items_from_path = MethodType(method, self.client)
        if source == ORIGINAL_LOCAL:
            for name in ('_on_hash_result', '_on_hash_error', '_check_scan_complete', '_emit_complete'):
                function = load_function(source, 'LocalClient.' + name, self.namespace)
                setattr(self.client, name, MethodType(function, self.client))

    def from_path(self, path, project_folder, md5_checksum=None):
        if not Path(path).resolve().is_relative_to(self.root.resolve()):
            raise AssertionError('Unexpected filesystem path outside disposable fixture')
        stat = path.stat()
        return SimpleNamespace(path=str(path.relative_to(project_folder)), full_path=str(path.resolve()),
                               md5_checksum=md5_checksum, size=stat.st_size,
                               modified_time=stat.st_mtime, is_folder=path.is_dir())

    def start(self, project, scan_id=None):
        if scan_id is None:
            return self.client.get_local_items_from_path(project, str(self.root))
        return self.client.get_local_items_from_path(
            project, str(self.root), scan_id=scan_id,
            on_success=lambda generation, items: self.successes.append((generation, items)),
            on_error=lambda generation, message: self.errors.append((generation, message)),
        )


class DriveFixture:
    def __init__(self, responses):
        self.successes = []
        self.errors = []
        self.globals = SimpleNamespace(drive_file_list_acquired=Signal(), error_occurred=Signal())
        self.service = FakeDriveService(responses)
        self.client = SimpleNamespace(service=self.service, build_drive_files=self.build_items)
        self.namespace = dict(
            deque=deque, logging=Mock(), traceback=SimpleNamespace(format_exc=lambda: 'fixture'),
            tic=lambda: None, toc=lambda *args: None, print=lambda *args: None,
            QEventLoop=FakeEventLoop, QTimer=FakeTimer, signal_manager=self.globals,
        )
        self.scan = load_function(DRIVE, 'DriveClient.get_drive_items_from_project_id', self.namespace)

    def build_items(self, folders, files, root_id, *, report_error=None):
        return [SimpleNamespace(path=item['name']) for item in files]

    def start(self, scan_id, cached_folders=None):
        self.scan(self.client, 'root', cached_folders,
                  scan_id=scan_id,
                  on_success=lambda generation, items: self.successes.append((generation, items)),
                  on_error=lambda generation, message: self.errors.append((generation, message)))


@unittest.skipUnless(all((ROOT / p).is_file() for p in (DRIVE, LOCAL, ORIGINAL_LOCAL)),
                     'Recovered/maintained source fixtures absent from wheel-only validation')
class ScanOutcomeTests(unittest.TestCase):
    def setUp(self):
        effects = deny_external_effects()
        effects.__enter__()
        self.addCleanup(effects.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory(prefix='cachecad-scan-outcomes-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def project(self, name, files=('drawing.step',)):
        directory = self.root / name
        directory.mkdir()
        for filename in files:
            path = directory / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('disposable fixture ' + name)
        return directory

    def assert_no_global_outcome(self, fixture):
        self.assertEqual(fixture.globals.error_occurred.calls, [])
        acquired = getattr(fixture.globals, 'drive_file_list_acquired', None)
        if acquired is None:
            acquired = fixture.globals.local_file_list_acquired
        self.assertEqual(acquired.calls, [])

    def test_original_hash_failure_still_publishes_partial_local_snapshot(self):
        self.project('Original')
        fixture = LocalFixture(self.root, ORIGINAL_LOCAL)
        fixture.start('Original')
        fixture.jobs[0].fail()
        items = fixture.globals.local_file_list_acquired.calls[0][0]
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0].md5_checksum)
        self.assertEqual(fixture.globals.error_occurred.calls, [])

    def test_local_hash_failure_is_one_tagged_error_and_never_partial_success(self):
        self.project('Current', ('first.step', 'second.step'))
        fixture = LocalFixture(self.root)
        fixture.start('Current', 20)
        fixture.jobs[0].fail()
        fixture.jobs[1].succeed()
        fixture.jobs[0].succeed('late-duplicate')
        self.assertEqual(fixture.successes, [])
        self.assertEqual(len(fixture.errors), 1)
        self.assertEqual(fixture.errors[0][0], 20)
        self.assertEqual(fixture.client._scan_states, {})
        self.assert_no_global_outcome(fixture)

    def test_hash_callbacks_explicitly_use_direct_connection_without_worker_event_loop(self):
        self.project('Current')
        fixture = LocalFixture(self.root)
        fixture.start('Current', 21)
        job = fixture.jobs[0]
        self.assertEqual(job.signals.finished.connection_types, [DIRECT_CONNECTION])
        self.assertEqual(job.signals.error.connection_types, [DIRECT_CONNECTION])
        job.succeed()
        self.assertEqual([generation for generation, _ in fixture.successes], [21])
        self.assertEqual(fixture.errors, [])
        self.assert_no_global_outcome(fixture)

    def test_old_local_success_cannot_change_new_scan_pending_items_or_shared_cache(self):
        self.project('Old')
        self.project('New')
        fixture = LocalFixture(self.root)
        fixture.start('Old', 100)
        fixture.start('New', 101)
        old_job, new_job = fixture.jobs
        old_job.succeed('old-hash')
        self.assertEqual([generation for generation, _ in fixture.successes], [100])
        self.assertEqual(len(fixture.client._scan_states), 1)
        remaining = next(iter(fixture.client._scan_states.values()))
        self.assertEqual(remaining['pending'], {'drawing.step'})
        self.assertEqual(fixture.client.md5_cache, {'unchanged-cache-entry': {'md5': 'old-cache'}})
        new_job.succeed('new-hash')
        old_job.succeed('stale-duplicate')
        self.assertEqual([generation for generation, _ in fixture.successes], [100, 101])
        old_item, new_item = fixture.successes[0][1][0], fixture.successes[1][1][0]
        self.assertIn('/Old/', old_item.full_path)
        self.assertIn('/New/', new_item.full_path)
        self.assertEqual(old_item.md5_checksum, 'old-hash')
        self.assertEqual(new_item.md5_checksum, 'new-hash')
        self.assertEqual(fixture.client._scan_states, {})
        fixture.client._save_md5_cache.assert_not_called()
        self.assert_no_global_outcome(fixture)

    def test_old_local_error_retains_old_id_and_cannot_settle_new_scan(self):
        self.project('Old')
        self.project('New', ('first.step', 'second.step'))
        fixture = LocalFixture(self.root)
        fixture.start('Old', 1)
        fixture.start('New', 2)
        fixture.jobs[0].fail()
        fixture.jobs[1].succeed('one')
        self.assertEqual([generation for generation, _ in fixture.errors], [1])
        self.assertEqual(fixture.successes, [])
        self.assertEqual(next(iter(fixture.client._scan_states.values()))['pending'], {fixture.jobs[2].rel_path})
        fixture.jobs[2].succeed('two')
        self.assertEqual([generation for generation, _ in fixture.successes], [2])
        self.assert_no_global_outcome(fixture)

    def test_local_missing_root_is_error_not_empty_success(self):
        fixture = LocalFixture(self.root)
        fixture.start('DoesNotExist', 5)
        self.assertEqual(fixture.successes, [])
        self.assertEqual(fixture.errors[0][0], 5)
        self.assertEqual(fixture.client._scan_states, {})
        self.assert_no_global_outcome(fixture)

    def test_local_traversal_failure_is_error_not_partial_success(self):
        self.project('Unreadable')
        def failed_walk(path, *, onerror):
            yield str(path), [], ['drawing.step']
            onerror(PermissionError('Fixture directory permission denied'))
        fixture = LocalFixture(self.root, walker=failed_walk)
        fixture.start('Unreadable', 6)
        self.assertEqual(fixture.jobs, [])
        self.assertEqual(fixture.successes, [])
        self.assertEqual(fixture.errors[0][0], 6)
        self.assertEqual(fixture.client._scan_states, {})
        self.assert_no_global_outcome(fixture)

    def test_local_symlinked_subtree_is_error_instead_of_missing_local_files(self):
        target = self.project('Target')
        project = self.project('LinkProject', ())
        (project / 'linked').symlink_to(target, target_is_directory=True)
        fixture = LocalFixture(self.root)
        fixture.start('LinkProject', 66)
        self.assertEqual(fixture.successes, [])
        self.assertEqual(fixture.errors[0][0], 66)
        self.assertIn('Symlink', fixture.errors[0][1])
        self.assert_no_global_outcome(fixture)

    def test_local_symlinked_project_root_is_error(self):
        target = self.project('Target')
        (self.root / 'RootLink').symlink_to(target, target_is_directory=True)
        fixture = LocalFixture(self.root)
        fixture.start('RootLink', 67)
        self.assertEqual(fixture.successes, [])
        self.assertEqual(fixture.errors[0][0], 67)
        self.assert_no_global_outcome(fixture)

    def test_local_disappearing_entry_is_error_not_partial_success(self):
        project = self.project('Vanished')
        def vanished_walk(path, *, onerror):
            (project / 'drawing.step').unlink()
            yield str(path), [], ['drawing.step']
        fixture = LocalFixture(self.root, walker=vanished_walk)
        fixture.start('Vanished', 68)
        self.assertEqual(fixture.successes, [])
        self.assertEqual(fixture.errors[0][0], 68)
        self.assertEqual(fixture.jobs, [])
        self.assert_no_global_outcome(fixture)

    def test_local_scheduler_failure_does_not_leak_pending_scan_state(self):
        self.project('Queue', ('first.step', 'second.step'))
        def reject_job(job):
            raise RuntimeError('Fixture scheduler failure')
        fixture = LocalFixture(self.root, start=reject_job)
        fixture.start('Queue', 7)
        self.assertEqual(fixture.successes, [])
        self.assertEqual(len(fixture.errors), 1)
        self.assertEqual(fixture.client._scan_states, {})
        self.assert_no_global_outcome(fixture)

    def test_local_complete_empty_directory_has_one_success(self):
        self.project('Empty', ())
        fixture = LocalFixture(self.root)
        fixture.start('Empty', 8)
        self.assertEqual(fixture.successes, [(8, [])])
        self.assertEqual(fixture.errors, [])
        self.assertEqual(fixture.client._scan_states, {})
        self.assert_no_global_outcome(fixture)

    def test_legacy_local_hash_failure_also_fails_closed(self):
        self.project('Legacy')
        fixture = LocalFixture(self.root)
        fixture.start('Legacy')
        fixture.jobs[0].fail()
        self.assertEqual(fixture.globals.local_file_list_acquired.calls, [])
        self.assertEqual(len(fixture.globals.error_occurred.calls), 1)

    def test_drive_failure_has_one_tagged_error_and_no_global_signals(self):
        for failure in ('callback_error', 'batch_error', 'missing_callback', None,
                        {'files': [], 'incompleteSearch': True}):
            with self.subTest(failure=failure):
                fixture = DriveFixture(failing_tree(failure))
                fixture.start(42)
                self.assertEqual(fixture.successes, [])
                self.assertEqual(len(fixture.errors), 1)
                self.assertEqual(fixture.errors[0][0], 42)
                self.assert_no_global_outcome(fixture)

    def test_drive_cached_root_duplicates_are_scanned_once(self):
        fixture = DriveFixture({('root', None): {'files': []}})
        fixture.start(43, ['root', 'root'])
        self.assertEqual(fixture.successes, [(43, [])])
        self.assertEqual(fixture.errors, [])
        self.assertEqual(fixture.service.executed_keys, [('root', None)])
        self.assert_no_global_outcome(fixture)

    def test_drive_initial_request_failure_becomes_tagged_error(self):
        fixture = DriveFixture({})
        fixture.service.files = Mock(side_effect=RuntimeError('Fixture setup failure'))
        fixture.start(44)
        self.assertEqual(fixture.successes, [])
        self.assertEqual(len(fixture.errors), 1)
        self.assertEqual(fixture.errors[0][0], 44)
        self.assert_no_global_outcome(fixture)

    def test_late_drive_callback_never_relabels_old_outcome_as_new_generation(self):
        fixture = DriveFixture({('root', None): {'files': []}})
        held = []
        class DelayedBatch:
            def add(self, request, callback):
                held.append(callback)
            def execute(self):
                pass
        fixture.service.new_batch_http_request = DelayedBatch
        fixture.start(50)
        fixture.client.service = FakeDriveService({('root', None): {'files': []}})
        fixture.start(51)
        held[0]('late', {'files': [drive_file('late.step')]}, None)
        self.assertEqual([generation for generation, _ in fixture.errors], [50])
        self.assertEqual(fixture.successes, [(51, [])])
        self.assert_no_global_outcome(fixture)

    def test_drive_duplicate_conversion_reports_only_tagged_error(self):
        duplicate_files = [dict(drive_file('same.step'), id=str(i), parents=['root']) for i in range(2)]
        fixture = DriveFixture({('root', None): {'files': duplicate_files}})
        class FakeDriveItem:
            @staticmethod
            def from_drive_item(item, full_path):
                return SimpleNamespace(path=full_path, name=item['name'], id=item['id'], is_folder=False)
        build = load_function(DRIVE, 'DriveClient.build_drive_files', dict(
            fixture.namespace, PureWindowsPath=PureWindowsPath, re=re, DriveItemInfo=FakeDriveItem))
        fixture.client.build_drive_files = MethodType(build, fixture.client)
        fixture.start(60)
        self.assertEqual(fixture.successes, [])
        self.assertEqual(len(fixture.errors), 1)
        self.assertEqual(fixture.errors[0][0], 60)
        self.assertIn('duplicate', fixture.errors[0][1])
        self.assert_no_global_outcome(fixture)

    @unittest.skipUnless((ROOT / 'maintained/core/sync_analyzer.py').is_file() and
                         (ROOT / 'maintained/app/app_controller.py').is_file(),
                         'Analyzer/controller lifecycle fixtures unavailable')
    def test_real_adapters_feed_real_lifecycle_failure_retry_and_ignore_stale_hashes(self):
        import test_recovered_scan_lifecycle as lifecycle
        case = lifecycle.LifecycleTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.project('Current', ('one.step', 'two.step'))
        local = LocalFixture(self.root)
        drive = DriveFixture({('root', None): {'files': []}})
        drive.client.get_drive_items_from_project_id = MethodType(drive.scan, drive.client)
        analyzer = case.analyzer()
        analyzer.local_client = local.client
        analyzer.drive_client = drive.client
        analyzer._load_cached_folders.return_value = []
        controller = case.controller(analyzer)
        controller.working_directory = self.root
        controller.project_name = 'Current'
        controller.project_id = 'root'
        controller.on_refresh_clicked()
        case.pool.drain()
        old = analyzer._generation
        analyzer.cancel_sync_analysis()
        controller.on_refresh_clicked()
        case.pool.drain()
        failed = analyzer._generation
        self.assertGreater(failed, old)
        local.jobs[2].fail()
        self.assertFalse(controller.buttons_disabled)
        self.assertFalse(controller.analysis_ready)
        self.assertEqual(len(analyzer.analysis_failed.calls), 1)
        controller.on_refresh_clicked()
        case.pool.drain()
        current = analyzer._generation
        self.assertGreater(current, failed)
        for job in local.jobs[:2]:
            job.succeed('cancelled-old-hash')
        local.jobs[3].succeed('failed-old-hash')
        analyzer.db_manager.reset.assert_not_called()
        self.assertFalse(controller.analysis_ready)
        self.assertTrue(controller.buttons_disabled)
        for job in local.jobs[4:]:
            job.succeed('current-hash')
        self.assertEqual(len(analyzer.analysis_succeeded.calls), 1)
        self.assertEqual(analyzer.analysis_succeeded.calls[0][0], current)
        analyzer.db_manager.write_local_items.assert_called_once()
        written = analyzer.db_manager.write_local_items.call_args.args[0]
        self.assertTrue(all(item.md5_checksum == 'current-hash' for item in written))
        self.assertTrue(controller.analysis_ready)
        self.assertFalse(controller.buttons_disabled)
        self.assertEqual(local.client._scan_states, {})
        self.assert_no_global_outcome(local)
        self.assert_no_global_outcome(drive)

    def test_partial_tagged_contract_is_rejected_before_scan(self):
        self.project('Contract')
        local = LocalFixture(self.root)
        drive = DriveFixture({})
        with self.assertRaises(ValueError):
            local.client.get_local_items_from_path('Contract', str(self.root), scan_id=1)
        with self.assertRaises(ValueError):
            drive.scan(drive.client, 'root', scan_id=1)
        self.assertEqual(local.jobs, [])
        self.assertEqual(drive.service.list_requests, [])


if __name__ == '__main__':
    unittest.main()
