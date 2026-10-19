"""Linux path/UI contracts and fail-closed auth, without accounts or network."""
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

if not (Path(__file__).resolve().parents[1] / 'maintained/utils/application_mode.py').is_file():
    raise unittest.SkipTest('Maintained application absent from isolated prototype wheel')

from maintained.utils import application_mode, windows_token_store
from recovered_harness import deny_external_effects, load_function


class LinuxProfileContracts(unittest.TestCase):
    def test_absolute_xdg_profile_is_separate_from_application_directory(self):
        base = Path('/fixture/data').absolute()
        with patch.object(application_mode.sys, 'platform', 'linux'), \
                patch.dict(os.environ, {'XDG_DATA_HOME': str(base)}):
            self.assertEqual(application_mode.default_profile_root(),
                             base / 'cachecad-reconstructed' / 'v1')

    def test_empty_relative_and_missing_xdg_use_home_data_directory(self):
        with patch.object(application_mode.sys, 'platform', 'linux'), \
                patch.object(application_mode.Path, 'home', return_value=Path('/fixture/home')):
            for value in (None, '', 'relative/data', '~/data'):
                with self.subTest(value=value), patch.dict(os.environ, clear=True):
                    if value is not None:
                        os.environ['XDG_DATA_HOME'] = value
                    self.assertEqual(application_mode.default_profile_root(),
                                     Path('/fixture/home/.local/share/cachecad-reconstructed/v1'))

    def test_windows_and_mac_profile_and_session_names_are_preserved(self):
        with patch.object(application_mode.Path, 'home', return_value=Path('/fixture/home')), \
                patch.dict(os.environ, {'LOCALAPPDATA': '/fixture/local', 'XDG_DATA_HOME': '/ignored'}):
            for platform, profile, session in (
                    ('win32', '/fixture/local/CacheCADReconstructed/v1', 'google-session.dpapi'),
                    ('darwin', '/fixture/home/Library/Application Support/CacheCADReconstructed/v1',
                     'google-session.keychain')):
                with self.subTest(platform=platform), patch.object(application_mode.sys, 'platform', platform):
                    self.assertEqual(application_mode.default_profile_root(), Path(profile))
                    config = application_mode.ApplicationConfig(Path(profile), Path('/fixture/client.json'))
                    self.assertEqual(config.session_path, Path(profile) / 'app_data' / session)


class LinuxRuntimeContracts(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())

    def test_linux_update_instructions_never_import_windows_or_network_services(self):
        # load_function rejects all imports, so reaching the Windows path fails.
        method = load_function('maintained/app/app_controller.py', 'AppController.on_download_app_update',
                               {'sys': SimpleNamespace(platform='linux')})
        controller = SimpleNamespace(main_window=SimpleNamespace(show_status_message=Mock()))
        method(controller)
        text = controller.main_window.show_status_message.call_args.args[0]
        self.assertIn('installed manually', text)
        self.assertIn('Quit CacheCAD', text)
        self.assertIn('keep your separate profile', text)
        self.assertFalse(hasattr(controller, '_update_dialog'))

    def test_development_update_action_remains_inert(self):
        method = load_function('maintained/app/app_controller.py', 'AppController.on_download_app_update',
                               {'sys': SimpleNamespace(platform='linux')})
        controller = SimpleNamespace(development=object(), main_window=Mock())
        method(controller)
        controller.main_window.show_status_message.assert_not_called()

    def test_unsupported_secure_store_creates_no_plaintext_or_token_file(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(windows_token_store.sys, 'platform', 'linux'):
            root = Path(temporary)
            config = application_mode.ApplicationConfig(root, root / 'missing-client.json')
            with self.assertRaises(windows_token_store.TokenStoreError) as caught:
                windows_token_store.SecureTokenStore(config.session_path)
            self.assertEqual(caught.exception.code, 'platform')
            self.assertEqual(list(root.iterdir()), [])

    def test_linux_auth_fails_before_constructing_a_google_session(self):
        # Exercise the real worker/factory boundary, with imports represented by
        # inert types. No successful session, OAuth grant or transport is faked.
        session_constructor = Mock(side_effect=AssertionError('Google session must not be constructed'))
        modules = {
            'core.google_session': SimpleNamespace(GoogleSession=session_constructor,
                SessionError=type('SessionError', (Exception,), {}), AuthorizationResult=type('AuthorizationResult', (), {})),
            'core.desktop_signin': SimpleNamespace(DesktopSignInError=type('DesktopSignInError', (Exception,), {})),
            'utils.windows_token_store': windows_token_store,
        }
        method = load_function('maintained/core/auth_manager.py', 'AuthManager._auth_worker',
                               {'SAFE_MESSAGES': {'platform': 'Secure storage is unsupported.'}})
        def imports(name, *args, **kwargs):
            if name not in modules:
                raise AssertionError('Unexpected import: ' + name)
            return modules[name]
        method.__globals__['__builtins__']['__import__'] = imports
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(windows_token_store.sys, 'platform', 'linux'):
            root = Path(temporary)
            for operation in ('authorize', 'restore'):
                with self.subTest(operation=operation):
                    controller = SimpleNamespace(configuration=application_mode.ApplicationConfig(
                        root, root / 'missing-client.json'), _session=None, _closed=False, _completed=Mock())
                    method(controller, 9, operation, threading.Event())
                    controller._completed.emit.assert_called_once_with(9, operation, None, 'platform')
                    self.assertIsNone(controller._session)
            session_constructor.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    def test_owned_ui_discloses_linux_limits_without_changing_supported_platform_text(self):
        widgets = SimpleNamespace(QTextBrowser=type('QTextBrowser', (), {}),
                                  QLabel=type('QLabel', (), {}), QPushButton=type('QPushButton', (), {}))
        for platform, label in (('linux', 'Manual Update Instructions'),
                                ('darwin', 'Mac Update Instructions'), ('win32', 'Check for Updates')):
            with self.subTest(platform=platform):
                method = load_function('maintained/app/app_controller.py', 'AppController._configure_owned_ui',
                    {'sys': SimpleNamespace(platform=platform), 'Page': SimpleNamespace(HOME='home', SETTINGS='settings')})
                def imports(name, *args, **kwargs):
                    if name != 'PySide6.QtWidgets':
                        raise AssertionError('Unexpected import: ' + name)
                    return widgets
                method.__globals__['__builtins__']['__import__'] = imports
                browser = Mock()
                page = SimpleNamespace(latest_version_label=Mock(), update_button=Mock(),
                                       findChildren=lambda kind: [])
                window = SimpleNamespace(setWindowTitle=Mock(), sponsor_label=Mock(), sponsor_logo=Mock(),
                    bug_btn=Mock(), settings_btn=Mock(), pages={
                        'home': SimpleNamespace(findChildren=lambda kind: [browser]), 'settings': page})
                method(SimpleNamespace(main_window=window))
                page.update_button.setText.assert_called_with(label)
                page.update_button.setEnabled.assert_called_once_with(True)
                browser.setOpenExternalLinks.assert_called_once_with(False)
                browser.setOpenLinks.assert_called_once_with(False)
                text = browser.setPlainText.call_args.args[0]
                if platform == 'linux':
                    self.assertIn('Google sign-in and Drive sync are unavailable', text)
                    self.assertIn('No plaintext fallback', text)
                    self.assertNotIn('Sign in with Google to select', text)
                else:
                    self.assertIn('Sign in with Google to select', text)
                    self.assertIn('operating system’s protected credential storage', text)


if __name__ == '__main__':
    unittest.main()
