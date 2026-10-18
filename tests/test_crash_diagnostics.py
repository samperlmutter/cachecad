"""Local-only fault injection. Never use a real profile, token or CAD file."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'application/crash_diagnostics.py'
if not MODULE.is_file():
    raise unittest.SkipTest('Production diagnostics absent from replacement wheel')
spec = importlib.util.spec_from_file_location('_crash_diagnostics_test', MODULE)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


def read_reports(directory):
    return [json.loads(path.read_text()) for path in sorted(Path(directory).glob('crash-*.json'))]


class CrashDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.root / 'diagnostics'
        self.owner = diag.CrashDiagnostics(build='a' * 64)
        self.addCleanup(self.owner.close)
        self.owner.activate(self.directory)

    def test_exception_never_persists_messages_locals_chains_paths_or_names(self):
        secret = 'SYNTHETIC_ACCESS_TOKEN_DO_NOT_RECORD'
        fake_path = '/Users/private-owner/private-project/secret-name.py'
        namespace = {}
        exec(compile("def run():\n    local_token = 'SYNTHETIC_LOCAL'\n    raise ValueError('https://provider.invalid/?token=SYNTHETIC_URL')", fake_path, 'exec'), namespace)
        try:
            try:
                raise KeyError(secret)
            except KeyError:
                namespace['run']()
        except Exception:
            self.assertTrue(self.owner.record_exception('unhandled-main', *sys.exc_info()))
        text = next(self.directory.glob('crash-*.json')).read_text()
        for forbidden in (secret, 'SYNTHETIC_LOCAL', 'SYNTHETIC_URL', fake_path,
                          'private-owner', 'secret-name', 'https://', 'local_token'):
            self.assertNotIn(forbidden, text)
        record = json.loads(text)
        self.assertEqual(record['exception_type'], 'ValueError')
        self.assertTrue(record['frames'])
        self.assertTrue(all(frame['source'] == '<external>' for frame in record['frames']))
        self.assertEqual(record['build_sha256'], 'a' * 64)
        self.assertEqual(record['version'], diag.APP_VERSION)
        self.assertIn('python', record['platform'])

    def test_only_reviewed_filenames_and_function_names_are_retained(self):
        raw = b"def deliberate_failure():\n    raise ValueError('SYNTHETIC')\n"
        catalog = diag.source_catalog(self.root, {'src/core/reviewed.py': raw})
        self.owner.catalog = catalog
        namespace = {}
        exec(compile(raw, str(self.root / 'src/core/reviewed.py'), 'exec'), namespace)
        try:
            namespace['deliberate_failure']()
        except ValueError:
            self.owner.record_exception('unhandled-main', *sys.exc_info())
        record = read_reports(self.directory)[0]
        self.assertEqual(record['frames'][-1], {'source': 'reviewed.py', 'function': 'deliberate_failure', 'line': 2})
        self.assertNotIn(str(self.root), json.dumps(record))

    def test_custom_exception_name_and_value_are_not_read_or_persisted(self):
        class SensitiveError(Exception):
            def __str__(self):
                raise AssertionError('Must never format an exception')
        SensitiveError.__name__ = 'SYNTHETIC_SECRET_CLASS'
        self.assertTrue(self.owner.record_exception('unhandled-main', SensitiveError, SensitiveError(), None))
        record = read_reports(self.directory)[0]
        self.assertEqual(record['exception_type'], 'ApplicationError')
        self.assertNotIn('SYNTHETIC', json.dumps(record))

    def test_breadcrumb_allowlist_and_bounded_retention(self):
        self.owner.mark('https://provider.invalid/?token=SYNTHETIC')
        for _ in range(30):
            self.owner.mark('window_shown')
            self.assertTrue(self.owner.record_event('shutdown-timeout'))
        files = list(self.directory.glob('crash-*.json'))
        self.assertEqual(len(files), diag.MAX_REPORTS)
        self.assertTrue(all(path.stat().st_size <= diag.MAX_REPORT_BYTES for path in files))
        self.assertEqual(list(self.directory.glob('*.tmp')), [])
        self.assertEqual(len(read_reports(self.directory)[0]['breadcrumbs']), diag.MAX_BREADCRUMBS)
        self.assertTrue(all(set(record['breadcrumbs']) == {'window_shown'} for record in read_reports(self.directory)))

    def test_stale_interrupted_temporary_reports_are_cleaned_but_active_writes_survive(self):
        stale = self.directory / ('.crash-' + 'a' * 32 + '.json.tmp')
        active = self.directory / ('.crash-' + 'b' * 32 + '.json.tmp')
        recent = self.directory / ('.crash-' + 'c' * 32 + '.json.tmp')
        unrelated = self.directory / '.other-file.tmp'
        for path in (stale, active, recent, unrelated):
            path.write_bytes(b'{}')
        old = time.time() - diag.TEMP_MAX_AGE_SECONDS - 100
        for path in (stale, active, unrelated):
            os.utime(path, (old, old))
        with active.open('r+b') as stream:
            diag._lock_temporary(stream.fileno())
            self.assertTrue(diag.CrashDiagnostics().activate(self.directory))
            self.assertFalse(stale.exists())
            self.assertTrue(active.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(unrelated.exists())
        self.assertTrue(diag.CrashDiagnostics().activate(self.directory))
        self.assertFalse(active.exists())
        self.assertTrue(recent.exists())

    def test_prior_report_is_discoverable_on_next_activation(self):
        self.assertFalse(self.owner.previous_reports)
        self.owner.record_event('shutdown-timeout')
        second = diag.CrashDiagnostics()
        self.assertTrue(second.activate(self.directory))
        self.assertTrue(second.previous_reports)

    def test_hooks_cover_python_threads_and_actual_qt_sys_hook_without_raw_chaining(self):
        previous = (sys.excepthook, threading.excepthook)
        self.owner.install()
        callback = []
        self.owner.set_qt_callback(lambda: callback.append('recorded'))
        sys.excepthook(ValueError, ValueError('SYNTHETIC'), None)
        thread = threading.Thread(target=lambda: (_ for _ in ()).throw(RuntimeError('SYNTHETIC')))
        thread.start()
        thread.join()
        self.assertEqual(callback, ['recorded'])
        self.assertEqual({r['kind'] for r in read_reports(self.directory)}, {'qt-callback', 'unhandled-thread'})
        self.owner.close()
        self.assertIs(sys.excepthook, previous[0])
        self.assertIs(threading.excepthook, previous[1])

    def test_filesystem_failure_never_escapes_into_the_app_or_raw_hook(self):
        with patch.object(diag, '_write', side_effect=OSError('SYNTHETIC private path')):
            self.owner.install()
            sys.excepthook(ValueError, ValueError('SYNTHETIC token'), None)
            self.assertFalse(self.owner.record_event('shutdown-timeout'))
        self.assertEqual(read_reports(self.directory), [])
        invalid = self.root / 'not-a-directory'
        invalid.write_text('untouched')
        self.assertFalse(diag.CrashDiagnostics().activate(invalid))
        self.assertEqual(invalid.read_text(), 'untouched')

    def test_native_ready_protocol_accepts_unix_and_windows_line_endings(self):
        for ending in (b'\n', b'\r\n'):
            with self.subTest(ending=ending):
                response = b'CACHECAD_DIAGNOSTICS_READY' + ending
                program = ("import os, sys; os.write(1, " + repr(response)
                           + "); sys.stdin.buffer.read()")
                self.assertTrue(self.owner.enable_native([sys.executable, '-B', '-c', program]))
                child = self.owner._native
                self.owner.close()
                self.assertIsNotNone(child.poll())
        self.assertEqual(read_reports(self.directory), [])

    def test_unready_real_helper_is_reaped_and_capture_fails_safely(self):
        launched = []
        real_popen = subprocess.Popen
        def start(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            launched.append(child)
            return child
        with patch.object(diag.subprocess, 'Popen', side_effect=start):
            self.assertFalse(self.owner.enable_native([sys.executable, '-B', '-c',
                "import time; print('NOT_READY', flush=True); time.sleep(60)"]))
        self.assertEqual(len(launched), 1)
        self.assertIsNotNone(launched[0].poll())
        self.assertIsNone(self.owner._native)
        self.assertEqual(read_reports(self.directory), [])

    def test_native_windows_exception_is_classified_without_raw_details(self):
        stream = io.BytesIO(b'Windows fatal exception: access violation SYNTHETIC_SECRET\n'
                            b'  File "C:\\Users\\private\\secret.py", line 24 in secret_function\n')
        self.assertEqual(diag.capture_native(self.directory, {}, '', stream), 0)
        record = read_reports(self.directory)[0]
        self.assertEqual(record['kind'], 'native-fatal')
        self.assertEqual(record['frames'][0]['source'], '<external>')
        for forbidden in ('SYNTHETIC', 'private', 'secret.py', 'secret_function'):
            self.assertNotIn(forbidden, json.dumps(record))

    def test_frozen_launcher_uses_only_its_reviewed_source_alias(self):
        with patch.object(sys, 'frozen', True, create=True):
            catalog = diag.source_catalog(self.root, {'application/launch.py': b'def main(): pass'})
        self.assertEqual(diag._frame(catalog, 'launch.py', 'main', 1),
                         {'source': 'launch.py', 'function': 'main', 'line': 1})
        self.assertEqual(diag._frame(catalog, 'launch.py', 'SYNTHETIC_SECRET', 1)['function'],
                         '<redacted>')

    def test_stalled_native_helper_is_terminated_killed_and_reaped(self):
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired('helper', 2),
                                    subprocess.TimeoutExpired('helper', 2), 0]
        process.poll.return_value = -9
        diag._stop_helper(process, timeout=2)
        process.stdin.close.assert_called_once()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 3)
        process.stdout.close.assert_called_once()

    def test_native_parser_discards_raw_paths_exception_text_and_arbitrary_lines(self):
        stream = io.BytesIO(b'CACHECAD_EVENT window_shown\nFatal Python error: SYNTHETIC_TOKEN\n'
                            b'Current thread 0xdeadbeef\n'
                            b'  File "/private/SYNTHETIC_PATH.py", line 24 in SYNTHETIC_FUNCTION\n'
                            b'https://provider.invalid/?token=SYNTHETIC_URL\n'
                            b'CACHECAD_EVENT SYNTHETIC_BREADCRUMB\n')
        self.assertEqual(diag.capture_native(self.directory, {}, 'a' * 64, stream), 0)
        record = read_reports(self.directory)[0]
        self.assertEqual(record['kind'], 'native-fatal')
        self.assertEqual(record['breadcrumbs'], ['window_shown'])
        self.assertEqual(record['frames'], [{'source': '<external>', 'function': '<redacted>', 'line': 24}])
        self.assertNotIn('SYNTHETIC', json.dumps(record))
        self.assertNotIn('deadbeef', json.dumps(record))

    def test_native_clean_eof_and_overlong_lines_do_not_create_false_reports(self):
        self.assertEqual(diag.capture_native(self.directory, {}, '', io.BytesIO(b'CACHECAD_EVENT window_shown\n')), 0)
        self.assertEqual(diag.capture_native(self.directory, {}, '', io.BytesIO(b'x' * 4096 + b'Fatal Python error: Aborted\n')), 0)
        self.assertEqual(read_reports(self.directory), [])

    @unittest.skipUnless(hasattr(os, 'symlink'), 'Requires symlink support')
    def test_linked_directory_is_rejected_without_writing_to_target(self):
        target = self.root / 'target'
        target.mkdir()
        link = self.root / 'linked'
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('Host cannot create symlinks')
        owner = diag.CrashDiagnostics()
        self.assertFalse(owner.activate(link / 'diagnostics'))
        self.assertEqual(list(target.iterdir()), [])

    def test_main_and_thread_hooks_in_disposable_real_subprocesses(self):
        for scenario in ('main', 'thread'):
            with self.subTest(scenario=scenario):
                directory = self.root / scenario
                program = """
