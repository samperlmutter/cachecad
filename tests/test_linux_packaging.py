"""Portable Linux packaging and adversarial archive checks; no live account use."""
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / 'packaging/linux'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


support = load('linux_packaging_support', PACKAGING / 'linux_support.py') if PACKAGING.is_dir() else None


def elf(machine=62, kind=3):
    return b'\x7fELF' + bytes((2, 1, 1)) + bytes(9) + struct.pack('<HHI', kind, machine, 1) + bytes(40)


@unittest.skipUnless(support, 'Linux packaging absent from installed wheel fixture')
class LinuxPackagingTests(unittest.TestCase):
    def archive(self, path, extras=()):
        with tarfile.open(path, 'w:gz') as archive:
            for name, raw, mode in [('CacheCAD/CacheCAD', elf(), 0o755),
                                    ('CacheCAD/_internal/app-source/assembly-manifest.json', b'{}', 0o644)]:
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(raw), mode
                archive.addfile(info, io.BytesIO(raw))
            for info in extras:
                archive.addfile(info)

    def test_actual_production_assembly_and_proto_exclusion(self):
        recipe = (PACKAGING / 'cachecad.spec').read_text()
        for required in ("source / 'application/launch.py'", 'payload = verified_payload(source)',
                         'if excluded_frozen_module(name):', 'excludes=list(SOURCE_PACKAGES)',
                         'list(TYPE_IMPORTS) + list(API_IMPORTS)', 'locked_distributions(runtime_lock)',
                         'third-party-notices/Python', 'console=True'):
            self.assertIn(required, recipe)
        for name in ('core.auth_manager', 'gui.main_window', 'development.launch', 'cachecad.__main__'):
            self.assertTrue(support.excluded_frozen_module(name))
        self.assertFalse(support.excluded_frozen_module('googleapiclient.discovery'))

    def test_elf_requires_complete_native_x86_64(self):
        self.assertEqual(support.elf_architecture(elf()), 'x86_64')
        self.assertIsNone(support.elf_architecture(b'ordinary data'))
        for raw in (elf(machine=183), elf(kind=1), elf()[:63], b'\x7fELF' + bytes(60)):
            with self.subTest(raw=raw[:20]), self.assertRaises(ValueError):
                support.elf_architecture(raw)

    def test_glibc_needs_only_numeric_version_order(self):
        text = ('Version definition section\nName: GLIBC_9.0\nVersion needs section\n'
                'Name: GLIBC_2.9\nName: GLIBC_2.38\nName: GLIBCXX_3.4\n')
        self.assertEqual(support.glibc_requirements(text), ['2.9', '2.38'])
        self.assertEqual(support.minimum_glibc([{'glibc_required': ['2.9', '2.38']}]), '2.38')
        with self.assertRaises(ValueError):
            support.minimum_glibc([])

    def test_archive_rejects_noncanonical_and_traversing_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'app.tar.gz'
            self.archive(path)
            self.assertEqual(support.validate_archive(path), 2)
            for name in ('../outside', '/absolute', 'CacheCAD/../outside', 'CacheCAD//file',
                         './CacheCAD/file', 'CacheCAD/file:stream', 'CacheCAD/CacheCAD'):
                self.archive(path, [tarfile.TarInfo(name)])
                with self.subTest(name=name), self.assertRaises(ValueError):
                    support.validate_archive(path)

    def test_archive_rejects_special_files_and_privileged_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'app.tar.gz'
            for kind in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE, tarfile.LNKTYPE):
                member = tarfile.TarInfo('CacheCAD/unsafe')
                member.type = kind
                member.linkname = 'CacheCAD/CacheCAD'
                self.archive(path, [member])
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    support.validate_archive(path)
            for mode in (0o4755, 0o2755, 0o1755):
                member = tarfile.TarInfo('CacheCAD/unsafe')
                member.mode = mode
                self.archive(path, [member])
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    support.validate_archive(path)

    def test_internal_regular_file_link_is_allowed_but_chains_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'app.tar.gz'
            member = tarfile.TarInfo('CacheCAD/link')
            member.type, member.linkname = tarfile.SYMTYPE, 'CacheCAD'
            self.archive(path, [member])
            self.assertEqual(support.validate_archive(path), 3)
            for target in ('/tmp/foreign', '../../outside', 'missing', 'link'):
                member.linkname = target
                self.archive(path, [member])
                with self.subTest(target=target), self.assertRaises(ValueError):
                    support.validate_archive(path)

    def test_file_or_link_cannot_be_an_archive_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'app.tar.gz'
            self.archive(path, [tarfile.TarInfo('CacheCAD/CacheCAD/child')])
            with self.assertRaisesRegex(ValueError, 'non-directory parent'):
                support.validate_archive(path)

    def test_extraction_rejects_existing_targets_and_linked_ancestors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'app.tar.gz'
            self.archive(path)
            (root / 'existing').mkdir()
            (root / 'linked').symlink_to(root / 'existing', target_is_directory=True)
            with self.assertRaises(ValueError):
                support.extract_archive(path, root / 'linked' / 'new-install')
            self.assertFalse((root / 'existing' / 'new-install').exists())
            with self.assertRaises(FileExistsError):
                support.extract_archive(path, root / 'existing')

    def test_disposable_environment_rejects_injection_and_remote_display(self):
        with tempfile.TemporaryDirectory() as temporary:
            hostile = {'HOME': '/real-home', 'PYTHONPATH': '/inject', 'LD_LIBRARY_PATH': '/inject',
                       'GOOGLE_APPLICATION_CREDENTIALS': '/secret', 'QT_PLUGIN_PATH': '/inject',
                       'HTTP_PROXY': 'proxy', 'CACHECAD_GOOGLE_CLIENT_CONFIG': '/client', 'DISPLAY': 'foreign:0'}
            with patch.dict(os.environ, hostile, clear=True):
                result = support.isolated_environment(temporary)
            self.assertEqual(result['HOME'], str(Path(temporary).resolve()))
            self.assertFalse((set(hostile) - {'HOME'}) & set(result))
            self.assertEqual(support.isolated_environment(temporary, display='127.0.0.1:101')['DISPLAY'], '127.0.0.1:101')
            for display in ('example.com:0', 'tcp/foreign:0', '', ':x'):
                with self.assertRaises(ValueError):
                    support.isolated_environment(temporary, display=display)

    def test_snapshot_tracks_permissions_and_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'file').write_text('payload')
            (root / 'link').symlink_to('file')
            before = support.snapshot(root)
            self.assertEqual(before['link'], {'symlink': 'file'})
            (root / 'file').chmod(0o755)
            self.assertNotEqual(support.snapshot(root), before)

    def test_packaging_lock_matches_official_wheel_evidence(self):
        lock = ROOT / 'requirements/linux-package.lock'
        records = json.loads((PACKAGING / 'dependency-artifacts.json').read_text())['artifacts']
        self.assertEqual(set(support.locked_distributions(lock)), {item['name'] for item in records})
        for item in records:
            self.assertIn(f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}", lock.read_text())
            self.assertTrue(item['metadata_url'].startswith('https://pypi.org/pypi/'))
            self.assertTrue(item['wheel_url'].startswith('https://files.pythonhosted.org/'))
            self.assertTrue(item['filename'].endswith(('-any.whl', '_x86_64.whl')))

    def test_harness_gates_native_run_crash_tests_and_unmodified_bundle(self):
        text = (PACKAGING / 'test_app.py').read_text()
        for value in ("('offscreen', False), ('xcb', False), ('xcb', True)",
                      'native_runtime_verified=not offscreen_only', 'snapshot(bundle) != initial',
                      "'os_network_sandbox': False", "'synthetic_fault': True",
                      "'--diagnostic-test', mode", 'profile_cleanup_verified',
                      "profiles = Path(environment['TMPDIR'])", 'profile = profiles / name',
                      'preserve_interactions(result, profile, report_dir)', 'all_installed_processes_exited'):
            self.assertIn(value, text)

    def test_readme_discloses_linux_auth_blocker(self):
        text = (PACKAGING / 'APPLICATION-README.txt').read_text()
        self.assertIn('Google sign-in and Drive sync are unavailable on Linux', text)
        self.assertNotIn('session-only', text)

    def test_startup_diagnostics_count_means_activated_not_remaining(self):
        with patch.dict(sys.modules, {'linux_support': support}):
            harness = load('linux_packaging_harness', PACKAGING / 'test_app.py')
        result = {'mode': 'application', 'actual_gui': 'gui.main_window.MainWindow',
                  'qt_platform': 'offscreen', 'frozen': True, 'source_loader': 'verified-source',
                  'signed_in': False, 'has_credentials': False, 'drive_client_created': False,
                  'network_disabled': True, 'active_threads': 0, 'setup_required': True,
                  'sdk_imports_verified': True, 'certificate_bundle_present': True,
                  'drive_discovery_present': True, 'rendered_size': [809, 518],
                  'errors': [], 'external_attempts': [], 'development_modules_loaded': [],
                  'forbidden_modules_loaded': [], 'credential_files': [],
                  'diagnostics': {'enabled': True, 'local_only': True,
                                  'native_capture': True, 'helper_processes': 1}}
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(harness.read_startup_result(
                'CACHECAD_APPLICATION_RESULT=' + json.dumps(result), temporary, 'offscreen'), result)
            result['diagnostics']['native_capture'] = False
            with self.assertRaises(ValueError):
                harness.read_startup_result('CACHECAD_APPLICATION_RESULT=' + json.dumps(result), temporary, 'offscreen')


if __name__ == '__main__':
    unittest.main()
