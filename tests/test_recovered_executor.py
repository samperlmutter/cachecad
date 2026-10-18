"""Run selected recovered/maintained methods with in-memory fakes only.

No application module is imported. Maintained new uploads use disposable fixture files.
These tests establish bounded terminal-event behavior, not Qt/thread integration.
"""
import hashlib
import logging
import os
import tempfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import traceback
import unittest
from unittest.mock import Mock

from recovered_harness import load_function, deny_external_effects
from test_recovered_overwrite import load_staging_function

ROOT = Path(__file__).resolve().parents[1]


class Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


@unittest.skipUnless(
    (ROOT / "recovered/core/sync_executor.py").is_file()
    and (ROOT / "maintained/core/sync_executor.py").is_file(),
    "recovery and maintenance source fixtures unavailable in wheel-only tests",
)
class ExecutorTests(unittest.TestCase):
    def setUp(self):
        if not (ROOT / 'recovered/core/sync_executor.py').is_file() or not (ROOT / 'maintained/core/sync_executor.py').is_file():
            self.skipTest('Recovered and maintained sources are source-distribution fixtures')
        self.enterContext(deny_external_effects())

    def setup_case(self, tree, result=None, error=None, cancelled=False):
        signals = SimpleNamespace(**{name: Signal() for name in
            ('action_failed', 'action_complete', 'error_occurred')})
        working_directory = '/fake'
        checksum = 'digest'
        size = None
        modified_time = None
        if tree == 'maintained':
            folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
            working_directory = str(folder)
            local_path = folder / 'project' / 'part.sldprt'
            local_path.parent.mkdir()
            local_path.write_bytes(b'fixture')
            checksum = hashlib.md5(b'fixture').hexdigest()
            size = local_path.stat().st_size
            modified_time = local_path.stat().st_mtime
            if isinstance(result, tuple):
                result = tuple(checksum if value == 'digest' else value for value in result)
        namespace = {'signal_manager': signals, 'logging': Mock(spec=logging),
                     'traceback': traceback, 'os': os,
                     'Path': Path if tree == 'maintained' else PurePosixPath}
        source = ROOT / tree / 'core/sync_executor.py'
        upload = (load_staging_function('SyncExecutor._upload', namespace, folder)
                  if tree == 'maintained' else load_function(source, 'SyncExecutor._upload', namespace))
        delete = load_function(source, 'SyncExecutor._delete_cloud', namespace)
        run = load_function(source, '_FunctionRunnable.run', namespace)
        drive = SimpleNamespace(upload_file=Mock(return_value=result, side_effect=error),
                                delete_file=Mock(return_value=result, side_effect=error))
        executor = SimpleNamespace(cancelled=cancelled, working_directory=working_directory,
                                   project_name='project', drive_client=drive,
                                   get_parent_id=Mock(return_value='parent'),
                                   log_sync_action=Mock())
        action = (7, SimpleNamespace(path='part.sldprt', drive_id='old', md5_checksum=checksum,
                                     size=size, modified_time=modified_time))
        return executor, action, signals, upload, delete, run

    def assert_terminal(self, signals, success):
        self.assertEqual(len(signals.action_complete.calls), int(success))
        self.assertEqual(len(signals.action_failed.calls), int(not success))
        self.assertEqual(len(signals.action_complete.calls) + len(signals.action_failed.calls), 1)

    def test_baseline_none_upload_wrapper_loses_terminal_event(self):
        obj, action, signals, upload, _, run = self.setup_case('recovered')
        run(SimpleNamespace(func=lambda: upload(obj, action)))
        self.assertEqual(signals.action_complete.calls, [])
        self.assertEqual(signals.action_failed.calls, [])
        obj.log_sync_action.assert_not_called()

    def test_baseline_false_delete_incorrectly_logs_success(self):
        obj, action, signals, _, delete, _ = self.setup_case('recovered', False)
        delete(obj, action)
        self.assert_terminal(signals, True)
        obj.log_sync_action.assert_called_once_with(action, id='old')

    def test_fixed_upload_failure_results_have_one_terminal_event(self):
        for result in (None, (), ('id',), (None, 'digest'), ('id', 'wrong')):
            with self.subTest(result=result):
                obj, action, signals, upload, _, run = self.setup_case('maintained', result)
                run(SimpleNamespace(func=lambda: upload(obj, action)))
                self.assert_terminal(signals, False)
                obj.log_sync_action.assert_not_called()

    def test_fixed_upload_exception_has_one_terminal_event(self):
        obj, action, signals, upload, _, run = self.setup_case('maintained', error=RuntimeError('fake failure'))
        run(SimpleNamespace(func=lambda: upload(obj, action)))
        self.assert_terminal(signals, False)
        obj.log_sync_action.assert_not_called()

    def test_fixed_upload_parent_lookup_exception_has_one_terminal_event(self):
        obj, action, signals, upload, _, run = self.setup_case('maintained')
        obj.get_parent_id.side_effect = ValueError('missing parent')
        run(SimpleNamespace(func=lambda: upload(obj, action)))
        self.assert_terminal(signals, False)
        obj.drive_client.upload_file.assert_not_called()

    def test_fixed_upload_success_and_revision_preserve_call_contract(self):
        for revision in (False, True):
            with self.subTest(revision=revision):
                obj, action, signals, upload, _, _ = self.setup_case('maintained', ('new', 'digest'))
                upload(obj, action, revision=revision)
                self.assert_terminal(signals, True)
                if revision:
                    obj.drive_client.upload_file.assert_called_once_with(
                        str(Path(obj.working_directory) / 'project' / 'part.sldprt'), 'parent', 7, 'old')
                else:
                    uploaded, parent, idx, existing_id = obj.drive_client.upload_file.call_args.args
                    self.assertEqual(Path(uploaded).name, 'part.sldprt')
                    self.assertNotEqual(uploaded, str(Path(obj.working_directory) / 'project' / 'part.sldprt'))
                    self.assertEqual((parent, idx, existing_id), ('parent', 7, None))
                obj.log_sync_action.assert_called_once_with(action, id='new')
                self.assertEqual(signals.action_complete.calls, [(action[1],)])

    def test_fixed_delete_false_or_none_fails_without_logging(self):
        for result in (False, None):
            with self.subTest(result=result):
                obj, action, signals, _, delete, run = self.setup_case('maintained', result)
                run(SimpleNamespace(func=lambda: delete(obj, action)))
                self.assert_terminal(signals, False)
                obj.log_sync_action.assert_not_called()

    def test_fixed_delete_exception_fails_without_logging(self):
        obj, action, signals, _, delete, run = self.setup_case('maintained', error=RuntimeError('fake failure'))
        run(SimpleNamespace(func=lambda: delete(obj, action)))
        self.assert_terminal(signals, False)
        obj.log_sync_action.assert_not_called()

    def test_fixed_delete_success_preserves_none_completion(self):
        obj, action, signals, _, delete, _ = self.setup_case('maintained', True)
        delete(obj, action)
        self.assert_terminal(signals, True)
        self.assertEqual(signals.action_complete.calls, [(None,)])
        obj.log_sync_action.assert_called_once_with(action, id='old')

    def test_cancelled_actions_have_one_failure_and_do_no_work(self):
        for name in ('upload', 'delete'):
            with self.subTest(method=name):
                obj, action, signals, upload, delete, _ = self.setup_case('maintained', cancelled=True)
                (upload if name == 'upload' else delete)(obj, action)
                self.assert_terminal(signals, False)
                obj.drive_client.upload_file.assert_not_called()
                obj.drive_client.delete_file.assert_not_called()
                obj.log_sync_action.assert_not_called()


if __name__ == '__main__':
    unittest.main()
