"""Linux interaction checks are opt-in, real Qt and strictly signed out.

Set CACHECAD_RUNTIME_PYTHON to run the actual assembled application offscreen.
The Linux package harness separately runs the extracted frozen binary under X11.
No test in this module creates a credential, Google session or mocked service.
"""
from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'application/launch.py'
if not MODULE.is_file():
    raise unittest.SkipTest('Maintained launcher absent from scaffold wheel')
spec = importlib.util.spec_from_file_location('_linux_interaction_launcher', MODULE)
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)
RUNTIME = os.environ.get('CACHECAD_RUNTIME_PYTHON')
CHECKS = ['settings_repeated_clicks_and_back', 'missing_configuration_rejected',
          'directory_modal_escape_and_navigation', 'ordinary_close_and_reopen',
          'explicit_exit_repeated']


class LinuxInteractionContracts(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.profile = self.directory / 'new-profile'
        self.config = self.directory / 'missing-client.json'
        self.arguments = ['--app-source', str(self.directory / 'source'), '--startup-check',
                          '--interaction-check', '--profile-dir', str(self.profile),
                          '--client-config', str(self.config)]

    def parse(self, arguments=None, *, platform='linux', enabled=True):
        with patch.object(launch.sys, 'platform', platform), patch.dict(os.environ, {
                'CACHECAD_ENABLE_INTERACTION_TESTS': '1' if enabled else '',
                'CACHECAD_ENABLE_DIAGNOSTIC_TESTS': '1',
                'CACHECAD_ENABLE_UPGRADE_TESTS': '1'}), redirect_stderr(io.StringIO()):
            return launch.parse_arguments(self.arguments if arguments is None else arguments)

    def test_xcb_is_linux_only_and_existing_platforms_keep_their_guards(self):
        base = ['--app-source', str(self.directory), '--startup-check']
        for platform, flag in (('linux', 'xcb'), ('win32', 'windows'), ('darwin', 'cocoa')):
            self.assertEqual(self.parse(base + ['--platform', flag], platform=platform).platform, flag)
            for other in {'linux', 'win32', 'darwin'} - {platform}:
                with self.subTest(flag=flag, platform=other), self.assertRaises(SystemExit):
                    self.parse(base + ['--platform', flag], platform=other)

    def test_interaction_mode_requires_explicit_startup_environment_and_profile(self):
        self.assertTrue(self.parse().interaction_check)
        with self.assertRaises(SystemExit):
            self.parse(enabled=False)
        for flag in ('--startup-check', '--profile-dir'):
            arguments = list(self.arguments)
            index = arguments.index(flag)
            del arguments[index:index + (2 if flag == '--profile-dir' else 1)]
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                self.parse(arguments)
        for platform in ('darwin', 'win32'):
            with self.subTest(platform=platform), self.assertRaises(SystemExit):
                self.parse(platform=platform)

    def test_interaction_mode_cannot_mix_fault_or_preserved_profile_modes(self):
        for extra in (['--diagnostic-test', 'main'], ['--diagnostic-test', 'qt'],
                      ['--diagnostic-native-helper'], ['--preserved-profile-check']):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.parse(self.arguments + extra)

    def test_existing_or_dangling_link_client_config_is_rejected(self):
        self.config.write_text('{}')
        with self.assertRaises(SystemExit):
            self.parse()
        self.config.unlink()
        self.config.symlink_to(self.directory / 'absent-target')
        with self.assertRaises(SystemExit):
            self.parse()

    def test_profile_must_be_below_the_disposable_temp_root(self):
        with patch.object(launch.tempfile, 'gettempdir', return_value=str(self.profile)):
            with self.assertRaises(SystemExit):
                self.parse()
        with patch.object(launch.tempfile, 'gettempdir', return_value=str(self.directory / 'elsewhere')):
            with self.assertRaises(SystemExit):
                self.parse()

    def test_existing_profile_contents_and_linked_profile_are_rejected_before_use(self):
        self.profile.mkdir()
        marker = self.profile / 'unrelated-user-file'
        marker.write_bytes(b'preserve')
        with self.assertRaisesRegex(ValueError, 'new empty'):
            launch.prepare_profile(self.profile, startup_check=True)
        self.assertEqual(marker.read_bytes(), b'preserve')
        link = self.directory / 'linked-profile'
        link.symlink_to(self.profile, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Linked'):
            launch.prepare_profile(link, startup_check=True)

    def test_normal_startup_does_not_enable_interaction_check(self):
        args = self.parse(['--app-source', str(self.directory), '--startup-check'])
        self.assertFalse(args.interaction_check)


@unittest.skipUnless(RUNTIME and sys.platform == 'linux',
                     'Set CACHECAD_RUNTIME_PYTHON for real Linux Qt interaction coverage')
class LinuxInteractionRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tooling.assemble_app import assemble
        cls.temporary = tempfile.TemporaryDirectory(prefix='cachecad-real-interactions-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.source = cls.directory / 'app-source'
        assemble(ROOT, cls.source, production=True)

    def test_real_controls_modal_close_and_explicit_quit(self):
        profile = self.directory / 'profile'
        env = dict(os.environ, CACHECAD_ENABLE_INTERACTION_TESTS='1',
                   QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1')
        env.pop('PYTHONPATH', None)
        env.pop('PYTHONHOME', None)
        run = subprocess.run([RUNTIME, '-B', str(self.source / 'application/launch.py'),
            '--app-source', str(self.source), '--startup-check', '--interaction-check',
            '--platform', 'offscreen', '--profile-dir', str(profile),
            '--client-config', str(self.directory / 'absent-client.json')],
            cwd=self.directory, env=env, text=True, capture_output=True, timeout=25)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        result = json.loads(next(line.split('=', 1)[1] for line in run.stdout.splitlines()
                                 if line.startswith('CACHECAD_APPLICATION_RESULT=')))
        self.assertEqual(json.loads((profile / 'startup-check-result.json').read_text()), result)
        self.assertEqual(result['actual_gui'], 'gui.main_window.MainWindow')
        self.assertEqual(result['qt_platform'], 'offscreen')
        self.assertFalse(result['frozen'])
        for key in ('signed_in', 'has_credentials', 'drive_client_created'):
            self.assertFalse(result[key], key)
        for key in ('setup_required', 'network_disabled', 'sdk_imports_verified'):
            self.assertTrue(result[key], key)
        for key in ('errors', 'external_attempts', 'credential_files',
                    'development_modules_loaded', 'forbidden_modules_loaded'):
            self.assertEqual(result[key], [], key)
        self.assertEqual(result['active_threads'], 0)
        interaction = result['interaction_check']
        self.assertTrue(interaction['completed'])
        self.assertTrue(interaction['shutdown_signal_received'])
        self.assertEqual(interaction['checks'], CHECKS)
        self.assertEqual([item['file'] for item in interaction['screenshots']],
                         ['interaction-settings.png', 'interaction-home.png'])
        for item in interaction['screenshots']:
            data = (profile / item['file']).read_bytes()
            self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(hashlib.sha256(data).hexdigest(), item['sha256'])
            self.assertEqual(list(struct.unpack('>II', data[16:24])), item['size'])
            self.assertGreater(item['size'][0], 100)
            self.assertGreater(item['size'][1], 100)

    def test_injected_interaction_failure_terminates_without_sensitive_error_text(self):
        for mode in ('navigation', 'snapshot'):
            with self.subTest(mode=mode):
                profile = self.directory / ('failed-' + mode)
                env = dict(os.environ, CACHECAD_ENABLE_INTERACTION_TESTS='1',
                           QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1')
                env.pop('PYTHONPATH', None)
                env.pop('PYTHONHOME', None)
                run = subprocess.run([RUNTIME, '-B', str(Path(__file__).resolve()),
                    '--run-failure', str(self.source), str(profile), mode],
                    cwd=self.directory, env=env, text=True, capture_output=True, timeout=15)
                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                self.assertNotIn('private-test-error-value', run.stdout + run.stderr)
                self.assertFalse(any(path.name in ('token.json', 'creds.json', 'google-session.dpapi')
                                     for path in profile.rglob('*')))
                if mode == 'navigation':
                    result = json.loads((profile / 'startup-check-result.json').read_text())
                    self.assertFalse(result['interaction_check']['completed'])
                    self.assertTrue(result['interaction_check']['shutdown_signal_received'])
                    self.assertIn('Interaction check failed: settings_repeated_clicks_and_back',
                                  result['errors'])
                    self.assertEqual(result['external_attempts'], [])


def run_failure(source, profile, mode):
    """Inject only a local test callback failure around the real Qt application."""
    original = launch.schedule_interaction_check

    def fail(*args, **kwargs):
        raise RuntimeError('private-test-error-value')

    def schedule(app, window, controller, directory, result, errors, finish):
        window.previous_page = fail
        original(app, window, controller, directory, result, errors,
                 fail if mode == 'snapshot' else finish)

    launch.schedule_interaction_check = schedule
    return launch.main(['--app-source', str(source), '--startup-check', '--interaction-check',
                        '--platform', 'offscreen', '--profile-dir', str(profile),
                        '--client-config', str(profile / 'absent-client.json')])


if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == '--run-failure':
        raise SystemExit(run_failure(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]))
    unittest.main()
