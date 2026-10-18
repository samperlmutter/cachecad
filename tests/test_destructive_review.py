"""Independent adversarial review of the maintained observed-version boundary.

Uses the real pure-Python helper and disposable fixtures only. No original
application initialization, credentials, network, or user files are accessed.
Passing these checks does not establish atomic filesystem or Drive updates.
"""
from dataclasses import replace
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

from recovered_harness import deny_external_effects, load_function
from test_recovered_overwrite import bind_recovery_executor


ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'maintained/core/sync_consent.py').is_file():
    raise unittest.SkipTest('Actual application sources absent from experimental wheel')
SPEC = importlib.util.spec_from_file_location(
    'independent_destructive_review_guard', ROOT / 'maintained/core/sync_consent.py')
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)
FOLDER = 'application/vnd.google-apps.folder'


class IndependentDestructiveReviewTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.working = self.temp / 'container'
        self.root = self.working / 'project'
        self.root.mkdir(parents=True)
        self.data = b'fixture version'
        self.md5 = hashlib.md5(self.data).hexdigest()
        self.responses = {'root-id': dict(id='root-id', name='project',
            mimeType=FOLDER, trashed=False)}
        self.pages = [dict(files=[], incompleteSearch=False)]
        self.files = SimpleNamespace(get=Mock(side_effect=self.get),
            list=Mock(side_effect=self.list_page))
        self.services = []
        self.drive = SimpleNamespace(_new_service=Mock(side_effect=self.new_service))

    def get(self, **kwargs):
        return SimpleNamespace(execute=lambda: dict(self.responses[kwargs['fileId']]))

    def list_page(self, **kwargs):
        page = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        return SimpleNamespace(execute=lambda: page)

    def new_service(self):
        service = SimpleNamespace(files=Mock(return_value=self.files), close=Mock())
        self.services.append(service)
        return service

    def prepare(self, kind='delete_local', path='part.sldprt', folders=()):
        relative = Path(path)
        local_path = self.root / relative
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_items = []
        remote_items = []
        folder_map = {}
        parent_id = 'root-id'
        for number, name in enumerate(folders, 1):
            folder_id = f'folder-{number}'
            folder_path = str(Path(*relative.parts[:number]))
            remote_items.append(SimpleNamespace(path=folder_path, id=folder_id,
                name=name, parent=parent_id, is_folder=True))
            self.responses[folder_id] = dict(id=folder_id, name=name,
                parents=[parent_id], mimeType=FOLDER, trashed=False)
            folder_map[folder_path] = folder_id
            parent_id = folder_id
        if kind != 'delete_cloud':
            local_path.write_bytes(self.data)
            local_items.append(SimpleNamespace(path=str(relative), is_folder=False,
                size=len(self.data), md5_checksum=self.md5,
                modified_time=local_path.stat().st_mtime))
        if kind != 'delete_local':
            remote_items.append(SimpleNamespace(path=str(relative), is_folder=False,
                id='file-id', name=relative.name, parent=parent_id, version='10',
                size=len(self.data), md5_checksum=self.md5))
            self.responses['file-id'] = dict(id='file-id', name=relative.name,
                parents=[parent_id], version='10', size=str(len(self.data)),
                md5Checksum=self.md5, mimeType='application/octet-stream', trashed=False)
        action = SimpleNamespace(path=str(relative), action=SimpleNamespace(value=kind),
            reason='fixture', size=len(self.data), md5_checksum=self.md5,
            parent_id=parent_id, modified_by='', modified_time='',
            drive_id='file-id' if kind != 'delete_local' else None,
            name=relative.name, folder=str(relative.parent), parent_name=relative.parent.name)
        snapshot = guard.capture_scan_snapshot([action], local_items, remote_items,
            self.working, 'project', 'root-id', folder_map)
        self.action = (4, action)
        self.consent = guard.approve_sync_actions([self.action], snapshot)
        return local_path

    def check(self):
        return guard.revalidate_action(self.consent, self.action, self.drive)

    def test_missing_parent_is_checked_under_deepest_verified_ancestor(self):
        self.prepare(path='existing/missing/deeper/part.sldprt', folders=('existing',))
        self.check()
        self.assertEqual(self.consent.snapshot.actions[0].absent_name, 'missing')
        self.assertEqual(self.files.list.call_args.kwargs['q'],
            "'folder-1' in parents and name = 'missing' and trashed = false")
        self.assertEqual([c.kwargs['fileId'] for c in self.files.get.call_args_list],
            ['root-id', 'folder-1'])

    def test_new_parent_folder_blocks_delete_of_locally_remaining_file(self):
        local = self.prepare(path='missing/deeper/part.sldprt')
        self.pages = [dict(files=[dict(id='new-parent')])]
        with self.assertRaises(guard.StaleSyncPlan):
            self.check()
        self.assertEqual(local.read_bytes(), self.data)

    def test_absence_search_rejects_a_match_on_a_later_page(self):
        self.prepare()
        self.pages = [dict(files=[], nextPageToken='next'), dict(files=[dict(id='late')])]
        with self.assertRaises(guard.StaleSyncPlan):
            self.check()
        self.assertEqual(self.files.list.call_args_list[1].kwargs['pageToken'], 'next')

    def test_repeated_absence_page_token_fails_closed(self):
        self.prepare()
        self.pages = [dict(files=[], nextPageToken='repeated')]
        with self.assertRaises(guard.StaleSyncPlan):
            self.check()
        self.assertEqual(self.files.list.call_count, 2)

    def test_absence_query_escapes_quote_and_backslash_without_operators(self):
        self.prepare()
        observed = replace(self.consent.snapshot.actions[0], absent_name="part'\\name")
        snapshot = replace(self.consent.snapshot, actions=(observed,))
        consent = replace(self.consent, snapshot=snapshot)
        guard._verify_remote_files(consent, observed, self.files)
        self.assertEqual(self.files.list.call_args.kwargs['q'],
            "'root-id' in parents and name = 'part\\'\\\\name' and trashed = false")

    def test_moved_existing_remote_parent_blocks_absence_approval(self):
        self.prepare(path='existing/missing/part.sldprt', folders=('existing',))
        self.responses['folder-1']['parents'] = ['other-root']
        with self.assertRaises(guard.StaleSyncPlan):
            self.check()
        self.files.list.assert_not_called()

    def test_remote_root_must_still_be_an_untrashed_folder(self):
        self.prepare()
        self.responses['root-id']['mimeType'] = 'application/octet-stream'
        with self.assertRaises(guard.StaleSyncPlan):
            self.check()
        self.files.list.assert_not_called()

    def test_local_directory_appearing_blocks_cloud_delete(self):
        local = self.prepare('delete_cloud')
        local.mkdir()
        with self.assertRaises(guard.StaleSyncPlan):
            self.check()

    def test_local_parent_symlink_replacement_blocks_cloud_delete(self):
        local = self.prepare('delete_cloud', 'parts/part.sldprt', ('parts',))
        local.parent.rmdir()
        alternate = self.temp / 'alternate'
        alternate.mkdir()
        local.parent.symlink_to(alternate, target_is_directory=True)
        with self.assertRaises(guard.StaleSyncPlan):
            self.check()

    def test_project_ancestor_symlink_replacement_blocks_cloud_delete(self):
        self.prepare('delete_cloud')
        displaced = self.temp / 'displaced'
        self.working.rename(displaced)
        self.working.symlink_to(displaced, target_is_directory=True)
        with self.assertRaises(guard.StaleSyncPlan):
            self.check()

    def test_each_revalidation_owns_and_closes_a_separate_drive_transport(self):
        self.prepare()
        self.check()
        self.check()
        self.assertEqual(len(self.services), 2)
        self.assertIsNot(self.services[0], self.services[1])
        for service in self.services:
            service.close.assert_called_once_with()

    def test_failed_remote_lookup_still_closes_owned_transport(self):
        self.prepare('delete_cloud')
        self.files.get.side_effect = TimeoutError('fixture timeout')
        with self.assertRaises(guard.StaleSyncPlan):
            self.check()
        self.services[0].close.assert_called_once_with()

    def test_selected_action_mutation_blocks_before_remote_lookup(self):
        self.prepare('upload_revision')
        self.action[1].drive_id = 'different-file'
        with self.assertRaises(guard.StaleSyncPlan):
            self.check()
        self.drive._new_service.assert_not_called()

    def test_directory_traversal_and_absolute_paths_are_rejected(self):
        for path in ('../outside.sldprt', str(self.temp / 'outside.sldprt'), '.'):
            with self.subTest(path=path), self.assertRaises(guard.StaleSyncPlan):
                guard.project_path(self.root, path)


class IndependentExecutorLifecycleReviewTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())
        self.signals = SimpleNamespace(**{name: SimpleNamespace(emit=Mock(), disconnect=Mock())
            for name in ('dropped_actions_acknowledged', 'action_complete', 'action_failed',
                         'sync_cancelled', 'sync_execution_finished')})
        self.pool = SimpleNamespace(start=Mock())
        namespace = dict(signal_manager=self.signals, logging=Mock(), network_pool=self.pool,
            _FunctionRunnable=Mock(), validate_selection=Mock(), StaleSyncPlan=guard.StaleSyncPlan)
        self.executor = SimpleNamespace(_started=False, _finished=False, cancelled=False,
            consent=None, action_tuple=(), sync_worker=Mock(), deleteLater=Mock())
        bind_recovery_executor(self.executor, namespace)
        self.executor._counted_actions = set()
        self.executor.outcome_ready = SimpleNamespace(emit=Mock(), disconnect=Mock())
        for name in ('start_sync', '_finish_execution', 'on_action_complete',
                     'on_action_failed', 'on_sync_cancelled', 'check_if_all_actions_finished',
                     'on_transfer_outcome'):
            method = load_function('maintained/core/sync_executor.py', 'SyncExecutor.' + name, namespace)
            setattr(self.executor, name, MethodType(method, self.executor))
        self.executor.outcome_ready.emit.side_effect = self.executor.on_transfer_outcome

    def test_all_dropped_actions_finish_once_without_scheduling_a_worker(self):
        self.executor.start_sync()
        self.executor.start_sync()
        self.pool.start.assert_not_called()
        self.signals.sync_execution_finished.emit.assert_called_once_with()
        self.executor.deleteLater.assert_called_once_with()

    def test_cancellation_before_start_is_never_reset(self):
        self.executor.action_tuple = ((1, SimpleNamespace(path='part.step', action='upload_new')),)
        self.executor.on_sync_cancelled()
        self.executor.start_sync()
        self.assertTrue(self.executor.cancelled)
        self.pool.start.assert_not_called()
        self.signals.sync_execution_finished.emit.assert_called_once_with()
        self.assertEqual(self.executor.cancelled_action_count, 1)
        self.assertEqual(self.executor.action_results[0]['outcome'], 'not_started')

    def test_terminal_emission_is_reentrant_safe_and_disconnects_all_global_slots(self):
        self.signals.sync_execution_finished.emit.side_effect = self.executor._finish_execution
        self.executor._finish_execution()
        self.signals.sync_execution_finished.emit.assert_called_once_with()
        for name, method in (('dropped_actions_acknowledged', 'start_sync'),
                             ('sync_cancelled', 'on_sync_cancelled')):
            getattr(self.signals, name).disconnect.assert_called_once_with(getattr(self.executor, method))
        self.executor.outcome_ready.disconnect.assert_called_once_with(self.executor.on_transfer_outcome)
        # Untagged completion/failure UI signals are no longer subscribed to.
        self.signals.action_complete.disconnect.assert_not_called()
        self.signals.action_failed.disconnect.assert_not_called()
        self.executor.deleteLater.assert_called_once_with()

    def test_late_action_events_after_finish_have_no_effect(self):
        self.executor._finish_execution()
        self.executor.on_action_complete(object())
        self.executor.on_action_failed()
        self.executor.on_transfer_outcome(dict(action_index=1, path='part.step',
            action='upload_new', outcome='committed', cancelled=False))
        self.signals.sync_execution_finished.emit.assert_called_once_with()
        self.assertFalse(hasattr(self.executor, 'complete_action_count'))


if __name__ == '__main__':
    unittest.main()
