"""Opt-in actual Qt identity-only GUI tests; all authentication is synthetic.

Set CACHECAD_RUNTIME_PYTHON to the reviewed hash-locked Python runtime. Each
scenario runs separately with network/browser/process denied and disposable data.
No own OAuth client, account, browser, listener, token, or real grant is used.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

from tooling.assemble_app import assemble

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = os.environ.get('CACHECAD_RUNTIME_PYTHON')


@unittest.skipUnless(RUNTIME, 'Set CACHECAD_RUNTIME_PYTHON for opt-in actual Qt sign-in tests')
@unittest.skipUnless((ROOT / 'development/signin.py').is_file(),
                     'Sign-in source absent in replacement wheel tests')
class SignInGuiRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.python = Path(RUNTIME).absolute()
        if not cls.python.is_file() or not os.access(cls.python, os.X_OK):
            raise ValueError('CACHECAD_RUNTIME_PYTHON must name an executable reviewed Python binary')
        cls.temporary = tempfile.TemporaryDirectory(prefix='cachecad-signin-gui-tests-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.application = cls.root / 'application'
        assemble(ROOT, cls.application)
        cls.scratch = cls.root / 'profiles'
        cls.scratch.mkdir()
        cls.environment = dict(os.environ)
        cls.environment.pop('PYTHONPATH', None)
        cls.environment.pop('PYTHONHOME', None)
        cls.environment.update(PYTHONDONTWRITEBYTECODE='1', QT_QPA_PLATFORM='offscreen',
                               TMPDIR=str(cls.scratch), TMP=str(cls.scratch), TEMP=str(cls.scratch))

    def snapshot(self):
        return {p.relative_to(self.application).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.application.rglob('*') if p.is_file()}

    def check_result(self, command, *, synthetic=True):
        before = self.snapshot()
        completed = subprocess.run(command, cwd=self.root, env=self.environment,
                                   capture_output=True, text=True, timeout=40)
        diagnostic = completed.stdout + '\n' + completed.stderr
        self.assertEqual(completed.returncode, 0, diagnostic)
        lines = [line.removeprefix('CACHECAD_SIGNIN_RESULT=') for line in completed.stdout.splitlines()
                 if line.startswith('CACHECAD_SIGNIN_RESULT=')]
        self.assertEqual(len(lines), 1, diagnostic)
        result = json.loads(lines[0])
        self.assertEqual(result['mode'], 'signin-only')
        self.assertEqual(result['actual_gui'], 'gui.main_window.MainWindow')
        self.assertEqual(result['actual_dialog'], 'gui.desktop_signin_dialog.DesktopSignInDialog')
        self.assertEqual(result['qt_platform'], 'offscreen')
        self.assertEqual(result['source_loader'], 'verified-source')
        self.assertIs(result['synthetic_auth'], synthetic)
        for field in ('errors', 'external_attempts', 'forbidden_modules_loaded', 'credential_files',
                      'slot_errors', 'missing_placeholder_resources', 'project_files',
                      'non_signin_buttons_enabled'):
            self.assertEqual(result[field], [], (field, diagnostic))
        for field in ('controller_signed_in', 'controller_has_credentials', 'drive_client_created',
                      'transfer_button_enabled'):
            self.assertIs(result[field], False, (field, diagnostic))
        self.assertEqual(result['active_threads'], 0)
        self.assertTrue(all(dimension > 100 for dimension in result['rendered_size']))
        if synthetic:
            for field in ('network_disabled', 'fixture_lifecycle_passed', 'repeated_click_ignored',
                          'stale_completion_ignored', 'signout_cleared_identity', 'cancelled_identity_hidden',
                          'sdk_imports_verified', 'certificate_bundle_present', 'closed_dialog_identity_cleared'):
                self.assertIs(result[field], True, (field, diagnostic))
        self.assertEqual(before, self.snapshot(), 'Source/configuration changed during launch')
        self.assertEqual(list(self.scratch.iterdir()), [], 'Disposable profile not cleaned up')
        self.assertNotIn('fixture@example.invalid', diagnostic, 'Identity leaked to machine log')
        self.assertNotIn('stale@example.invalid', diagnostic, 'Stale identity leaked to machine log')
        return result

    def run_scenario(self, scenario):
        return self.check_result([str(self.python), '-B', str(self.application / 'development/signin.py'),
                                 '--app-source', str(self.application), '--smoke', '--scenario', scenario])

    def test_complete_login_denial_timeout_cancel_close_retry_and_signout_lifecycle(self):
        result = self.run_scenario('lifecycle')
        self.assertEqual(result['outcomes'], ['success', 'denied', 'timeout', 'cancelled', 'stale-close', 'success'])

    def test_success_signout_and_relogin(self):
        self.assertEqual(self.run_scenario('success')['outcomes'], ['success', 'success'])

    def test_consent_denied_then_successful_retry(self):
        self.assertEqual(self.run_scenario('failure')['outcomes'], ['denied', 'success'])

    def test_timeout_then_successful_retry(self):
        self.assertEqual(self.run_scenario('timeout')['outcomes'], ['timeout', 'success'])

    def test_cancel_discards_completion_then_retry(self):
        self.assertEqual(self.run_scenario('cancel-retry')['outcomes'], ['cancelled', 'success'])

    def test_close_discards_pending_identity_then_reopen(self):
        self.assertEqual(self.run_scenario('stale-close')['outcomes'], ['stale-close', 'success'])

    def test_real_default_path_starts_signed_out_without_opening_browser_or_network(self):
        # Test code adds offline tripwires to real launch without selecting its
        # --smoke branch. Opening/closing the actual dialog must remain inert.
        script = textwrap.dedent('''\
            import importlib.util
            from pathlib import Path
            import sys
            root = Path(sys.argv[1])
            spec = importlib.util.spec_from_file_location('signin_under_test', root / 'development/signin.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            original_guards = module.install_signin_guards
            real_attempts = []
            inspected = []
            def guards(profile, forbidden):
                attempts, deny = original_guards(profile, forbidden)
                fixture_attempts, fixture_deny = module._offline_launcher().install_tripwires(profile)
                real_attempts.append(attempts)
                return fixture_attempts, fixture_deny
            module.install_signin_guards = guards
            from PySide6.QtCore import QTimer
            from PySide6.QtWidgets import QApplication
            original_exec = QApplication.exec
            def checked_exec(self):
                def inspect():
                    window = next(w for w in self.topLevelWidgets() if type(w).__name__ == 'MainWindow')
                    window.signin_button.click()
                    from gui.desktop_signin_dialog import DesktopSignInDialog
                    dialogs = window.findChildren(DesktopSignInDialog)
                    assert len(dialogs) == 1
                    dialog = dialogs[0]
                    assert dialog.identity is None and not dialog.busy
                    assert not dialog.signin_button.isEnabled()
                    assert dialog.choose_button.isEnabled()
                    dialog.close()
                    assert dialog.closed and not dialog.busy
                    inspected.append(True)
                    self.quit()
                QTimer.singleShot(0, inspect)
                QTimer.singleShot(5000, self.quit)
                return original_exec()
            QApplication.exec = checked_exec
            status = module.main(['--app-source', str(root), '--platform', 'offscreen'])
            assert not any(real_attempts) and inspected == [True]
            raise SystemExit(status)
        ''')
        result = self.check_result([str(self.python), '-B', '-c', script, str(self.application)], synthetic=False)
        self.assertEqual(result['outcomes'], [])
        self.assertEqual(result['qt_warnings'], [])


if __name__ == '__main__':
    unittest.main()
