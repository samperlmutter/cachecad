"""Upgrade failures retain bounded redacted evidence before fixture cleanup."""
import copy
from contextlib import redirect_stderr
import io
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / 'packaging/windows'
if not (PACKAGING / 'test_upgrade.py').is_file():
    raise unittest.SkipTest('Windows packaging absent from isolated scaffold wheel')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


support = load('_upgrade_evidence_support', PACKAGING / 'support.py')
with patch.dict(sys.modules, {'support': support}):
    installer = load('_upgrade_evidence_installer', PACKAGING / 'test_installer.py')
    with patch.dict(sys.modules, {'test_installer': installer}):
        upgrade = load('_upgrade_evidence_test', PACKAGING / 'test_upgrade.py')


class UpgradeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.profile = self.root / 'startup-profiles/check'
        self.directory = self.profile / 'diagnostics'
        self.directory.mkdir(parents=True)
        self.reports = self.root / 'reports'
        self.reports.mkdir()
        self.labels = {'launch.py': {'main', 'run_application'}, '<external>': {'<redacted>'}}
        self.identity = {'version': '0.1.1', 'release_number': 2,
                         'windows_version': '0.1.1.2', 'product_id': 'cachecad-reconstructed'}
        self.record = {'schema': 1, 'application': 'CacheCAD', 'version': '0.1.1',
            'build_sha256': 'a' * 64, 'platform': {'os': 'windows', 'python': '3.12.10', 'bits': 64},
            'timestamp_utc': '2026-10-18T23:40:40.123456+00:00', 'kind': 'startup-failure',
            'frames': [{'source': 'launch.py', 'function': 'run_application', 'line': 355}],
            'breadcrumbs': ['bootstrap_verified'], 'exception_type': 'ApplicationError'}

    def write_record(self, value=None, number=0):
        path = self.directory / ('crash-' + format(number, '032x') + '.json')
        path.write_text(json.dumps(value if value is not None else self.record))
        return path

    def collect(self):
        return upgrade.collect_startup_crashes(self.profile, self.identity, 'a' * 64, self.labels)

    def test_collects_only_reviewed_crash_json_without_touching_private_fixture_files(self):
        self.write_record()
        for name in ('settings.json', 'google-session.dpapi', 'crash-not-a-uuid.json', '.crash-private.tmp'):
            (self.directory / name).write_bytes(b'SYNTHETIC PRIVATE BYTES')
        (self.profile / 'settings.json').write_bytes(b'SYNTHETIC SECRET')
        original = Path.open
        def guarded_open(path, *args, **kwargs):
            if not path.name.startswith('crash-0000'):
                raise AssertionError('Collector opened a non-crash file')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded_open):
            result = self.collect()
        self.assertEqual(result['reports'], [self.record])
        self.assertEqual(result['rejected_count'], 0)
        self.assertNotIn('SYNTHETIC', json.dumps(result))

    def test_rejects_unknown_fields_paths_custom_names_and_invalid_identity(self):
        mutations = [lambda r: r.update(message='SYNTHETIC SECRET'),
                     lambda r: r.update(version='other'), lambda r: r.update(build_sha256='b' * 64),
                     lambda r: r.update(exception_type='SecretCustomName'),
                     lambda r: r.update(breadcrumbs=['private/path']),
                     lambda r: r['frames'][0].update(source='C:/private/launch.py'),
                     lambda r: r['frames'][0].update(function='private_function'),
                     lambda r: r['frames'][0].update(line=True),
                     lambda r: r.update(kind=['unhashable']),
                     lambda r: r.update(timestamp_utc='private time'),
                     lambda r: r.update(frames=[r['frames'][0]] * 65)]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                record = copy.deepcopy(self.record)
                mutation(record)
                self.write_record(record)
                result = self.collect()
                self.assertEqual(result['reports'], [])
                self.assertEqual(result['rejected_count'], 1)
                self.assertNotIn('private', json.dumps(result))

    def test_collection_is_size_count_and_scan_bounded(self):
        for number in range(12):
            self.write_record(number=number)
        result = self.collect()
        self.assertEqual(len(result['reports']), 8)
        self.assertEqual(result['rejected_count'], 4)
        for path in self.directory.iterdir():
            path.unlink()
        self.write_record().write_bytes(b'x' * (upgrade.CRASH_BYTES_LIMIT + 1))
        with patch.object(Path, 'open', side_effect=AssertionError('Oversized file must not be opened')):
            self.assertEqual(self.collect()['rejected_count'], 1)
        for number in range(65):
            (self.directory / ('ignored-' + str(number))).touch()
        self.assertTrue(self.collect()['scan_limited'])

    def test_linked_directory_and_hardlinked_report_are_not_exported(self):
        path = self.write_record()
        import os
        os.link(path, self.root / 'second-link.json')
        self.assertEqual(self.collect()['rejected_count'], 1)
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path == self.directory or original(path)):
            self.assertEqual(self.collect()['status'], 'linked-directory-refused')

    def test_missing_and_unreadable_reports_remain_explicit_without_raw_errors(self):
        self.directory.rmdir()
        self.assertEqual(self.collect()['status'], 'absent')
        self.directory.mkdir()
        with patch.object(upgrade.os, 'scandir', side_effect=PermissionError('SYNTHETIC SECRET PATH')):
            result = self.collect()
        self.assertEqual(result['status'], 'unreadable')
        self.assertNotIn('SYNTHETIC', json.dumps(result))

    def invoke(self, process):
        with patch.object(upgrade, 'reviewed_crash_labels', return_value=self.labels), \
                patch.object(upgrade, 'sha256', return_value='a' * 64), \
                patch.object(upgrade, 'startup_gate_snapshot', return_value={'Install': {'presence': 'absent'}}), \
                patch.object(upgrade.subprocess, 'run', side_effect=process if isinstance(process, BaseException) else None,
                             return_value=process) as run:
            with self.assertRaises(RuntimeError):
                upgrade.native_startup(self.root / 'installed', self.root, {}, self.reports, 'check', self.identity)
            self.assertEqual(run.call_count, 1)
        return json.loads((self.reports / 'check.json').read_text())

    def test_nonzero_exit_exports_crash_before_cleanup_and_never_retries(self):
        self.write_record()
        result = self.invoke(SimpleNamespace(returncode=1, stdout='', stderr='Generic startup failure'))
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['stage'], 'process')
        self.assertEqual(result['launch_attempts'], 1)
        self.assertEqual(result['crash_diagnostics']['reports'], [self.record])
        self.assertEqual(result['gates_before'], result['gates_after'])
        self.assertNotIn(str(self.profile), json.dumps(result))
        self.assertEqual((self.reports / 'check.stderr.log').read_text(), 'Generic startup failure')

    def test_timeout_exports_evidence_without_retry_and_validation_failure_is_separate(self):
        result = self.invoke(subprocess.TimeoutExpired(['redacted-command'], 90, output=b'bounded stdout', stderr=b''))
        self.assertTrue(result['timed_out'])
        self.assertEqual(result['launch_attempts'], 1)
        with patch.object(upgrade, 'validate_application_smoke', side_effect=RuntimeError('validation failed')):
            result = self.invoke(SimpleNamespace(returncode=0, stdout='invalid', stderr=''))
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['stage'], 'smoke-validation')
        self.assertEqual(result['status'], 'failed')

    def test_success_keeps_identity_and_preservation_assertions(self):
        record = dict(self.identity, preserved_profile_unchanged=True)
        with patch.object(upgrade, 'reviewed_crash_labels', return_value=self.labels), \
                patch.object(upgrade, 'sha256', return_value='a' * 64), \
                patch.object(upgrade, 'startup_gate_snapshot', return_value={}), \
                patch.object(upgrade, 'validate_application_smoke', return_value=record), \
                patch.object(upgrade.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')) as run:
            self.assertEqual(upgrade.native_startup(self.root, self.root, {}, self.reports, 'check',
                             self.identity, preserved_profile=self.profile), record)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs['env']['CACHECAD_ENABLE_UPGRADE_TESTS'], '1')
            record['preserved_profile_unchanged'] = False
            with self.assertRaisesRegex(RuntimeError, 'unchanged'):
                upgrade.native_startup(self.root, self.root, {}, self.reports, 'check',
                                       self.identity, preserved_profile=self.profile)
        self.assertEqual(json.loads((self.reports / 'check.json').read_text())['status'], 'failed')


