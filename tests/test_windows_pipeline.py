"""Portable Windows delivery contracts; these do not claim Windows execution."""
import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest

try:
    import yaml
except ImportError:
    yaml = None


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/linux-ci.yml'
SOURCE_FIXTURE = (ROOT / 'development/launch.py').is_file()


@unittest.skipUnless(SOURCE_FIXTURE, 'Application sources absent in replacement wheel tests')
@unittest.skipIf(yaml is None, 'Optional PyYAML parser not installed')
class WindowsWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load(WORKFLOW.read_text(encoding='utf-8'))
        self.job = self.workflow['jobs']['windows-installer']
        self.steps = self.job['steps']

    def test_windows_job_requires_both_linux_gates(self):
        self.assertEqual(set(self.job['needs']), {'offline', 'offline-qt'})
        self.assertEqual(self.job['runs-on'], 'windows-2022')
        self.assertLessEqual(self.job['timeout-minutes'], 45)
        self.assertNotIn('continue-on-error', self.job)

    def test_actions_are_immutable_and_repository_permissions_are_read_only(self):
        self.assertEqual(self.workflow['permissions'], {'contents': 'read'})
        self.assertNotIn('permissions', self.job)
        for step in self.steps:
            with self.subTest(step=step.get('name', step.get('uses'))):
                self.assertNotIn('continue-on-error', step)
                if 'uses' in step:
                    self.assertRegex(step['uses'], r'^actions/[a-z-]+@[a-f0-9]{40}$')
                if step.get('uses', '').startswith('actions/checkout@'):
                    self.assertIs(step['with']['persist-credentials'], False)
        self.assertNotRegex(str(self.job), r'(?i)secrets\.|id-token|pull_request_target')

    def test_python_and_wheel_only_dependencies_are_pinned(self):
        setup = [step for step in self.steps
                 if step.get('uses', '').startswith('actions/setup-python@')]
        self.assertEqual(len(setup), 1)
        self.assertEqual(setup[0]['with']['python-version'], '3.12.14')
        self.assertEqual(setup[0]['with']['architecture'], 'x64')
        install = [step['run'] for step in self.steps
                   if 'python -m pip install' in step.get('run', '')]
        self.assertEqual(len(install), 1)
        command = install[0]
        for required in ('--index-url https://pypi.org/simple', '--require-hashes',
                         '--only-binary=:all:', '-r requirements/runtime-windows.lock',
                         '-r requirements/windows-build.lock'):
            self.assertIn(required, command)
        self.assertNotIn('--extra-index-url', command)
        self.assertNotIn('--trusted-host', command)

    def test_installer_is_uploaded_only_after_build_and_installed_app_test(self):
        build = next(index for index, step in enumerate(self.steps)
                     if 'python packaging/windows/build.py ' in step.get('run', ''))
        test = next(index for index, step in enumerate(self.steps)
                    if 'python packaging/windows/test_installer.py ' in step.get('run', ''))
        uploads = [(index, step) for index, step in enumerate(self.steps)
                   if step.get('uses', '').startswith('actions/upload-artifact@')]
        deliverables = [(index, step) for index, step in uploads
                        if 'dist/windows/installer/*.exe' in step['with']['path']]
        self.assertEqual(len(deliverables), 1)
        index, upload = deliverables[0]
        self.assertLess(build, test)
        self.assertLess(test, index)
        for step in (self.steps[build], self.steps[test], upload):
            self.assertIn(step.get('if', 'success()'), ('success()', '${{ success() }}'))
        self.assertIn('--installer dist/windows/installer/CacheCAD-Offline-Development-Setup.exe',
                      self.steps[test]['run'])
        self.assertEqual(upload['with']['if-no-files-found'], 'error')
        self.assertLessEqual(upload['with']['retention-days'], 30)
        self.assertIn('${{ github.sha }}', upload['with']['name'])

    def test_artifact_paths_cannot_sweep_up_sources_secrets_or_original_binaries(self):
        allowed = {'dist/windows/installer/*.exe', 'dist/windows/installer.sha256',
                   'dist/windows/build-provenance.json', 'dist/windows/build-logs/*.log',
                   'dist/windows/test-results/*.json', 'dist/windows/test-results/*.log',
                   'docs/WINDOWS_TRYOUT.md'}
        uploads = [step for step in self.steps
                   if step.get('uses', '').startswith('actions/upload-artifact@')]
        self.assertEqual(len(uploads), 2)
        for upload in uploads:
            paths = set(upload['with']['path'].splitlines())
            self.assertTrue(paths)
            self.assertTrue(paths <= allowed, paths - allowed)
            self.assertFalse(upload['with'].get('include-hidden-files', False))
            if upload.get('if') == 'failure()':
                self.assertNotIn('dist/windows/installer/*.exe', paths)
                self.assertLessEqual(upload['with']['retention-days'], 7)


