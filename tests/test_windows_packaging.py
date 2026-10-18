"""Portable checks for packaging contracts; these do not claim Windows execution."""
import ast
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / 'packaging/windows'
support = None
if (PACKAGING / 'support.py').is_file():
    spec = importlib.util.spec_from_file_location('windows_packaging_support', PACKAGING / 'support.py')
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)


@unittest.skipUnless(support, 'Windows packaging sources absent from installed wheel fixture')
class WindowsPackagingTests(unittest.TestCase):
    def payload(self, root):
        files = []
        for name in support.REQUIRED_SOURCE:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# Reviewed test source.\n', encoding='utf-8')
            files.append({'path': name, 'sha256': support.sha256(path)})
        (root / 'assembly-manifest.json').write_text(json.dumps({'files': files}), encoding='utf-8')
        return files

    def smoke(self, scenario='success', platform='offscreen'):
        result = {'mode': 'offline-development', 'actual_gui': 'gui.main_window.MainWindow',
                  'qt_platform': platform, 'synthetic_remote': True, 'frozen': True,
                  'source_loader': 'verified-source', 'active_threads': 0,
                  'rendered_size': [1024, 768], 'errors': [], 'external_attempts': [],
                  'forbidden_modules_loaded': [], 'credential_files': [], 'slot_errors': [],
                  'missing_placeholder_resources': [], 'scan_ready': True,
                  'planned_paths': ['part.txt'], 'planned_uploads': 1,
                  'transfer_button_enabled': False,
                  'outcomes': {'cancelled': [], 'failed': [], 'succeeded': [1]},
                  'database_write_generations': [1]}
        if scenario == 'lifecycle':
            result.update(outcomes={'cancelled': [1], 'failed': [2], 'succeeded': [3]},
                          database_write_generations=[3])
        elif scenario == 'close-pending':
            result.update(outcomes={'cancelled': [1], 'failed': [], 'succeeded': []},
                          database_write_generations=[], closed_pending_scan=True, scan_ready=False)
        return result

    def encode(self, result):
        return 'An ordinary diagnostic line\nCACHECAD_OFFLINE_RESULT=' + json.dumps(result) + '\n'

    def test_verified_payload_is_manifest_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = self.payload(root)
            payload = support.verified_payload(root)
            self.assertEqual(set(payload), {'assembly-manifest.json', *(item['path'] for item in files)})
            (root / 'src/gui/main_window.py').write_text('# altered\n')
            with self.assertRaisesRegex(ValueError, 'hash/bytecode'):
                support.verified_payload(root)

    def test_unmanifested_source_and_stale_bytecode_are_rejected(self):
        for name in ('src/gui/extra.py', 'src/gui/__pycache__/main_window.pyc', 'src/gui/old.pyo'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.payload(root)
                extra = root / name
                extra.parent.mkdir(parents=True, exist_ok=True)
                extra.write_bytes(b'not reviewed')
                with self.assertRaisesRegex(ValueError, 'Unmanifested'):
                    support.verified_payload(root)

    def test_manifest_rejects_windows_and_posix_traversal_and_duplicates(self):
        for name in ('../outside.py', '/outside.py', r'..\outside.py', 'C:/outside.py',
                     'application/launch.py'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                files = self.payload(root)
                files.append({'path': name, 'sha256': '0' * 64})
                (root / 'assembly-manifest.json').write_text(json.dumps({'files': files}))
                with self.assertRaisesRegex(ValueError, 'Unsafe or duplicate'):
                    support.verified_payload(root)

    def test_import_discovery_collects_stdlib_but_never_application_or_live_accounts(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'sample.py'
            path.write_text('import csv, sqlite3\nfrom zoneinfo import ZoneInfo\n'
                            'from google.oauth2 import credentials\nfrom core.models import Item\n'
                            'from . import relative\n')
            self.assertEqual(support.stdlib_hidden_imports([path]), ['csv', 'sqlite3', 'zoneinfo'])

    def test_recovered_signal_type_and_real_sdk_are_bundled_with_source_only_app(self):
        source = ROOT / 'recovered/core/signal_manager.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        self.assertTrue(any(isinstance(node, ast.ImportFrom) and node.module == 'google.oauth2.credentials'
                            and any(alias.name == 'Credentials' for alias in node.names) for node in ast.walk(tree)))
        self.assertIn('google.oauth2.credentials', support.TYPE_IMPORTS)
        recipe = (PACKAGING / 'cachecad.spec').read_text()
        self.assertIn('hidden += list(TYPE_IMPORTS) + list(API_IMPORTS)', recipe)
        self.assertIn('locked_distributions(runtime_lock)', recipe)
        for name in ('google', 'google.auth', 'google.oauth2.credentials', 'google.auth.crypt.rsa'):
            self.assertFalse(support.excluded_frozen_module(name), name)
        for name in ('core.auth_manager', 'core.drive_client', 'application', 'development.launch'):
            self.assertTrue(support.excluded_frozen_module(name), name)
        for name in ('google.cloud', 'google.api_core.client_options', 'googleapiclient.discovery',
                     'google_auth_httplib2', 'httplib2'):
            self.assertFalse(support.excluded_frozen_module(name), name)
        self.assertIn('if excluded_frozen_module(name):', recipe)

    def test_inno_version_is_pinned_and_not_inferred_from_path(self):
        self.assertEqual(support.inno_version('Inno Setup 6 Command-Line Compiler\nCompiler engine version: Inno Setup 6.7.1\n'), '6.7.1')
        for text in ('Inno Setup 6.6.1', 'Inno Setup 6.7.2', 'C:/Inno Setup 6/ISCC.exe'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                support.inno_version(text)

    def test_pe_file_version_decodes_both_dwords_and_all_four_words(self):
        self.assertEqual(support.decode_file_version(0x00060007, 0x00010000), (6, 7, 1, 0))
        self.assertEqual(support.decode_file_version(0x1234abcd, 0x5678ffff),
                         (0x1234, 0xabcd, 0x5678, 0xffff))
        for bad in (-1, 2**32, '6', True, None):
            for pair in ((bad, 0), (0, bad)):
                with self.subTest(pair=pair), self.assertRaises(ValueError):
                    support.decode_file_version(*pair)

    def test_engine_banner_pin_is_exact_and_does_not_use_wrapper_metadata(self):
        banner = 'Compiler engine version: Inno Setup 6.7.1\r\n'
        self.assertEqual(support.inno_version(banner), '6.7.1')
        self.assertEqual(support.decode_file_version(0, 0), (0, 0, 0, 0))
        for text in (banner * 2, 'Inno Setup 6.7.1', 'FileVersion=6.7.1',
                     banner.replace('6.7.1', '6.7.0'), banner.replace('6.7.1', '6.7.2'),
                     banner.replace('6.7.1', '6.7.1.1'), banner.replace('6.7.1', '6.7.1-beta')):
            with self.subTest(text=text), self.assertRaises(ValueError):
                support.inno_version(text)

    def test_engine_version_probe_has_no_output_or_executable_sections(self):
        text = (PACKAGING / 'version-probe.iss').read_text()
        active = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith(';'))
        for directive in ('AppName=', 'AppVersion=', 'Output=no', 'CreateAppDir=no', 'Uninstallable=no'):
            self.assertIn(directive, active)
        self.assertEqual([line for line in active.splitlines() if line.startswith('[')], ['[Setup]'])
        build = (PACKAGING / 'build.py').read_text()
        self.assertIn("run_logged([iscc, ROOT / 'packaging/windows/version-probe.iss']", build)
        self.assertIn("'iscmplr_sha256': sha256(engine)", build)

    def test_pe_reader_uses_static_resource_table_and_closes_file(self):
        executable = MagicMock()
        executable.VS_FIXEDFILEINFO = [SimpleNamespace(Signature=0xfeef04bd,
                                                       FileVersionMS=0x00060007,
                                                       FileVersionLS=0x00010000)]
        factory = MagicMock()
        factory.return_value.__enter__.return_value = executable
        module = SimpleNamespace(PE=factory, DIRECTORY_ENTRY={'IMAGE_DIRECTORY_ENTRY_RESOURCE': 2})
        with patch.dict(sys.modules, {'pefile': module}):
            self.assertEqual(support.pe_file_version(Path('ISCC.exe')), (6, 7, 1, 0))
        factory.assert_called_once_with('ISCC.exe', fast_load=True)
        executable.parse_data_directories.assert_called_once_with(directories=[2])
        factory.return_value.__exit__.assert_called_once()

    def test_pe_reader_rejects_absent_ambiguous_or_invalid_resource(self):
        valid = SimpleNamespace(Signature=0xfeef04bd, FileVersionMS=0x00060007, FileVersionLS=0x00010000)
        for records in ([], [valid, valid], [SimpleNamespace(Signature=0)]):
            executable = MagicMock()
            executable.VS_FIXEDFILEINFO = records
            factory = MagicMock()
            factory.return_value.__enter__.return_value = executable
            module = SimpleNamespace(PE=factory, DIRECTORY_ENTRY={'IMAGE_DIRECTORY_ENTRY_RESOURCE': 2})
            with patch.dict(sys.modules, {'pefile': module}), self.assertRaises(ValueError):
                support.pe_file_version(Path('ISCC.exe'))

    def test_compiler_discovery_rejects_path_shim_and_prefers_full_installation(self):
        spec = importlib.util.spec_from_file_location('windows_packaging_build', PACKAGING / 'build.py')
        build = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'support': support}):
            spec.loader.exec_module(build)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shim = root / 'shim/ISCC.exe'
            real = root / 'programs/Inno Setup 6/ISCC.exe'
            for path in (shim, real):
                path.parent.mkdir(parents=True)
                path.write_bytes(b'fixture')
            (real.parent / 'ISCmplr.dll').write_bytes(b'fixture engine')
            with patch.dict(build.os.environ, {'ProgramFiles(x86)': str(root / 'programs'), 'ProgramFiles': str(root / 'other')}), patch.object(build.shutil, 'which', return_value=str(shim)):
                self.assertEqual(build.find_iscc(), real.resolve())
                with self.assertRaises(FileNotFoundError):
                    build.find_iscc(shim)
                (real.parent / 'ISCmplr.dll').unlink()
                with self.assertRaises(FileNotFoundError):
                    build.find_iscc()

    def test_failed_build_prints_only_last_sixty_diagnostic_lines(self):
        spec = importlib.util.spec_from_file_location('windows_packaging_build', PACKAGING / 'build.py')
        build = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'support': support}):
            spec.loader.exec_module(build)
        def fail(command, **kwargs):
            kwargs['stdout'].write('\n'.join(f'diagnostic-{number}' for number in range(100)))
            return SimpleNamespace(returncode=2)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(build.subprocess, 'run', side_effect=fail), patch.object(sys, 'stderr', new_callable=io.StringIO) as stderr:
                with self.assertRaisesRegex(RuntimeError, 'failed \\(2\\)'):
                    build.run_logged(['compiler'], Path(temporary) / 'build.log')
            diagnostic = stderr.getvalue()
            self.assertIn('Last 60 lines', diagnostic)
            self.assertIn('diagnostic-40\n', diagnostic)
            self.assertIn('diagnostic-99\n', diagnostic)
            self.assertNotIn('diagnostic-39\n', diagnostic)

    def test_all_installed_runtime_scenarios_require_real_gui_and_platform(self):
        for scenario in ('success', 'lifecycle', 'close-pending'):
            for platform in ('offscreen', 'windows'):
                result = self.smoke(scenario, platform)
                self.assertEqual(support.validate_smoke(self.encode(result), scenario, platform), result)

    def test_smoke_rejects_source_python_wrong_platform_and_missing_safety_fields(self):
        mutations = {'frozen': False, 'source_loader': 'frozen-bytecode', 'qt_platform': 'offscreen',
                     'actual_gui': 'dummy.MainWindow', 'external_attempts': ['socket.connect'],
                     'active_threads': 1, 'transfer_button_enabled': True, 'rendered_size': [0, 0],
                     'database_write_generations': [1, 2]}
        for field, value in mutations.items():
            result = self.smoke(platform='windows')
            result[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                support.validate_smoke(self.encode(result), 'success', 'windows')
        result = self.smoke()
        del result['credential_files']
        with self.assertRaises(ValueError):
            support.validate_smoke(self.encode(result), 'success', 'offscreen')

    def test_missing_duplicate_or_invalid_json_result_is_rejected(self):
        for text in ('', self.encode(self.smoke()) * 2, 'CACHECAD_OFFLINE_RESULT=no-json'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                support.validate_smoke(text, 'success', 'offscreen')

    def test_recipe_is_per_user_without_automatic_run_or_unrelated_registry_writes(self):
        text = (PACKAGING / 'cachecad.iss').read_text()
        active = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith(';'))
        self.assertIn('PrivilegesRequired=lowest', active)
        self.assertIn('DefaultDirName={localappdata}', active)
        self.assertIn('CloseApplications=no', active)
        self.assertIn('RestartApplications=no', active)
        self.assertIn('InfoBeforeFile=APPLICATION-README.txt', active)
        self.assertNotIn('PrivilegesRequiredOverridesAllowed', active)
        for section in ('[Run]', '[UninstallRun]', '[UninstallDelete]'):
            self.assertNotIn(section, active)
        self.assertIn('AppId={' + support.APP_ID, active)
        deletion = active.split('[InstallDelete]', 1)[1].split('[Files]', 1)[0]
        self.assertEqual([line for line in deletion.splitlines() if line.strip()],
                         [r'Type: filesandordirs; Name: "{app}\_internal\app-source"'])
        registry = active.split('[Registry]', 1)[1].split('[Code]', 1)[0]
        self.assertTrue(all(line.startswith('Root: HKCU64; Subkey: "{#IdentityKey}";')
                            for line in registry.splitlines() if line.strip()))

    def test_spec_keeps_qt_libraries_and_source_separate(self):
        text = (PACKAGING / 'cachecad.spec').read_text()
        ast.parse(text)
        for required in ('SOURCE_PACKAGES', 'verified_payload(source)',
                         'console=True', 'upx=False', "'PySide6.QtNetwork'", 'copy_metadata(package)',
                         "'third-party-notices/Python'", 'COLLECT('):
            self.assertIn(required, text)

    def test_command_line_help_is_portable_and_syntax_valid(self):
        for name in ('build.py', 'test_installer.py'):
            completed = subprocess.run([sys.executable, '-B', str(PACKAGING / name), '--help'],
                                       capture_output=True, text=True, timeout=15)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn('usage:', completed.stdout)


if __name__ == '__main__':
    unittest.main()
