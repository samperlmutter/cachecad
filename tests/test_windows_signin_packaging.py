"""Production sign-in packaging and retained source-only offline bootstrap tests."""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / 'packaging/windows'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless((PACKAGING / 'support.py').is_file(), 'Packaging absent in replacement wheel')
class OfflineBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.support = load('_signin_packaging_support', PACKAGING / 'support.py')
        cls.entrypoint = load('_signin_bootstrap', ROOT / 'development/entrypoint.py')

    def fixture(self, root):
        files = {
            'development/launch.py': (
                b"import json, sys\nmarker = 'verified-offline'\n"
                b"def main():\n    print(json.dumps({'mode': 'offline', 'argv': sys.argv[1:]}))\n"
            ),
            'development/signin.py': (
                b"from development import launch\nimport json, sys\n"
                b"def main():\n    print(json.dumps({'mode': 'signin', 'argv': sys.argv[1:], "
                b"'offline_marker': launch.marker}))\n"
            ),
        }
        records = []
        for name, raw in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            records.append({'path': name, 'sha256': hashlib.sha256(raw).hexdigest()})
        (root / 'assembly-manifest.json').write_text(json.dumps({'files': records}), encoding='utf-8')
        return records

    def dispatch(self, root, args):
        program = """
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('entrypoint', sys.argv[1])
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)
sys.frozen = True
sys._MEIPASS = sys.argv[2]
raise SystemExit(entrypoint.main(sys.argv[3:]))
"""
        return subprocess.run([sys.executable, '-B', '-c', program,
                               str(ROOT / 'development/entrypoint.py'), str(root), *args],
                              capture_output=True, text=True, timeout=15)

    def test_bootstrap_defaults_offline_and_only_exact_flag_selects_signin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root / 'app-source')
            for args, expected in ((['--smoke'], 'offline'),
                                   (['--signin', '--smoke'], 'signin'),
                                   (['--sign', '--smoke'], 'offline')):
                process = self.dispatch(root, args)
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                result = json.loads(process.stdout)
                self.assertEqual(result['mode'], expected)
                self.assertNotIn('--signin', result['argv'])
                self.assertEqual(result['argv'], [arg for arg in args if arg != '--signin'])
                if expected == 'signin':
                    self.assertEqual(result['offline_marker'], 'verified-offline')

    def test_default_does_not_execute_signin_module(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = self.fixture(root / 'app-source')
            path = root / 'app-source/development/signin.py'
            path.write_bytes(b"raise AssertionError('signin imported without explicit mode')\n")
            records[1]['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            (root / 'app-source/assembly-manifest.json').write_text(json.dumps({'files': records}))
            process = self.dispatch(root, [])
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertEqual(json.loads(process.stdout)['mode'], 'offline')

    def test_frozen_bootstrap_rejects_changed_and_unmanifested_launcher(self):
        for name in ('launch', 'signin'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.fixture(root / 'app-source')
                (root / f'app-source/development/{name}.py').write_text("raise AssertionError('unreviewed')\n")
                process = self.dispatch(root, ['--signin'])
                self.assertNotEqual(process.returncode, 0)
                self.assertIn('Launcher source differs', process.stderr)
                self.assertNotIn('AssertionError: unreviewed', process.stderr)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = self.fixture(root)
            (root / 'assembly-manifest.json').write_text(json.dumps({'files': records[:1]}))
            with self.assertRaisesRegex(ValueError, 'differs'):
                self.entrypoint.launcher_sources(root, verify=True)

    def test_frozen_bootstrap_rejects_duplicate_manifest_and_linked_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = self.fixture(root)
            (root / 'assembly-manifest.json').write_text(json.dumps({'files': records * 2}))
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                self.entrypoint.launcher_sources(root, verify=True)
        if os.name == 'nt':
            return  # Windows link creation can require an unrelated system privilege.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            source = root / 'development/launch.py'
            target = root / 'outside.py'
            source.rename(target)
            source.symlink_to(target)
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                self.entrypoint.launcher_sources(root, verify=True)

    def test_bootstrap_snapshots_ignore_adjacent_bytecode_and_later_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            (root / 'development/signin.pyc').write_bytes(b'untrusted bytecode')
            sources = self.entrypoint.launcher_sources(root, verify=True)
            original = sources['signin'][1]
            (root / 'development/signin.py').write_bytes(b'untrusted later source')
            self.assertEqual(sources['signin'][1], original)
            self.assertIn(b'from development import launch', original)


@unittest.skipUnless((PACKAGING / 'support.py').is_file(), 'Packaging absent in replacement wheel')
class ApplicationPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.support = load('_application_packaging_support', PACKAGING / 'support.py')

    def result(self):
        return {'mode': 'application', 'actual_gui': 'gui.main_window.MainWindow',
                'qt_platform': 'windows', 'frozen': True, 'source_loader': 'verified-source',
                'signed_in': False, 'has_credentials': False, 'drive_client_created': False,
                'network_disabled': True, 'active_threads': 0, 'setup_required': True,
                'sdk_imports_verified': True, 'certificate_bundle_present': True,
                'drive_discovery_present': True, 'forbidden_modules_loaded': [],
                'errors': [], 'external_attempts': [], 'development_modules_loaded': [],
                'credential_files': [], 'rendered_size': [1000, 700]}

    def encode(self, result):
        return 'diagnostic\nCACHECAD_APPLICATION_RESULT=' + json.dumps(result) + '\n'

    def client(self):
        # Public synthetic metadata, never a real registered client or credential.
        return {'installed': {'client_id': 'synthetic-unregistered.apps.googleusercontent.com',
                              'auth_uri': 'https://accounts.google.com/o/oauth2/v2/auth',
                              'token_uri': 'https://oauth2.googleapis.com/token',
                              'redirect_uris': ['http://localhost']}}

    def payload(self, root):
        files = []
        for name in self.support.REQUIRED_SOURCE:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# Reviewed synthetic source fixture.\n', encoding='utf-8')
            files.append({'path': name, 'sha256': self.support.sha256(path)})
        (root / 'assembly-manifest.json').write_text(json.dumps({'files': files}), encoding='utf-8')
        return files

    def test_real_google_sdk_is_bundled_and_first_party_is_source_only(self):
        for name in ('google_auth_oauthlib.flow', 'google.oauth2.id_token',
                     'google.auth.transport.requests', 'googleapiclient.discovery',
                     'google_auth_httplib2', 'httplib2', 'requests', 'certifi'):
            self.assertFalse(self.support.excluded_frozen_module(name), name)
        for name in ('application', 'development', 'development.signin', 'core.auth_manager',
                     'core.drive_client', 'utils.windows_token_store'):
            self.assertTrue(self.support.excluded_frozen_module(name), name)
        recipe = (PACKAGING / 'cachecad.spec').read_text()
        self.assertIn("[str(source / 'application' / 'launch.py')]", recipe)
        self.assertIn('list(API_IMPORTS)', recipe)
        self.assertIn("collect_data_files('certifi')", recipe)
        self.assertIn('discovery_cache/documents/drive.v3.json', recipe)
        self.assertIn("('pyinstaller',) + locked_distributions(runtime_lock):", recipe)
        lock = (ROOT / 'requirements/runtime-windows.lock').read_text().lower()
        for package in self.support.API_DISTRIBUTIONS:
            self.assertIn(package.lower() + '==', lock)

    def test_complete_runtime_dependency_metadata_is_preserved(self):
        packages = self.support.locked_distributions(ROOT / 'requirements/runtime-windows.lock')
        self.assertTrue(set(self.support.API_DISTRIBUTIONS) <= set(packages))
        self.assertIn('grpcio', packages)
        self.assertIn('protobuf', packages)
        self.assertIn('pyside6', packages)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'runtime.lock'
            for content in ('', 'requests>=2', 'requests==2 --hash=sha256:bad',
                            'a==1 --hash=sha256:' + '0' * 64 + '\na==1 --hash=sha256:' + '0' * 64):
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    self.support.locked_distributions(path)

    def test_installer_has_one_real_default_and_separate_installation_identity(self):
        recipe = (PACKAGING / 'cachecad.iss').read_text()
        shortcut = next(line for line in recipe.splitlines()
                        if line.startswith('Name: "{group}\\CacheCAD";'))
        self.assertNotIn('Parameters:', shortcut)
        self.assertIn('Filename: "{app}\\CacheCAD.exe"', shortcut)
        self.assertNotIn('--signin', recipe)
        self.assertNotIn('signinshortcut', recipe)
        self.assertNotIn('4A03D312-83C5-4EC3-9C46-8CE43DA792EA', recipe)
        self.assertEqual(self.support.EXE_NAME, 'CacheCAD.exe')
        self.assertEqual(self.support.INSTALLER_NAME, 'CacheCAD-Setup.exe')

    def test_application_report_requires_actual_installed_signed_out_gui(self):
        for platform in ('windows', 'offscreen'):
            result = self.result()
            result['qt_platform'] = platform
            self.assertEqual(self.support.validate_application_smoke(self.encode(result), platform), result)

    def test_application_report_rejects_every_missing_field(self):
        original = self.result()
        for key in original:
            result = dict(original)
            result.pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError):
                self.support.validate_application_smoke(self.encode(result), 'windows')

    def test_application_report_rejects_wrong_types_and_unsafe_states(self):
        for key, value in {'mode': 'offline-development', 'frozen': False, 'signed_in': True,
                           'has_credentials': True, 'network_disabled': False, 'active_threads': True,
                           'drive_client_created': True, 'setup_required': False,
                           'qt_platform': 'offscreen', 'rendered_size': [False, 800],
                           'credential_files': ['google-session.dpapi'],
                           'development_modules_loaded': ['development.launch'],
                           'external_attempts': ['socket.connect']}.items():
            result = dict(self.result(), **{key: value})
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.support.validate_application_smoke(self.encode(result), 'windows')

    def test_application_report_requires_exactly_one_object_record(self):
        for stdout in ('', self.encode(self.result()) * 2, 'CACHECAD_APPLICATION_RESULT=not-json',
                       'CACHECAD_APPLICATION_RESULT=[]', 'CACHECAD_APPLICATION_RESULT=null'):
            with self.subTest(stdout=stdout), self.assertRaises(ValueError):
                self.support.validate_application_smoke(stdout, 'windows')

    def test_installer_checks_native_startup_without_live_authentication(self):
        source = (PACKAGING / 'test_installer.py').read_text()
        ast.parse(source)
        self.assertIn("for qt_platform in ('offscreen', 'windows'):", source)
        self.assertIn("[executable, '--startup-check', '--profile-dir', profile", source)
        self.assertIn("'--client-config', missing_config", source)
        self.assertIn('validate_application_smoke(process.stdout, qt_platform)', source)
        self.assertIn("summary['signed_out_startup_verified'] = True", source)
        self.assertIn("'live_authentication_verified': False", source)
        self.assertLess(source.index('validate_application_smoke(process.stdout, qt_platform)'),
                        source.index("summary['installed_runtime_verified'] = True"))
        self.assertNotIn('--signin', source)
        self.assertNotIn('--scenario', source)

    def test_installed_environment_isolates_profile_and_strips_inherited_google_settings(self):
        with patch.dict(sys.modules, {'support': self.support}):
            installer = load('_application_installer_test', PACKAGING / 'test_installer.py')
        variables = {'SystemRoot': 'C:/Windows', 'PythonPath': 'untrusted',
                     'Google_Application_Credentials': 'untrusted',
                     'OAUTHLIB_INSECURE_TRANSPORT': '1', 'REQUESTS_CA_BUNDLE': 'untrusted',
                     'CURL_CA_BUNDLE': 'untrusted', 'SSL_CERT_FILE': 'untrusted',
                     'HTTPS_PROXY': 'untrusted', 'CACHECAD_CLIENT': 'untrusted',
                     'LOCALAPPDATA': '/real-profile', 'APPDATA': '/real-profile'}
        with patch.dict(os.environ, variables, clear=True):
            environment = installer.installed_environment(Path('/disposable-profile'))
        self.assertEqual(set(environment), {'SYSTEMROOT', 'PATH', 'TMP', 'TEMP', 'TMPDIR',
                                           'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA'})
        self.assertEqual(environment['TEMP'], '/disposable-profile')
        self.assertEqual(environment['LOCALAPPDATA'], '/disposable-profile/Local')
        self.assertNotIn('/real-profile', str(environment))

    def test_shortcut_verification_reads_without_running_or_saving_shortcut(self):
        with patch.dict(sys.modules, {'support': self.support}):
            installer = load('_application_shortcut_test', PACKAGING / 'test_installer.py')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / 'CacheCAD.exe'
            result = {'target': str(executable), 'arguments': '', 'working_directory': str(root)}
            with patch.object(installer, 'run_capture', return_value=SimpleNamespace(stdout=json.dumps(result))) as run:
                self.assertEqual(installer.verify_shortcut(
                    root / 'app.lnk', executable, '', root, 'shortcut',
                    {'SYSTEMROOT': str(root)}, root), result)
                command = run.call_args.args[0]
                self.assertTrue(str(command[0]).endswith('powershell.exe'))
                self.assertNotIn('.Save', command[-1])
                self.assertNotIn('Start-Process', command[-1])
                self.assertNotIn(str(executable), command[-1])
                with self.assertRaisesRegex(RuntimeError, 'unexpected target'):
                    installer.verify_shortcut(root / 'app.lnk', executable, '--signin', root, 'shortcut',
                                              {'SYSTEMROOT': str(root)}, root)

    def test_provenance_records_real_application_without_claiming_live_auth(self):
        source = (PACKAGING / 'build.py').read_text()
        ast.parse(source)
        for expected in ("'application/manifest.json'", "'application/launch.py'",
                         "'google_sdk'", "'distributions'", "'default': 'application'",
                         "'required_installed_smokes': {'signed_out_application': 2}",
                         "'live_authentication_verified': False", "'--production'",
                         "'owner_client_configuration'", "'sanitized_sha256'"):
            self.assertIn(expected, source)
        self.assertNotIn("'development/", source)
        notice = (PACKAGING / 'APPLICATION-README.txt').read_text()
        self.assertIn('network disabled', notice)
        self.assertIn('Windows user-bound DPAPI', notice)
        self.assertIn('End users do not create their own OAuth project', notice)

    def test_payload_rejects_manifested_development_and_test_code(self):
        for name in ('development/launch.py', 'development/signin.py', 'tests/fake_remote.py'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                records = self.payload(root)
                path = root / name
                path.parent.mkdir(parents=True)
                path.write_text('# not production\n', encoding='utf-8')
                records.append({'path': name, 'sha256': self.support.sha256(path)})
                (root / 'assembly-manifest.json').write_text(json.dumps({'files': records}))
                with self.assertRaisesRegex(ValueError, 'Development/test'):
                    self.support.verified_payload(root)

    def test_payload_rejects_offline_origin_even_with_neutral_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = self.payload(root)
            records[0]['origin'] = 'offline-development'
            (root / 'assembly-manifest.json').write_text(json.dumps({'files': records}))
            with self.assertRaisesRegex(ValueError, 'Offline fixture'):
                self.support.verified_payload(root)

    def test_owner_desktop_metadata_is_optional_and_sanitized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / 'owner.json'
            config = self.client()
            config['installed'].update(project_id='public-project', client_secret='public-desktop-value')
            selected.write_text(json.dumps(config))
            output = json.loads(self.support.client_config_bytes(selected))
            self.assertNotIn('project_id', output['installed'])
            self.assertEqual(output['installed']['client_secret'], 'public-desktop-value')
            config['installed'].pop('client_secret')
            selected.write_text(json.dumps(config))
            self.assertEqual(json.loads(self.support.client_config_bytes(selected))['installed']['client_secret'], '')

    def test_owner_configuration_rejects_tokens_service_accounts_web_clients_and_untrusted_endpoints(self):
        cases = [{'refresh_token': 'synthetic-invalid'}, {'type': 'service_account'},
                 {'web': self.client()['installed']}, {'installed': dict(self.client()['installed'], refresh_token='synthetic-invalid')}]
        for key, value in (('auth_uri', 'https://example.invalid/auth'),
                           ('token_uri', 'http://oauth2.googleapis.com/token'),
                           ('client_id', 'not-a-client'), ('client_secret', 'line\nbreak'),
                           ('redirect_uris', ['http://localhost.attacker.invalid']),
                           ('redirect_uris', ['http://user@localhost']),
                           ('redirect_uris', ['http://localhost/callback?token=x']),
                           ('redirect_uris', ['http://localhost:99999']),
                           ('auth_provider_x509_cert_url', 'https://example.invalid/certs')):
            config = self.client()
            config['installed'][key] = value
            cases.append(config)
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary) / 'owner.json'
            for config in cases:
                selected.write_text(json.dumps(config))
                with self.subTest(config=config), self.assertRaises(ValueError):
                    self.support.client_config_bytes(selected)

    def test_owner_configuration_rejects_duplicate_oversized_or_non_json_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary) / 'owner.json'
            for raw in (b'{"installed":{},"installed":{}}', b' ' * 65537, b'not-json', b'\xff'):
                selected.write_bytes(raw)
                with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                    self.support.client_config_bytes(selected)
            with self.assertRaises(ValueError):
                self.support.client_config_bytes(Path(temporary))

    def test_owner_configuration_is_hashed_in_source_manifest_but_source_path_is_not_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / 'app'
            app.mkdir()
            self.payload(app)
            selected = root / 'private-owner-path.json'
            selected.write_text(json.dumps(self.client()))
            digest = self.support.add_owner_client_config(app, selected)
            payload = self.support.verified_payload(app)
            name = 'config/google-desktop-client.json'
            self.assertIn(name, payload)
            self.assertEqual(self.support.sha256(payload[name]), digest)
            manifest = json.loads((app / 'assembly-manifest.json').read_text())
            self.assertTrue(manifest['owner_desktop_client_configured'])
            self.assertNotIn('private-owner-path', json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'already exists'):
                self.support.add_owner_client_config(app, selected)


if __name__ == '__main__':
    unittest.main()
