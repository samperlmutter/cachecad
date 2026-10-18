"""Source-only tests of the Windows/frozen launcher contract; never load Qt."""
from contextlib import redirect_stderr
import hashlib
import importlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless((ROOT / 'development/launch.py').is_file(),
                     'Application development sources absent in replacement wheel tests')
class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('_windows_launcher_test', ROOT / 'development/launch.py')
        self.launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.launcher)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def assembly(self, payload=None):
        if payload is None:
            payload = {'src/launcher_fixture/module.py': b'value = 42\n'}
        entries = []
        for name, raw in payload.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            entries.append({'path': name, 'sha256': hashlib.sha256(raw).hexdigest()})
        (self.root / 'assembly-manifest.json').write_text(
            json.dumps({'files': entries}), encoding='utf-8')
        return self.launcher.verify_source(self.root)

    def source_importer(self, sources):
        self.enterContext(patch.object(sys, 'meta_path', list(sys.meta_path)))
        self.enterContext(patch.dict(sys.modules))
        for name in list(sys.modules):
            if name == 'launcher_fixture' or name.startswith('launcher_fixture.'):
                del sys.modules[name]
        return self.launcher.install_source_importer(self.root, sources)

    def test_source_run_requires_explicit_assembly(self):
        with patch.object(sys, 'frozen', False, create=True), redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as error:
                self.launcher.parse_arguments([])
        self.assertEqual(error.exception.code, 2)
        self.assertIn('--app-source is required', stderr.getvalue())

    def test_source_run_accepts_explicit_assembly(self):
        with patch.object(sys, 'frozen', False, create=True):
            args = self.launcher.parse_arguments(['--app-source', str(self.root), '--smoke'])
        self.assertEqual(args.app_source, self.root)
        self.assertTrue(args.smoke)
        self.assertIsNone(args.platform)

    def test_frozen_run_uses_meipass_bundle(self):
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, '_MEIPASS', str(self.root), create=True):
            args = self.launcher.parse_arguments(['--smoke'])
        self.assertEqual(args.app_source, self.root / 'app-source')

    def test_frozen_run_preserves_explicit_override(self):
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, '_MEIPASS', '/unused', create=True):
            args = self.launcher.parse_arguments(['--app-source', str(self.root)])
        self.assertEqual(args.app_source, self.root)

    def test_native_windows_backend_is_explicit(self):
        with patch.object(sys, 'platform', 'win32'):
            args = self.launcher.parse_arguments(['--app-source', str(self.root), '--smoke', '--platform', 'windows'])
        self.assertEqual(args.platform, 'windows')

    def test_windows_backend_rejected_on_other_platforms(self):
        with patch.object(sys, 'platform', 'linux'), redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                self.launcher.parse_arguments(['--app-source', str(self.root), '--platform', 'windows'])
        self.assertIn('requires Windows', stderr.getvalue())

    def test_finder_precedes_frozen_and_normal_importers(self):
        finder = self.source_importer(self.assembly())
        self.assertIs(sys.meta_path[0], finder)
        module = importlib.import_module('launcher_fixture.module')
        self.assertEqual(module.value, 42)
        self.assertEqual(Path(module.__file__), self.root / 'src/launcher_fixture/module.py')
        self.assertIsNone(finder.find_spec('unrelated_third_party'))
        self.assertEqual(list((self.root / 'src').rglob('*.pyc')), [])

    def test_importer_does_not_capture_standard_or_third_party_roots(self):
        finder = self.source_importer(self.assembly())
        for name in ('json', 'logging', 'pathlib', 'hashlib', 'PySide6.QtCore', 'sqlite3'):
            with self.subTest(name=name):
                self.assertIsNone(finder.find_spec(name))

    def test_forbidden_import_is_blocked_ahead_of_verified_source_loader(self):
        self.assembly({'src/core/auth_manager.py': b"raise AssertionError('live body executed')\n"})
        profile = self.root / 'profile'
        profile.mkdir()
        program = """
import importlib.util, json, os, pathlib, sys
spec = importlib.util.spec_from_file_location('fixture_launcher', sys.argv[1])
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
root, profile = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
sys.dont_write_bytecode = True
finder = launcher.install_source_importer(root, launcher.verify_source(root))
os.chdir(profile)
attempts, _ = launcher.install_tripwires(profile)
assert sys.meta_path.index(finder) == 1
try:
    import core.auth_manager
except RuntimeError as error:
    assert 'Live account' in str(error)
else:
    raise AssertionError('Forbidden import allowed')
assert 'core.auth_manager' not in sys.modules
print(json.dumps(attempts))
"""
        completed = subprocess.run(
            [sys.executable, '-B', '-c', program, str(ROOT / 'development/launch.py'),
             str(self.root), str(profile)], capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(json.loads(completed.stdout), ['live-module:core.auth_manager'])

    def test_verified_bytes_ignore_stale_bytecode(self):
        sources = self.assembly()
        source = self.root / 'src/launcher_fixture/module.py'
        source.write_bytes(b'value = -1\n')
        py_compile.compile(str(source), doraise=True)
        source.write_bytes(sources['src/launcher_fixture/module.py'])
        self.source_importer(sources)
        self.assertEqual(importlib.import_module('launcher_fixture.module').value, 42)

    def test_import_executes_verified_snapshot_after_source_changes(self):
        sources = self.assembly()
        (self.root / 'src/launcher_fixture/module.py').write_bytes(b'value = -1\n')
        self.source_importer(sources)
        self.assertEqual(importlib.import_module('launcher_fixture.module').value, 42)

    def test_unmanifested_bytecode_cannot_load_within_application_namespace(self):
        sources = self.assembly()
        foreign = self.root / 'src/launcher_fixture/foreign.py'
        foreign.write_bytes(b"raise AssertionError('unreviewed code executed')\n")
        py_compile.compile(str(foreign), cfile=str(foreign.with_suffix('.pyc')), doraise=True)
        foreign.unlink()
        self.source_importer(sources)
        with self.assertRaisesRegex(ModuleNotFoundError, 'Unmanifested application import'):
            importlib.import_module('launcher_fixture.foreign')
        self.assertNotIn('launcher_fixture.foreign', sys.modules)

    def test_regular_packages_and_relative_imports_use_verified_source(self):
        sources = self.assembly({
            'src/launcher_fixture/__init__.py': b'from .module import value\n',
            'src/launcher_fixture/module.py': b'value = 42\n',
        })
        self.source_importer(sources)
        self.assertEqual(importlib.import_module('launcher_fixture').value, 42)

    def test_development_resources_use_manifested_source(self):
        sources = self.assembly({
            'src/launcher_fixture/module.py': b'value = 42\n',
            'development/resources_rc.py': b'value = "authored placeholder"\n',
            'development/build_resources.py': b'ALIASES = ("placeholder.png",)\n',
        })
        self.source_importer(sources)
        self.assertEqual(importlib.import_module('resources_rc').value, 'authored placeholder')
        self.assertEqual(importlib.import_module('build_resources').ALIASES, ('placeholder.png',))

    def test_unmanifested_development_helpers_are_rejected(self):
        self.source_importer(self.assembly())
        for module in ('resources_rc', 'build_resources'):
            with self.subTest(module=module), self.assertRaisesRegex(ModuleNotFoundError, 'Unmanifested'):
                importlib.import_module(module)

    def test_preloaded_application_module_is_rejected(self):
        sources = self.assembly()
        with patch.dict(sys.modules, {'launcher_fixture': types.ModuleType('launcher_fixture')}):
            with self.assertRaisesRegex(RuntimeError, 'before source verification'):
                self.launcher.install_source_importer(self.root, sources)

    def test_windows_drive_ads_and_separator_paths_are_rejected(self):
        for name in ('C:relative.py', 'C:/absolute.py', r'src\escaped.py', 'src/mod.py:stream', '../escaped.py'):
            with self.subTest(name=name):
                (self.root / 'assembly-manifest.json').write_text(
                    json.dumps({'files': [{'path': name, 'sha256': 'unused'}]}), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'Unsafe'):
                    self.launcher.verify_source(self.root)

    def test_duplicate_manifest_paths_are_rejected(self):
        self.assembly()
        path = self.root / 'assembly-manifest.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest['files'].append(manifest['files'][0])
        path.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.launcher.verify_source(self.root)

    def test_profile_redirects_windows_and_cached_python_temp_directories(self):
        old_cwd, old_tempdir, old_environment = Path.cwd(), tempfile.tempdir, dict(os.environ)
        with self.launcher.disposable_profile() as profile:
            self.assertEqual(Path.cwd(), profile)
            for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP'):
                self.assertEqual(Path(os.environ[key]), profile)
            self.assertEqual(Path(tempfile.gettempdir()), profile)
            with tempfile.NamedTemporaryFile() as fixture:
                self.assertTrue(Path(fixture.name).is_relative_to(profile))
            self.assertTrue((profile / 'runtime').is_dir())
        self.assertFalse(profile.exists())
        self.assertEqual(Path.cwd(), old_cwd)
        self.assertEqual(tempfile.tempdir, old_tempdir)
        self.assertEqual(dict(os.environ), old_environment)

    def test_profile_closes_logging_files_before_cleanup(self):
        with self.launcher.disposable_profile() as profile:
            handler = logging.FileHandler(profile / 'synthetic.log', encoding='utf-8')
            handler.emit(logging.LogRecord('fixture', logging.INFO, __file__, 0, 'synthetic', (), None))
            self.assertIsNotNone(handler.stream)
        self.assertIsNone(handler.stream)
        self.assertFalse(profile.exists())

    def test_profile_restores_working_directory_and_cleans_up_on_exception(self):
        old_cwd = Path.cwd()
        with self.assertRaisesRegex(RuntimeError, 'startup failure'):
            with self.launcher.disposable_profile() as profile:
                (profile / 'fixture').write_text('synthetic', encoding='utf-8')
                raise RuntimeError('startup failure')
        self.assertEqual(Path.cwd(), old_cwd)
        self.assertFalse(profile.exists())


if __name__ == '__main__':
    unittest.main()