class BootstrapDiagnosticTests(unittest.TestCase):
    def test_only_startup_check_exports_pending_reviewed_diagnostics(self):
        launcher = load('_upgrade_bootstrap_launcher', ROOT / 'application/launch.py')
        diagnostics = load('_upgrade_bootstrap_diagnostics', ROOT / 'application/crash_diagnostics.py')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'assembly-manifest.json').write_text('{}')
            source = b"def fail():\n    raise ValueError('SYNTHETIC_SECRET_EXCEPTION_MESSAGE')\n"
            namespace = {}
            exec(compile(source, str(root / 'src/core/reviewed.py'), 'exec'), namespace)
            sources = {'application/launch.py': (ROOT / 'application/launch.py').read_bytes(),
                       'src/core/reviewed.py': source}
            for startup, persisted in ((True, False), (False, False), (True, True)):
                with self.subTest(startup=startup, persisted=persisted):
                    args = SimpleNamespace(app_source=root, startup_check=startup, diagnostic_native_helper=False)
                    def failure(args, root, owner, *unused):
                        if persisted:
                            owner.activate(root / 'profile/diagnostics')
                        namespace['fail']()
                    stderr = io.StringIO()
                    with patch.object(launcher, 'parse_arguments', return_value=args), \
                            patch.object(launcher, 'verify_source', return_value=sources), \
                            patch.object(launcher, 'install_source_importer'), \
                            patch.object(launcher, 'run_application', side_effect=failure), \
                            patch.object(sys, 'dont_write_bytecode', sys.dont_write_bytecode), \
                            patch.dict(sys.modules, {'crash_diagnostics': diagnostics,
                                       'build_version': SimpleNamespace(APP_VERSION='0.1.1')}), redirect_stderr(stderr):
                        self.assertEqual(launcher.main([]), 1)
                    output = stderr.getvalue()
                    self.assertNotIn('SYNTHETIC_SECRET', output)
                    self.assertNotIn(str(root), output)
                    prefix = 'CACHECAD_STARTUP_DIAGNOSTIC='
                    lines = [line[len(prefix):] for line in output.splitlines() if line.startswith(prefix)]
                    self.assertEqual(len(lines), int(startup and not persisted))
                    if lines:
                        record = json.loads(lines[0])
                        self.assertEqual(record['exception_type'], 'ValueError')
                        self.assertEqual(record['frames'][-1], {'source': 'reviewed.py', 'function': 'fail', 'line': 2})
                        self.assertEqual(record['breadcrumbs'], ['bootstrap_verified'])
                    if persisted:
                        self.assertEqual(len(list((root / 'profile/diagnostics').glob('crash-*.json'))), 1)


if __name__ == '__main__':
    unittest.main()