@unittest.skipUnless(SOURCE_FIXTURE, 'Application sources absent in replacement wheel tests')
class WindowsSourceDistributionTests(unittest.TestCase):
    def test_packaging_and_locks_are_included_in_source_distribution(self):
        directives = (ROOT / 'MANIFEST.in').read_text(encoding='utf-8').splitlines()
        self.assertIn('recursive-include packaging/windows *.py *.spec *.iss *.md *.txt', directives)
        self.assertIn('recursive-include requirements *.txt *.lock *.in', directives)
        self.assertIn('recursive-include .github/workflows *.yml', directives)
        self.assertIn('recursive-include tests *.py', directives)
        self.assertIn('recursive-include docs *.md *.json *.txt', directives)

    def test_checked_in_text_is_kept_lf_for_source_hashes_on_windows(self):
        attributes = (ROOT / '.gitattributes').read_text(encoding='utf-8')
        self.assertRegex(attributes, r'(?m)^\*\s+text=auto\s+eol=lf\s*$')
        self.assertIn('include .gitattributes', (ROOT / 'MANIFEST.in').read_text(encoding='utf-8'))

    def test_windows_packaging_python_is_in_offline_syntax_and_lint_gate(self):
        tree = ast.parse((ROOT / 'tooling/check.py').read_text(encoding='utf-8'))
        values = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)]
        self.assertGreaterEqual(values.count('packaging/windows'), 2)

    def test_windows_dependency_locks_are_version_and_hash_pinned(self):
        pattern = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*==[^\s;]+(?:\s+--hash=sha256:[a-f0-9]{64})+$')
        for filename in ('runtime-windows.lock', 'windows-build.lock'):
            path = ROOT / 'requirements' / filename
            self.assertTrue(path.is_file(), filename)
            lines = [line.strip() for line in path.read_text(encoding='utf-8').splitlines()
                     if line.strip() and not line.lstrip().startswith('#')]
            self.assertTrue(lines, filename)
            packages = 0
            for line in lines:
                if line == '--only-binary :all:':
                    continue
                self.assertRegex(line, pattern, filename)
                packages += 1
            self.assertGreater(packages, 0)


