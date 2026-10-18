"""Portable checks for packaging contracts; these do not claim Windows execution."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
        for name in ('development/launch.py', 'development/resources_rc.py',
                     'development/build_resources.py', 'src/gui/main_window.py'):
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
                     'development/launch.py'):
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

    def test_inno_version_is_pinned_and_not_inferred_from_path(self):
        self.assertEqual(support.inno_version('Inno Setup 6 Command-Line Compiler\nCompiler engine version: Inno Setup 6.7.1\n'), '6.7.1')
        for text in ('Inno Setup 6.6.1', 'Inno Setup 6.7.2', 'C:/Inno Setup 6/ISCC.exe'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                support.inno_version(text)

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

    def test_recipe_is_per_user_without_run_or_registry_side_effects(self):
        text = (PACKAGING / 'cachecad.iss').read_text()
        active = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith(';'))
        self.assertIn('PrivilegesRequired=lowest', active)
        self.assertIn('DefaultDirName={localappdata}', active)
        self.assertIn('CloseApplications=no', active)
        self.assertIn('RestartApplications=no', active)
        self.assertIn('InfoBeforeFile=OFFLINE-README.txt', active)
        self.assertNotIn('PrivilegesRequiredOverridesAllowed', active)
        for section in ('[Run]', '[UninstallRun]', '[Registry]', '[Code]'):
            self.assertNotIn(section, active)
        self.assertIn('AppId={' + support.APP_ID, active)

    def test_spec_keeps_qt_libraries_and_source_separate(self):
        text = (PACKAGING / 'cachecad.spec').read_text()
        ast.parse(text)
        for required in ('SOURCE_PACKAGES + LIVE_PACKAGES', 'verified_payload(source)',
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