import importlib.util, pathlib, sys, threading
spec = importlib.util.spec_from_file_location('diagnostics', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
owner = module.CrashDiagnostics(build='a'*64).install()
owner.activate(pathlib.Path(sys.argv[2]))
def fail():
    raise RuntimeError('SYNTHETIC_SECRET_UNHANDLED')
if sys.argv[3] == 'main':
    fail()
else:
    worker = threading.Thread(target=fail); worker.start(); worker.join()
    owner.close()
"""
                run = subprocess.run([sys.executable, '-B', '-c', program, str(MODULE), str(directory), scenario],
                                     text=True, capture_output=True, timeout=15)
                self.assertEqual(run.returncode, 1 if scenario == 'main' else 0, run.stderr)
                self.assertNotIn('SYNTHETIC', run.stdout + run.stderr)
                records = read_reports(directory)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['kind'], 'unhandled-' + scenario)
                self.assertNotIn('SYNTHETIC', json.dumps(records))

    @unittest.skipUnless(os.name == 'posix', 'Portable native crash injection uses POSIX resource limits')
    def test_actual_native_abort_is_sanitized_and_durable_after_parent_death(self):
        program = """
import importlib.util, os, pathlib, resource, sys
spec = importlib.util.spec_from_file_location('diagnostics', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
root = pathlib.Path(sys.argv[2]); root.mkdir()
owner = module.CrashDiagnostics(build='a'*64).install(); owner.activate(root)
helper = '''import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('diagnostics', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
raise SystemExit(module.capture_native(pathlib.Path(sys.argv[2]), {}, 'a'*64, announce=True))'''
assert owner.enable_native([sys.executable, '-B', '-c', helper, sys.argv[1], str(root)])
owner.mark('window_shown')
private_token = 'SYNTHETIC_NATIVE_TOKEN'
os.abort()
"""
        directory = self.root / 'native'
        run = subprocess.run([sys.executable, '-B', '-c', program, str(MODULE), str(directory)],
                             text=True, capture_output=True, timeout=15)
        self.assertNotEqual(run.returncode, 0)
        self.assertNotIn('SYNTHETIC', run.stdout + run.stderr)
        deadline = time.monotonic() + 10
        while not list(directory.glob('crash-*.json')) and time.monotonic() < deadline:
            time.sleep(.05)
        records = read_reports(directory)
        self.assertEqual(len(records), 1, run.stderr)
        self.assertEqual(records[0]['kind'], 'native-fatal')
        self.assertTrue(records[0]['frames'])
        self.assertIn('native_capture_ready', records[0]['breadcrumbs'])
        self.assertNotIn('SYNTHETIC', json.dumps(records))
        self.assertNotIn(str(self.root), json.dumps(records))


class DiagnosticLauncherTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        spec = importlib.util.spec_from_file_location('_diagnostic_launch_test', ROOT / 'application/launch.py')
        self.launch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.launch)

    def test_fault_flags_require_all_disposable_test_gates(self):
        base = ['--app-source', str(self.root), '--diagnostic-test', 'main']
        for extra, env in (([], '1'), (['--startup-check'], '1'),
                           (['--startup-check', '--profile-dir', str(self.root)], '0')):
            with self.subTest(extra=extra, env=env), patch.dict(os.environ, CACHECAD_ENABLE_DIAGNOSTIC_TESTS=env), \
                    patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit):
                self.launch.parse_arguments(base + extra)
        with patch.dict(os.environ, CACHECAD_ENABLE_DIAGNOSTIC_TESTS='1'):
            args = self.launch.parse_arguments(base + ['--startup-check', '--profile-dir', str(self.root)])
        self.assertEqual(args.diagnostic_test, 'main')
        (self.root / 'existing').write_text('never touch')
        with self.assertRaises(ValueError):
            self.launch.prepare_profile(args.profile_dir, startup_check=True)

    def test_helper_does_not_write_unmarked_or_unlocked_profile(self):
        module_spec = importlib.util.spec_from_file_location('utils.profile_lock', ROOT / 'maintained/utils/profile_lock.py')
        lock_module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(lock_module)
        from types import SimpleNamespace
        args = SimpleNamespace(profile_dir=self.root)
        with patch.dict(sys.modules, {'utils.profile_lock': lock_module}):
            self.assertEqual(self.launch.run_native_helper(args, diag, {}, ''), 1)
            self.launch.prepare_profile(self.root)
            with lock_module.ProfileLock(self.root):
                pass
            self.assertEqual(self.launch.run_native_helper(args, diag, {}, ''), 1)
        self.assertFalse((self.root / 'diagnostics').exists())

    def test_application_helper_is_required_in_verified_source_snapshot(self):
        (self.root / 'src').mkdir()
        required = ['src/core/auth_manager.py', 'src/core/google_session.py',
                    'src/utils/application_mode.py', 'src/gui/application_account.py',
                    'application/resources_rc.py', 'application/launch.py']
        records = []
        for relative in required:
            path = self.root / relative
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_bytes(b'# test\n')
            records.append({'path': relative, 'sha256': hashlib.sha256(b'# test\n').hexdigest()})
        manifest = self.root / 'assembly-manifest.json'
        manifest.write_text(json.dumps({'files': records}))
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.launch.verify_source(self.root)
        path = self.root / 'application/crash_diagnostics.py'
        path.write_bytes(MODULE.read_bytes())
        records.append({'path': 'application/crash_diagnostics.py', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        manifest.write_text(json.dumps({'files': records}))
        sources = self.launch.verify_source(self.root)
        self.assertEqual(sources['application/crash_diagnostics.py'], MODULE.read_bytes())


@unittest.skipUnless(os.environ.get('CACHECAD_RUNTIME_PYTHON'),
                     'Set CACHECAD_RUNTIME_PYTHON for actual application/Qt fault tests')
class ProductionDiagnosticRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tooling.assemble_app import assemble
        cls.temporary = tempfile.TemporaryDirectory(prefix='cachecad-diagnostic-runtime-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / 'app-source'
        assemble(ROOT, cls.source, production=True)
        cls.runtime = os.environ['CACHECAD_RUNTIME_PYTHON']

    def test_actual_launcher_faults_and_clean_startup(self):
        environment = dict(os.environ, CACHECAD_ENABLE_DIAGNOSTIC_TESTS='1',
                           QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1')
        environment.pop('PYTHONPATH', None)
        cases = {'main': 'startup-failure', 'thread': 'unhandled-thread', 'qt': 'qt-callback',
                 'native': 'native-fatal', 'clean': None}
        for scenario, expected in cases.items():
            with self.subTest(scenario=scenario):
                profile = self.root / scenario
                command = [self.runtime, '-B', str(self.source / 'application/launch.py'),
                           '--app-source', str(self.source), '--startup-check',
                           '--profile-dir', str(profile)]
                if scenario != 'clean':
                    command += ['--diagnostic-test', scenario]
                result = subprocess.run(command, env=environment, text=True,
                                        capture_output=True, timeout=30)
                if scenario == 'clean':
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    record = json.loads(next(line.split('=', 1)[1] for line in result.stdout.splitlines()
                                             if line.startswith('CACHECAD_APPLICATION_RESULT=')))
                    self.assertEqual(record['external_attempts'], [])
                    self.assertTrue(record['diagnostics']['native_capture'])
                    self.assertEqual(read_reports(profile / 'diagnostics'), [])
                    continue
                self.assertNotEqual(result.returncode, 0, result.stdout)
                if scenario != 'native':
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                deadline = time.monotonic() + 10
                while not list((profile / 'diagnostics').glob('crash-*.json')) and time.monotonic() < deadline:
                    time.sleep(.05)
                reports = read_reports(profile / 'diagnostics')
                self.assertEqual(len(reports), 1, result.stdout + result.stderr)
                self.assertEqual(reports[0]['kind'], expected)
                self.assertNotIn(str(profile), json.dumps(reports))
                self.assertNotIn('Synthetic diagnostic', json.dumps(reports))
                self.assertTrue(reports[0]['frames'])


if __name__ == '__main__':
    unittest.main()