@unittest.skipUnless(SOURCE_FIXTURE, 'Application sources absent in replacement wheel tests')
class WindowsPackagingSafetyTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / 'packaging/windows/support.py'
        self.assertTrue(path.is_file())
        spec = importlib.util.spec_from_file_location('_windows_support_test', path)
        self.support = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.support)
        self.temporary = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def fixture(self):
        root = self.temporary / 'application'
        root.mkdir()
        records = []
        for name in ('development/launch.py', 'development/resources_rc.py',
                     'development/build_resources.py', 'src/gui/main_window.py'):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"raise AssertionError('packaging must not execute this source')\n")
            records.append({'path': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        (root / 'assembly-manifest.json').write_text(json.dumps({'files': records}), encoding='utf-8')
        return root, records

    def test_payload_read_is_manifest_scoped_and_never_executes_source(self):
        root, records = self.fixture()
        (root / 'unlisted-original.bin').write_bytes(b'not approved')
        result = self.support.verified_payload(root)
        self.assertEqual(set(result), {'assembly-manifest.json'} | {record['path'] for record in records})
        self.assertNotIn('unlisted-original.bin', result)

    def test_payload_rejects_tampering_extra_source_and_bytecode(self):
        root, records = self.fixture()
        for name in ('extra.py', 'extra.pyc', 'extra.pyo'):
            path = root / name
            path.write_bytes(b'not approved')
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.support.verified_payload(root)
            path.unlink()
        (root / records[0]['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.support.verified_payload(root)

    def test_payload_rejects_unsafe_paths_and_duplicates(self):
        root, records = self.fixture()
        for name in ('../outside.py', '/absolute.py', 'C:/drive.py', r'src\escaped.py', ''):
            changed = copy.deepcopy(records)
            changed[0]['path'] = name
            (root / 'assembly-manifest.json').write_text(json.dumps({'files': changed}), encoding='utf-8')
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.support.verified_payload(root)
        (root / 'assembly-manifest.json').write_text(json.dumps({'files': records * 2}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.support.verified_payload(root)

    def test_source_dependency_discovery_parses_without_importing_application(self):
        root, _ = self.fixture()
        source = root / 'development/launch.py'
        source.write_text("import json, pathlib\nfrom email.message import Message\n"
                          "import google.auth\nfrom core import auth_manager\n"
                          "raise AssertionError('must not run')\n", encoding='utf-8')
        self.assertEqual(self.support.stdlib_hidden_imports([source]), ['email.message', 'json', 'pathlib'])

    def test_inno_version_must_match_reviewed_preinstalled_tool(self):
        self.assertEqual(self.support.inno_version('Inno Setup 6 Command-Line Compiler\n'
                                                  'Compiler engine version: 6.7.1\n'), '6.7.1')
        for text in ('Compiler engine version: 6.7.0', 'Inno Setup unknown', ''):
            with self.subTest(output=text), self.assertRaises(ValueError):
                self.support.inno_version(text)

    def test_installer_is_per_user_has_no_elevation_or_automatic_execution(self):
        recipe = (ROOT / 'packaging/windows/cachecad.iss').read_text(encoding='utf-8')
        active = '\n'.join(line for line in recipe.splitlines() if not line.lstrip().startswith(';'))
        settings = dict(re.findall(r'(?m)^([A-Za-z]+)=(.*)$', active))
        self.assertEqual(settings['PrivilegesRequired'], 'lowest')
        self.assertNotIn('PrivilegesRequiredOverridesAllowed', settings)
        self.assertEqual(settings['DefaultDirName'], r'{localappdata}\Programs\CacheCAD Offline Development')
        self.assertEqual(settings['Uninstallable'], 'yes')
        self.assertEqual(settings['CloseApplications'], 'no')
        self.assertEqual(settings['RestartApplications'], 'no')
        sections = re.findall(r'(?m)^\[([^\]]+)\]', active)
        self.assertEqual(sections, ['Setup', 'Files', 'Icons'])
        self.assertNotRegex(active.lower(), r'runonce|\\currentversion\\run|startup|download|hklm')

    def test_frozen_recipe_excludes_first_party_and_live_account_modules_from_pyz(self):
        spec = (ROOT / 'packaging/windows/cachecad.spec').read_text(encoding='utf-8')
        self.assertIn('verified_payload(source)', spec)
        self.assertIn('excludes=list(SOURCE_PACKAGES + LIVE_PACKAGES)', spec)
        self.assertIn('for name, *_ in a.pure:', spec)
        self.assertIn('noarchive=False', spec)
        self.assertIn('console=True', spec)
        self.assertIn('upx=False', spec)
        self.assertEqual(set(self.support.SOURCE_PACKAGES),
                         {'app', 'core', 'gui', 'utils', 'resources_rc', 'build_resources'})
        self.assertTrue({'google', 'googleapiclient', 'google_auth_oauthlib', 'requests'}
                        <= set(self.support.LIVE_PACKAGES))

    def smoke_record(self, scenario='success', platform='windows'):
        outcomes = {'success': {'cancelled': [], 'failed': [], 'succeeded': [1]},
                    'lifecycle': {'cancelled': [1], 'failed': [2], 'succeeded': [3]},
                    'close-pending': {'cancelled': [1], 'failed': [], 'succeeded': []}}
        return {'mode': 'offline-development', 'actual_gui': 'gui.main_window.MainWindow',
                'qt_platform': platform, 'synthetic_remote': True, 'active_threads': 0,
                'frozen': True, 'source_loader': 'verified-source',
                'errors': [], 'external_attempts': [], 'forbidden_modules_loaded': [],
                'credential_files': [], 'slot_errors': [], 'missing_placeholder_resources': [],
                'rendered_size': [1000, 700], 'outcomes': outcomes[scenario],
                'database_write_generations': {'success': [1], 'lifecycle': [3], 'close-pending': []}[scenario],
                'scan_ready': scenario != 'close-pending', 'closed_pending_scan': scenario == 'close-pending',
                'planned_paths': ['part.txt'], 'planned_uploads': 1, 'transfer_button_enabled': False}

    def test_installed_smoke_accepts_each_explicit_backend_and_scenario(self):
        for platform in ('windows', 'offscreen'):
            for scenario in ('success', 'lifecycle', 'close-pending'):
                record = self.smoke_record(scenario, platform)
                output = 'CACHECAD_OFFLINE_RESULT=' + json.dumps(record)
                with self.subTest(platform=platform, scenario=scenario):
                    self.assertEqual(self.support.validate_smoke(output, scenario, platform), record)

    def test_installed_smoke_rejects_wrong_app_unfrozen_run_and_safety_failures(self):
        bad_values = {'actual_gui': 'prototype.MainWindow', 'frozen': False,
                      'source_loader': 'pyz', 'qt_platform': 'offscreen',
                      'synthetic_remote': False, 'external_attempts': ['network'],
                      'credential_files': ['token.json'], 'forbidden_modules_loaded': ['core.auth_manager'],
                      'slot_errors': ['RuntimeError'], 'errors': ['failed'], 'active_threads': 1,
                      'missing_placeholder_resources': ['icon.png'], 'rendered_size': [0, 0],
                      'database_write_generations': [1, 2], 'transfer_button_enabled': True}
        for key, value in bad_values.items():
            record = self.smoke_record()
            record[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.support.validate_smoke('CACHECAD_OFFLINE_RESULT=' + json.dumps(record), 'success', 'windows')

    def test_installed_smoke_requires_one_result_record(self):
        valid = 'CACHECAD_OFFLINE_RESULT=' + json.dumps(self.smoke_record())
        for output in ('', '{}', valid + '\n' + valid):
            with self.subTest(output=output[:20]), self.assertRaises(ValueError):
                self.support.validate_smoke(output, 'success', 'windows')


if __name__ == '__main__':
    unittest.main()
