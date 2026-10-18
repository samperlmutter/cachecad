"""Real offscreen Qt selection/dialog/controller checks, with test-only I/O doubles.

The opt-in child imports the actual original selection page and maintained dialog,
then runs unchanged maintained controller method bodies. No account, service,
transfer, credentials or real project is used. Runtime is the reviewed Qt lock.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = os.environ.get('CACHECAD_RUNTIME_PYTHON')


@unittest.skipUnless((ROOT / 'maintained/app/app_controller.py').is_file(),
                     'Actual maintained application source absent')
class ControllerRetirementTests(unittest.TestCase):
    def setUp(self):
        from recovered_harness import deny_external_effects
        self.enterContext(deny_external_effects())

    def test_retiring_clients_closes_discovery_and_disconnects_old_signals(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from recovered_harness import load_function
        signals = SimpleNamespace(token_refreshed=Mock(), sync_execution_finished=Mock(), sync_cancelled=Mock())
        client, analyzer, database = Mock(), Mock(), object()
        controller = SimpleNamespace(sync_analyzer=analyzer, drive_client=client,
                                     db_manager=database, _retired_databases=[], signed_in=True)
        method = load_function('maintained/app/app_controller.py', 'AppController._retire_sync_clients',
                               {'signal_manager': signals})
        method(controller)
        client.close.assert_called_once_with()
        analyzer.cancel_sync_analysis.assert_called_once_with()
        signals.token_refreshed.disconnect.assert_called_once_with(client.on_token_refresh)
        self.assertFalse(hasattr(controller, 'drive_client'))
        self.assertEqual(controller._retired_databases, [database])
        self.assertFalse(controller.signed_in)

    def test_shutdown_closes_production_client_but_preserves_offline_fixture_contract(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from recovered_harness import load_function
        for development in (None, object()):
            with self.subTest(offline=development is not None):
                client = Mock() if development is None else SimpleNamespace()
                manager = SimpleNamespace(shutdown=Mock(return_value=True))
                controller = SimpleNamespace(development=development, sync_executing=False,
                    sync_analyzer=Mock(), drive_client=client, auth_manager=manager, signed_in=True)
                method = load_function('maintained/app/app_controller.py', 'AppController.shutdown', {})
                self.assertTrue(method(controller))
                if development is None:
                    client.close.assert_called_once_with()
                self.assertTrue(controller._shutting_down)
                self.assertFalse(controller.signed_in)

    def test_stale_nested_service_construction_closes_the_unadopted_client(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from recovered_harness import denied, load_function
        credentials = object()
        manager = SimpleNamespace(get_creds=Mock(side_effect=[credentials, object()]), identity=object())
        client = SimpleNamespace(service=object(), close=Mock())
        controller = SimpleNamespace(development=None, _shutting_down=False, auth_manager=manager,
                                     _retire_sync_clients=Mock())
        method = load_function('maintained/app/app_controller.py', 'AppController.on_auth_success', {})
        def test_import(name, *args, **kwargs):
            if name != 'core.drive_client':
                return denied()
            return SimpleNamespace(DriveClient=lambda creds: client)
        method.__globals__['__builtins__']['__import__'] = test_import
        method(controller, credentials)
        client.close.assert_called_once_with()
        self.assertFalse(hasattr(controller, 'drive_client'))


@unittest.skipUnless(RUNTIME, 'Set CACHECAD_RUNTIME_PYTHON for real destructive-confirmation Qt tests')
@unittest.skipUnless((ROOT / 'maintained/gui/destructive_confirmation.py').is_file(),
                     'Actual maintained application source absent')
class DestructiveConfirmationRuntimeTests(unittest.TestCase):
    def test_actual_selection_dialog_and_controller_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix='cachecad-confirmation-test-') as folder:
            environment = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1')
            environment.pop('PYTHONPATH', None)
            environment.pop('PYTHONHOME', None)
            run = subprocess.run([RUNTIME, '-B', str(Path(__file__).resolve()), '--run-ui', folder],
                                 cwd=folder, env=environment, capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stdout + '\n' + run.stderr)
            self.assertIn('DESTRUCTIVE_CONFIRMATION_QT_OK', run.stdout)
            self.assertEqual(list(Path(folder).iterdir()), [], 'UI test created unexpected files')


def run_ui(folder):
    import importlib.util
    from types import MethodType, ModuleType, SimpleNamespace
    from unittest.mock import Mock
    sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'recovered')]
    from recovered_harness import deny_external_effects, load_function
    # This test-only boundary avoids original utils.paths' top-level profile mkdir.
    paths = ModuleType('utils.paths')
    paths.RESOURCES_DIR = Path(folder)
    paths.CaseInsensitivePathDict = dict
    sys.modules['utils.paths'] = paths
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer, Signal
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QStackedWidget, QWidget
    from core.models import SyncAction, SyncActionType
    from core.signal_manager import signal_manager
    from gui.page import Page
    from gui.pages.selection_page import SelectionPage
    from maintained.core.sync_consent import StaleSyncPlan
    spec = importlib.util.spec_from_file_location('confirmation_under_test',
        ROOT / 'maintained/gui/destructive_confirmation.py')
    ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui)
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)

    class ControllerFixture(SimpleNamespace):
        pass

    class ExecutorFixture(QObject):
        guard_failed = Signal(str)

    class UiTests(unittest.TestCase):
        def setUp(self):
            self.enterContext(deny_external_effects())
            self.executor = Mock(return_value=ExecutorFixture())
            self.approve = Mock(return_value=object())
            self.window = QMainWindow()
            status = load_function('recovered/gui/main_window.py', 'MainWindow.show_status_message', {})
            self.window.show_status_message = Mock(wraps=MethodType(status, self.window))
            self.window.main_stack = QStackedWidget(self.window)
            self.window.setCentralWidget(self.window.main_stack)
            self.controller = ControllerFixture(development=None, signed_in=True,
                analysis_ready=True, sync_executing=False, buttons_disabled=False,
                _shutting_down=False, _executor_unsettled=False,
                project_name='fixture-project', project_id='fixture-folder-id',
                project_read_only=False, working_directory=Path(folder),
                drive_client=object(), local_client=object(), path_dict={},
                sync_analyzer=SimpleNamespace(_generation=7, _cancelled=False,
                                              _settled=True, observed_snapshot=object()),
                main_window=self.window, upload_actions=[], download_actions=[])
            namespace = dict(Page=Page, signal_manager=signal_manager, SyncExecutor=self.executor,
                approve_sync_actions=self.approve, confirm_destructive_actions=ui.confirm_destructive_actions,
                selection_identity=ui.selection_identity, CaseInsensitivePathDict=dict,
                StaleSyncPlan=StaleSyncPlan, Qt=Qt)
            names = ('on_execute_sync', '_sync_confirmation_context', '_reset_sync_selection',
                     'enable_buttons', 'disable_buttons', '_clear_sync_plan', 'on_sync_guard_failed')
            namespace['logging'] = Mock()
            for name in names:
                method = load_function('maintained/app/app_controller.py', 'AppController.' + name, namespace)
                setattr(self.controller, name, MethodType(method, self.controller))
            self.page = SelectionPage(self.controller)
            self.window.pages = {Page.SELECTION: self.page}
            self.window.main_stack.addWidget(self.page)
            signal_manager.execute_sync.connect(self.controller.on_execute_sync)
            self.window.show()
            application.processEvents()
            self.callback_errors = []

        def tearDown(self):
            signal_manager.execute_sync.disconnect(self.controller.on_execute_sync)
            for dialog in application.topLevelWidgets():
                if isinstance(dialog, QDialog):
                    dialog.reject()
            self.window.close()
            self.window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            application.processEvents()

        def select(self, kind='DOWNLOAD_UNKNOWN', path='parts/assembly.SLDASM'):
            action = SyncAction(path=path, action=getattr(SyncActionType, kind), reason='fixture',
                                size=3, md5_checksum='0' * 32, parent_id='folder',
                                modified_by='fixture', modified_time='NA', drive_id='remote-file')
            self.controller.download_actions = [action]
            self.page.selection_tree.update_model([action])
            self.page.selection_tree.model.item(0, 0).setCheckState(Qt.Checked)
            return action

        def during_dialog(self, callback):
            def invoke():
                try:
                    dialogs = [widget for widget in application.topLevelWidgets()
                               if isinstance(widget, ui.DestructiveSyncDialog) and widget.isVisible()]
                    self.assertEqual(len(dialogs), 1)
                    callback(dialogs[0])
                except BaseException as error:
                    self.callback_errors.append(error)
                    for widget in application.topLevelWidgets():
                        if isinstance(widget, QDialog):
                            widget.reject()
            QTimer.singleShot(0, invoke)

        def click(self):
            self.page.execute_sync_button.click()
            if self.callback_errors:
                raise self.callback_errors[0]

        def assert_cancelled(self):
            self.executor.assert_not_called()
            self.approve.assert_not_called()
            self.assertFalse(self.controller.sync_executing)
            self.assertFalse(self.controller._executor_unsettled)
            self.assertFalse(self.controller._sync_confirmation_pending)
            self.assertFalse(self.page.sync_started)
            self.assertFalse(hasattr(self.page, 'progress_bar'))
            self.assertEqual(self.page.back_button.text(), 'Back')

        def test_cancel_escape_close_and_default_enter_never_execute(self):
            for dismissal in ('cancel', 'escape', 'close', 'enter'):
                with self.subTest(dismissal=dismissal):
                    self.select()
                    def dismiss(dialog):
                        self.assertTrue(dialog.cancel_button.isDefault())
                        self.assertFalse(dialog.destructive_button.isDefault())
                        self.assertFalse(dialog.destructive_button.autoDefault())
                        if dismissal == 'cancel':
                            dialog.cancel_button.click()
                        elif dismissal == 'close':
                            dialog.close()
                        else:
                            QTest.keyClick(dialog, Qt.Key_Escape if dismissal == 'escape' else Qt.Key_Return)
                    self.during_dialog(dismiss)
                    self.click()
                    self.assert_cancelled()
                    self.assertTrue(self.controller.analysis_ready)
                    self.assertTrue(self.page.execute_sync_button.isEnabled())

        def test_all_six_destructive_directions_and_exact_plain_text_paths(self):
            for kind in sorted(ui.DESTRUCTIVE_ACTIONS):
                with self.subTest(kind=kind):
                    path = 'parts/<literal>&\u88c5\u914d.SLDPRT'
                    self.select(kind.upper(), path)
                    def inspect(dialog):
                        text = dialog.details.toPlainText()
                        self.assertIn(ui.ACTION_DIRECTIONS[kind], text)
                        self.assertIn('Project-relative path: ' + ui.display_value(path), text)
                        self.assertIn(ui.display_value(Path(folder) / 'fixture-project' / path), text)
                        self.assertIn('REMOTE file ID: "remote-file"', text)
                        self.assertIn('overwrite or delete existing files', dialog.warning.text())
                        self.assertGreater(dialog.grab().width(), 300)
                        dialog.cancel_button.click()
                    self.during_dialog(inspect)
                    self.click()
                    self.assert_cancelled()

        def test_untrusted_names_cannot_spoof_warning_lines_or_direction(self):
            path = 'part\nDELETE LOCAL\u202e.prt\u2066\x1b\u2028.SLDASM'
            self.select(path=path)
            def inspect(dialog):
                text = dialog.details.toPlainText()
                self.assertIn(ui.display_value(path), text)
                self.assertNotIn('\nDELETE LOCAL', text)
                for char in ('\u202e', '\u2066', '\x1b', '\u2028'):
                    self.assertNotIn(char, text)
                self.assertIn('\\u202e', text)
                self.assertIn('\\nDELETE LOCAL', text)
                dialog.cancel_button.click()
            self.during_dialog(inspect)
            self.click()
            self.assert_cancelled()

        def test_explicit_button_accepts_exact_selection_once_even_on_repeated_clicks(self):
            action = self.select()
            expected = self.page.selection_tree.get_selected_actions()
            def accept(dialog):
                self.controller.on_execute_sync(expected)
                self.page.execute_sync_button.click()
                self.assertEqual(len([w for w in application.topLevelWidgets()
                    if isinstance(w, ui.DestructiveSyncDialog) and w.isVisible()]), 1)
                dialog.destructive_button.click()
                dialog.destructive_button.click()
            self.during_dialog(accept)
            self.click()
            self.executor.assert_called_once()
            self.approve.assert_called_once_with(tuple(expected), self.controller.sync_analyzer.observed_snapshot)
            self.assertEqual(self.executor.call_args.args[5], tuple(expected))
            self.assertIs(self.executor.call_args.args[5][0][1], action)
            self.assertIs(self.executor.call_args.kwargs['consent'], self.approve.return_value)
            self.assertTrue(self.controller.sync_executing)
            self.assertFalse(self.page.execute_sync_button.isEnabled())
            self.controller.on_execute_sync(expected)
            self.executor.assert_called_once()

        def test_generic_dialog_accept_is_not_destructive_consent(self):
            self.select()
            self.during_dialog(lambda dialog: dialog.accept())
            self.click()
            self.assert_cancelled()

        def test_nested_loop_stale_context_and_selection_never_execute(self):
            mutations = {
                'path': lambda c, a: setattr(a, 'path', 'other.SLDPRT'),
                'direction': lambda c, a: setattr(a, 'action', SyncActionType.UPLOAD_UNKNOWN),
                'remote_id': lambda c, a: setattr(a, 'drive_id', 'other-id'),
                'generation': lambda c, a: setattr(c.sync_analyzer, '_generation', 8),
                'analyzer': lambda c, a: setattr(c, 'sync_analyzer', SimpleNamespace(
                    _generation=7, _cancelled=False, _settled=True, observed_snapshot=object())),
                'snapshot': lambda c, a: setattr(c.sync_analyzer, 'observed_snapshot', object()),
                'cancelled_scan': lambda c, a: setattr(c.sync_analyzer, '_cancelled', True),
                'new_scan': lambda c, a: setattr(c.sync_analyzer, '_settled', False),
                'project': lambda c, a: setattr(c, 'project_id', 'different-project'),
                'directory': lambda c, a: setattr(c, 'working_directory', Path(folder) / 'other'),
                'read_only': lambda c, a: setattr(c, 'project_read_only', True),
                'client': lambda c, a: setattr(c, 'drive_client', object()),
                'folder_map': lambda c, a: c.path_dict.update(folder='other-id'),
                'plan': lambda c, a: setattr(c, 'download_actions', []),
                'signed_out': lambda c, a: setattr(c, 'signed_in', False),
                'shutdown': lambda c, a: setattr(c, '_shutting_down', True),
                'selection': lambda c, a: self.page.selection_tree.model.item(0, 0).setCheckState(Qt.Unchecked),
                'replacement_row': lambda c, a: self.select(),
                'navigation': lambda c, a: self.window.main_stack.setCurrentIndex(
                    self.window.main_stack.addWidget(QWidget())),
            }
            original = dict(vars(self.controller))
            for name, mutate in mutations.items():
                with self.subTest(mutation=name):
                    self.controller.__dict__.update(original)
                    self.controller.path_dict = {}
                    self.controller.sync_analyzer = SimpleNamespace(_generation=7, _cancelled=False,
                        _settled=True, observed_snapshot=object())
                    self.window.main_stack.setCurrentWidget(self.page)
                    action = self.select()
                    def change(dialog):
                        mutate(self.controller, action)
                        dialog.destructive_button.click()
                    self.during_dialog(change)
                    self.click()
                    self.assert_cancelled()
                    self.window.show_status_message.assert_called()

        def test_empty_selection_resets_the_original_page(self):
            self.select()
            self.page.selection_tree.model.item(0, 0).setCheckState(Qt.Unchecked)
            self.click()
            self.assert_cancelled()

        def test_new_file_skips_destructive_dialog_but_gets_bound_consent(self):
            self.select('DOWNLOAD_NEW')
            self.click()
            self.executor.assert_called_once()
            self.approve.assert_called_once()
            self.assertFalse(any(isinstance(w, ui.DestructiveSyncDialog) and w.isVisible()
                                 for w in application.topLevelWidgets()))

        def test_read_only_project_rejects_remote_changes(self):
            self.select('UPLOAD_UNKNOWN')
            self.controller.project_read_only = 'True'
            self.click()
            self.assert_cancelled()
            self.assertIn('read-only', self.window.show_status_message.call_args.args[0])

        def test_missing_scan_snapshot_does_not_ask_for_or_run_sync(self):
            self.select()
            self.controller.sync_analyzer.observed_snapshot = None
            self.click()
            self.assert_cancelled()
            self.assertIn('Refresh', self.window.show_status_message.call_args.args[0])

        def test_executor_rejection_clears_plan_and_restores_controls(self):
            self.select()
            self.executor.side_effect = ValueError('access_token=FIXTURE-PRIVATE-ERROR')
            self.during_dialog(lambda dialog: dialog.destructive_button.click())
            self.click()
            self.assertFalse(self.controller.sync_executing)
            self.assertFalse(self.controller._executor_unsettled)
            self.assertFalse(self.controller.analysis_ready)
            self.assertFalse(hasattr(self.page, 'progress_bar'))
            self.assertTrue(self.page.execute_sync_button.isEnabled())
            self.assertEqual(self.page.back_button.text(), 'Back')
            self.assertNotIn('FIXTURE-PRIVATE-ERROR', self.window.show_status_message.call_args.args[0])

        def test_guard_failure_is_queued_to_actual_visible_status_bar(self):
            self.select()
            self.during_dialog(lambda dialog: dialog.destructive_button.click())
            self.click()
            self.executor.return_value.guard_failed.emit('stale')
            self.assertEqual(self.window.statusBar().currentMessage(), '')
            application.processEvents()
            message = self.window.statusBar().currentMessage()
            self.assertIn('changed after the scan', message)
            self.assertIn('Refresh', message)
            self.assertIn('confirm again', message)
            self.executor.return_value.guard_failed.emit('download_failed')
            application.processEvents()
            self.assertIn('Trash/Recycle Bin', self.window.statusBar().currentMessage())
            self.executor.return_value.guard_failed.emit('access_token=FIXTURE-PRIVATE-ERROR')
            application.processEvents()
            self.assertNotIn('FIXTURE-PRIVATE-ERROR', self.window.statusBar().currentMessage())

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(UiTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print('DESTRUCTIVE_CONFIRMATION_QT_OK')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--run-ui':
        run_ui(sys.argv[2])
    else:
        unittest.main()
