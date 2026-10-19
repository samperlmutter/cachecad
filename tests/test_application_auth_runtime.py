"""Opt-in real Qt application/auth integration; transports are mocked here only.

No live account, permission grant, network request or user CAD file is used.
CACHECAD_RUNTIME_PYTHON selects the reviewed hash-locked runtime.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = os.environ.get('CACHECAD_RUNTIME_PYTHON')


@unittest.skipUnless(RUNTIME, 'Set CACHECAD_RUNTIME_PYTHON for actual Qt integration')
class ApplicationAuthRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tooling.assemble_app import assemble
        cls.temporary = tempfile.TemporaryDirectory(prefix='cachecad-application-tests-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.source = cls.directory / 'app-source'
        assemble(ROOT, cls.source, production=True)
        cls.environment = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1')
        cls.environment.pop('PYTHONPATH', None)
        cls.environment.pop('PYTHONHOME', None)

    def test_real_signed_out_startup_missing_owner_configuration(self):
        run = subprocess.run([RUNTIME, '-B', str(self.source / 'application/launch.py'),
            '--app-source', str(self.source), '--startup-check', '--platform', 'offscreen',
            '--profile-dir', str(self.directory / 'startup-profile'),
            '--client-config', str(self.directory / 'absent-owner-config.json')],
            env=self.environment, cwd=self.directory, text=True, capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        result = json.loads(next(line.split('=', 1)[1] for line in run.stdout.splitlines()
                                 if line.startswith('CACHECAD_APPLICATION_RESULT=')))
        self.assertEqual(result['actual_gui'], 'gui.main_window.MainWindow')
        self.assertEqual(result['mode'], 'application')
        for key in ('signed_in', 'has_credentials', 'drive_client_created'):
            self.assertFalse(result[key], key)
        for key in ('setup_required', 'network_disabled', 'sdk_imports_verified', 'certificate_bundle_present'):
            self.assertTrue(result[key], key)
        for key in ('errors', 'credential_files', 'external_attempts', 'development_modules_loaded', 'forbidden_modules_loaded'):
            self.assertEqual(result[key], [], key)
        self.assertEqual(result['active_threads'], 0)
        self.assertGreater(result['rendered_size'][0], 100)

    def test_real_qt_account_lifecycle_and_controller_wiring(self):
        run = subprocess.run([RUNTIME, '-B', str(Path(__file__).resolve()),
            '--run-lifecycle', str(self.source)], env=self.environment, cwd=self.directory,
            text=True, capture_output=True, timeout=45)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('APPLICATION_AUTH_LIFECYCLE_OK', run.stdout)
        if sys.platform == 'linux':
            self.assertIn('APPLICATION_LINUX_STORAGE_GUARD_OK', run.stdout)


def run_lifecycle(source):
    """Execute actual app functions/Qt delivery with test-only remote transport."""
    import importlib.util
    import threading
    import time
    from datetime import datetime, timedelta
    from unittest.mock import Mock, patch
    spec = importlib.util.spec_from_file_location('_application_test_bootstrap', source / 'application/launch.py')
    launch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launch)
    launch.install_source_importer(source, launch.verify_source(source))
    with tempfile.TemporaryDirectory(prefix='cachecad-auth-profile-') as temporary:
        profile = launch.prepare_profile(Path(temporary))
        os.chdir(profile)
        from utils.application_mode import configure_application
        config_path = profile / 'owner-client.json'
        # Actual owner config parsing/session transports have separate security tests.
        config_path.write_text('{}')
        configure_application(profile, config_path, startup_check=True)
        attempts = launch.install_startup_check_guards()
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(lambda *args: None)
        from google.oauth2.credentials import Credentials
        from core.desktop_signin import AuthorizationResult, SignInIdentity
        from core.google_session import SessionError
        from core.signal_manager import signal_manager
        import resources_rc
        from app.app_controller import AppController
        from gui.main_window import MainWindow
        from core.thread_manager import thread_pool, network_pool
        import core.drive_client as drive_module
        app = QApplication(['cachecad-auth-test'])
        slot_errors = []
        def report_exception(kind, value, tb):
            slot_errors.append((kind.__name__, str(value)))
            sys.__excepthook__(kind, value, tb)
        sys.excepthook = report_exception
        controller = AppController()
        manager = controller.auth_manager
        window = MainWindow(controller)
        controller.set_main_window(window)
        window.show()
        assert not controller.signed_in and not hasattr(controller, 'drive_client')

        class SessionTransport:
            def __init__(self):
                self.credentials = self.identity = None
                self.on_invalidation = None
                self.authorizations = self.signouts = self.revocations = self.restores = 0
                self.entered = threading.Event()
                self.release = threading.Event()
                self.behavior = 'success'
                self.clear_fails = False

            def issue(self):
                self.credentials = Credentials(token='synthetic-access', refresh_token='synthetic-refresh',
                    token_uri='https://oauth2.googleapis.com/token', client_id='synthetic', client_secret='synthetic',
                    expiry=datetime.utcnow() + timedelta(hours=1))
                self.identity = SignInIdentity('synthetic@example.invalid', 'synthetic-subject')
                return AuthorizationResult(self.identity, self.credentials)

            def restore(self):
                self.restores += 1
                return None

            def authorize(self, cancel_event=None, timeout=120):
                self.authorizations += 1
                self.entered.set()
                if self.behavior == 'pending':
                    self.release.wait(3)
                    if cancel_event.is_set():
                        raise SessionError('cancelled')
                if self.behavior == 'error':
                    raise RuntimeError('sensitive-provider-text-must-not-reach-ui')
                return self.issue()

            def cancel(self):
                self.release.set()

            def sign_out(self):
                self.signouts += 1
                self.credentials = self.identity = None
                if self.clear_fails:
                    raise SessionError('storage')

            def disconnect(self, cancel_event=None):
                self.revocations += 1
                self.sign_out()

            def close(self):
                self.cancel()
                self.credentials = self.identity = None

        session = SessionTransport()
        build = Mock(return_value=Mock())
        profile_fetch = Mock(side_effect=AssertionError('Arbitrary profile fetch forbidden'))

        def pump(predicate, timeout=5):
            deadline = time.monotonic() + timeout
            while not predicate() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.005)
            app.processEvents()
            assert predicate(), ('Qt result timeout', manager.state, slot_errors)
            assert not slot_errors, slot_errors

        if sys.platform == 'linux':
            # Before injecting the pre-existing lifecycle transport below,
            # exercise the actual unsupported platform path with real Qt signal
            # delivery. Browser, network and process tripwires remain active.
            from core.auth_manager import SAFE_MESSAGES
            before = {path.relative_to(profile) for path in profile.rglob('*')}
            for operation in (manager.authorize, manager.restore, manager.authorize):
                assert operation()
                pump(lambda: not manager._auth_in_progress)
                assert manager.state == 'error' and manager.message == SAFE_MESSAGES['platform']
                assert manager._session is None and manager.get_creds() is None
                assert not controller.signed_in and not hasattr(controller, 'drive_client')
                assert window.signin_button.status_label.text() == SAFE_MESSAGES['platform']
                assert not attempts, attempts
            assert {path.relative_to(profile) for path in profile.rglob('*')} == before
            print('APPLICATION_LINUX_STORAGE_GUARD_OK')

        manager._session = session
        with patch.object(drive_module, 'build', build), patch.object(drive_module.DriveClient, 'get_user_profile', profile_fetch):
            # No automatic browser or fake authenticated state from an empty cache.
            assert manager.restore()
            pump(lambda: not manager._auth_in_progress)
            assert session.restores == 1 and session.authorizations == 0
            assert manager.state == 'signed_out' and not controller.signed_in
            build.assert_not_called()

            session.behavior = 'pending'
            assert manager.authorize()
            assert session.entered.wait(1)
            old_generation = manager._generation
            assert not manager.authorize()
            assert manager.cancel()
            pump(lambda: not manager._auth_in_progress)
            assert manager.state == 'error' and manager.creds is None
            build.assert_not_called()

            session.behavior = 'error'
            assert manager.authorize()
            pump(lambda: not manager._auth_in_progress)
            assert manager.state == 'error'
            assert 'sensitive-provider' not in manager.message
            assert window.signin_button.button.isEnabled()

            session.behavior = 'success'
            assert manager.authorize()
            pump(lambda: controller.signed_in)
            assert manager.identity.email == 'synthetic@example.invalid'
            assert controller.drive_client.creds is manager.creds is session.credentials
            assert type(controller.local_client).__name__ == 'LocalClient'
            assert type(controller.sync_analyzer).__name__ == 'SyncAnalyzer'
            assert type(controller.db_manager).__name__ == 'DBManager'
            assert controller.user == {'email': 'synthetic@example.invalid', 'sub': 'synthetic-subject'}
            profile_fetch.assert_not_called()
            assert build.call_args.kwargs['http'].credentials is manager.creds
            assert build.call_args.kwargs['http'].http.timeout == 10
            current_credentials = manager.creds
            manager._receive(manager._generation, 'authorize', None, 'failed')
            assert manager.creds is current_credentials and manager.state == 'signed_in'
            manager._receive(old_generation, 'authorize', session.issue(), '')
            assert manager.creds is current_credentials
            # The fake session was manually changed above; restore its expected identity.
            session.credentials = current_credentials

            # Local sign-out never revokes. A failed clear visibly permits retry.
            session.clear_fails = True
            controller.on_local_signout()
            pump(lambda: not manager._auth_in_progress)
            assert manager.state == 'cleanup_required' and not controller.signed_in
            assert manager.creds is None and session.revocations == 0
            session.clear_fails = False
            controller.on_signin_clicked()  # The UI button now retries local cache deletion.
            pump(lambda: not manager._auth_in_progress)
            assert manager.state == 'signed_out' and session.signouts == 2

            # Commit wins if Cancel arrives after the backend committed but before Qt delivery.
            assert manager.authorize()
            manager._thread.join(2)
            assert not manager._thread.is_alive()
            assert manager.cancel()
            pump(lambda: controller.signed_in)
            assert 'completed before cancellation' in manager.message

            with patch('gui.application_account.confirm_disconnect', return_value=False):
                controller.on_disconnect_clicked()
                assert session.revocations == 0
            with patch('gui.application_account.confirm_disconnect', return_value=True):
                controller.on_disconnect_clicked()
                pump(lambda: not manager._auth_in_progress)
            assert session.revocations == 1 and manager.state == 'signed_out'

            assert manager.authorize()
            pump(lambda: controller.signed_in)
            # Queued invalidation is generation-scoped; old account notifications cannot win.
            manager._on_invalidated(manager._generation - 1, 'reauth_required')
            assert controller.signed_in
            manager._on_invalidated(manager._generation, 'reauth_required')
            assert not controller.signed_in and not controller.analysis_ready
            assert manager.state == 'error'

            assert manager.authorize()
            pump(lambda: controller.signed_in)
            # Interrupted transfer uses a conservative restart boundary for legacy global signals.
            controller.sync_executing = True
            manager._on_invalidated(manager._generation, 'reauth_required')
            assert manager.state == 'restart_required' and not manager.authorize()
            assert not window.signin_button.button.isEnabled()
            assert not controller.signed_in and controller.download_actions == []

        assert controller.shutdown()
        assert thread_pool.waitForDone(3000) and network_pool.waitForDone(3000)
        app.processEvents()
        assert manager.creds is None
        assert not attempts, attempts
        assert not slot_errors, slot_errors
        for database in controller._retired_databases:
            database.conn.close()
        window.tray_icon.hide()
        window.deleteLater()
        app.processEvents()
        os.chdir(source.parent)
        print('APPLICATION_AUTH_LIFECYCLE_OK')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--run-lifecycle':
        run_lifecycle(Path(sys.argv[2]))
    else:
        unittest.main()
