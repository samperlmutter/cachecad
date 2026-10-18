"""Sign-in launcher guards/entry contract without Qt or live application imports."""
import ast
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import webbrowser

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless((ROOT / 'development/signin.py').is_file(),
                     'Sign-in development sources absent in replacement wheel tests')
class SignInLauncherSafetyTests(unittest.TestCase):
    def setUp(self):
        self.application_modules_before = {name for name in sys.modules if name.startswith(('core.', 'gui.', 'app.'))}
        spec = importlib.util.spec_from_file_location('_cachecad_signin_test', ROOT / 'development/signin.py')
        self.launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.launcher)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.profile = self.root / 'profile'
        self.profile.mkdir()
        self.outside = self.root / 'outside'
        self.outside.mkdir()

    def guards(self):
        hooks = []
        self.enterContext(patch.object(sys, 'meta_path', list(sys.meta_path)))
        self.enterContext(patch.object(os, 'open', os.open))
        self.enterContext(patch.object(sys, 'addaudithook', hooks.append))
        attempts, deny = self.launcher.install_signin_guards(self.profile, ('core.auth_manager', 'core.drive_client'))
        self.assertEqual(len(hooks), 1)
        return hooks[0], attempts, deny

    def test_import_has_no_application_or_oauth_effects(self):
        self.assertEqual({name for name in sys.modules if name.startswith(('core.', 'gui.', 'app.'))},
                         self.application_modules_before)
        self.assertEqual(list(self.profile.iterdir()), [])

    def test_credential_and_project_cli_options_are_not_accepted(self):
        for option in ('--token', '--credentials', '--client-secret', '--project', '--drive-folder'):
            with self.subTest(option=option), patch('sys.stderr'), self.assertRaises(SystemExit):
                self.launcher.parse_arguments(['--app-source', str(self.profile), option, 'fixture'])

    def test_real_guard_preserves_explicit_oauth_transport_and_browser(self):
        before = (socket.socket.connect, socket.socket.bind, socket.create_connection,
                  subprocess.Popen, webbrowser.open)
        self.guards()
        self.assertEqual(before, (socket.socket.connect, socket.socket.bind, socket.create_connection,
                                  subprocess.Popen, webbrowser.open))

    def test_all_profile_writes_and_only_reading_selected_external_config_are_allowed(self):
        hook, attempts, _ = self.guards()
        hook('open', (str(self.profile / 'settings.json'), 'w', os.O_WRONLY | os.O_CREAT))
        hook('open', (str(self.outside / 'own-desktop-client.json'), 'r', os.O_RDONLY))
        hook('os.mkdir', (str(self.profile / 'projects'), 0o700, -1))
        self.assertEqual(attempts, [])

    def test_writes_outside_profile_and_symlinks_are_rejected(self):
        hook, attempts, _ = self.guards()
        for event, args in (
                ('open', (str(self.outside / 'token.json'), 'w', os.O_WRONLY)),
                ('os.mkdir', (str(self.outside / 'projects'), 0o700, -1)),
                ('os.remove', (str(self.outside / 'client.json'), -1)),
                ('os.rename', (str(self.profile / 'settings.json'), str(self.outside / 'saved.json'), -1, -1)),
                ('os.symlink', ('anything', str(self.profile / 'escape'), -1))):
            with self.subTest(event=event), self.assertRaises(RuntimeError):
                hook(event, args)
        self.assertEqual(len(attempts), 5)

    def test_descriptor_relative_open_cannot_write_outside_profile(self):
        if os.open not in os.supports_dir_fd:
            self.skipTest('dir_fd open is unavailable')
        descriptor = os.open(self.outside, os.O_RDONLY)
        try:
            self.guards()
            with self.assertRaises(RuntimeError):
                os.open('token.json', os.O_WRONLY | os.O_CREAT, dir_fd=descriptor)
            self.assertFalse((self.outside / 'token.json').exists())
        finally:
            os.close(descriptor)

    def test_vendor_imports_are_blocked_but_new_identity_helper_is_allowed(self):
        _, attempts, _ = self.guards()
        for name in ('core.auth_manager', 'core.drive_client'):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'Vendor/Drive'):
                sys.meta_path[0].find_spec(name)
        self.assertIsNone(sys.meta_path[0].find_spec('core.desktop_signin'))
        self.assertEqual(len(attempts), 2)

    def test_source_keeps_signin_separate_from_recovered_auth_success(self):
        tree = ast.parse((ROOT / 'development/signin.py').read_text(encoding='utf-8'))
        configure_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                           and isinstance(node.func, ast.Name) and node.func.id == 'configure_offline']
        self.assertEqual(len(configure_calls), 1)
        self.assertEqual(len(configure_calls[0].args), 1)
        self.assertEqual(configure_calls[0].keywords, [])
        prohibited = {'auth_success', 'auth_started', 'user_profile_loaded'}
        self.assertFalse(any(isinstance(node, ast.Attribute) and node.attr in prohibited
                             for node in ast.walk(tree)))
        self.assertFalse(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                             and node.func.id in ('DriveClient', 'AuthManager') for node in ast.walk(tree)))

    def test_dialog_never_serializes_or_calls_recovered_auth_signals(self):
        tree = ast.parse((ROOT / 'maintained/gui/desktop_signin_dialog.py').read_text(encoding='utf-8'))
        prohibited = {'auth_success', 'user_profile_loaded', 'dump', 'dumps', 'write_text', 'write_bytes'}
        self.assertFalse(any(isinstance(node, ast.Attribute) and node.attr in prohibited
                             for node in ast.walk(tree)))


if __name__ == '__main__':
    unittest.main()
