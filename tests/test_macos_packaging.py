"""Portable macOS contracts and hostile native/archive fixtures; no native execution."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import stat
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / 'packaging/macos'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


support = load('macos_packaging_support', PACKAGING / 'macos_support.py') if PACKAGING.is_dir() else None


def thin(cpu=0x0100000c, minimum=0x000c0000, platform=1, legacy=False):
    command = (struct.pack('<IIII', 0x24, 16, minimum, minimum) if legacy else
               struct.pack('<IIIIII', 0x32, 24, platform, minimum, minimum, 0))
    return struct.pack('<IIIIIIII', 0xfeedfacf, cpu, 0, 6, 1, len(command), 0, 0) + command


def fat(*slices):
    header = struct.pack('>II', 0xcafebabe, len(slices))
    position = 8 + 20 * len(slices)
    for cpu, raw in slices:
        header += struct.pack('>IIIII', cpu, 0, position, len(raw), 0)
        position += len(raw)
    return header + b''.join(raw for cpu, raw in slices)


@unittest.skipUnless(support, 'macOS packaging absent from installed wheel fixture')
class MacOSPackagingTests(unittest.TestCase):
    def test_actual_production_source_guard_is_reused_without_stubs(self):
        recipe = (PACKAGING / 'cachecad.spec').read_text()
        self.assertIn("source / 'application/launch.py'", recipe)
        self.assertIn('payload = verified_payload(source)', recipe)
        self.assertIn('if excluded_frozen_module(name):', recipe)
        self.assertIn('excludes=list(SOURCE_PACKAGES)', recipe)
        self.assertIn("target_arch='arm64'", recipe)
        self.assertIn('codesign_identity=None', recipe)
        self.assertIn('entitlements_file=None', recipe)
        self.assertIn('console=True', recipe)
        self.assertIn("'LSBackgroundOnly': False", recipe)
        for name in ('core.auth_manager', 'gui.main_window', 'development.launch'):
            self.assertTrue(support.excluded_frozen_module(name))
        for name in ('google.oauth2.credentials', 'googleapiclient.discovery', 'Foundation'):
            self.assertFalse(support.excluded_frozen_module(name))

    def test_required_real_sdk_and_native_backend_imports_are_frozen(self):
        recipe = (PACKAGING / 'cachecad.spec').read_text()
        self.assertIn('list(TYPE_IMPORTS) + list(API_IMPORTS)', recipe)
        self.assertIn("collect_submodules('Foundation')", recipe)
        self.assertIn("collect_submodules('objc')", recipe)
        self.assertIn("collect_submodules('AppKit')", recipe)
        self.assertIn('locked_distributions(runtime_lock)', recipe)
        self.assertIn('third-party-notices/Python', recipe)

    def test_macho_thin_build_version_and_legacy_commands(self):
        self.assertEqual(support.macho_slices(thin()), [{'architecture': 'arm64', 'minimum_macos': '12.0.0'}])
        self.assertEqual(support.macho_slices(thin(minimum=0x000b0301, legacy=True)),
                         [{'architecture': 'arm64', 'minimum_macos': '11.3.1'}])
        self.assertEqual(support.macho_slices(b'ordinary data'), [])

    def test_macho_universal2_reports_distinct_architecture_minima(self):
        raw = fat((0x01000007, thin(0x01000007, 0x000a0d00)), (0x0100000c, thin()))
        self.assertEqual(support.macho_slices(raw), [
            {'architecture': 'x86_64', 'minimum_macos': '10.13.0'},
            {'architecture': 'arm64', 'minimum_macos': '12.0.0'}])

    def test_macho_truncation_and_wrong_platform_fail_closed(self):
        cases = [thin()[:12], thin()[:-1], thin(platform=2), thin(minimum=0),
                 struct.pack('>II', 0xcafebabe, 100),
                 fat((0x01000007, thin())), fat((0x0100000c, thin()), (0x0100000c, thin()))]
        for raw in cases:
            with self.subTest(raw=raw[:12]), self.assertRaises(ValueError):
                support.macho_slices(raw)

    def test_macho_command_size_zero_and_missing_minimum_fail_closed(self):
        for command in (struct.pack('<II', 0x32, 0), struct.pack('<II', 0x1b, 8)):
            raw = struct.pack('<IIIIIIII', 0xfeedfacf, 0x0100000c, 0, 6, 1, len(command), 0, 0) + command
            with self.assertRaises(ValueError):
                support.macho_slices(raw)

    def test_native_bundle_audit_requires_thin_arm64_and_os_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'extension.so'
            binary.write_bytes(thin())
            audit = support.audit_native_files(root)
            self.assertEqual(audit[0]['path'], 'extension.so')
            self.assertEqual(audit[0]['sha256'], support.sha256(binary))
            for raw in (thin(0x01000007), thin(minimum=0x000d0000), b'MZforeign', b'\x7fELFforeign'):
                binary.write_bytes(raw)
                with self.subTest(raw=raw[:8]), self.assertRaises(ValueError):
                    support.audit_native_files(root)
            binary.write_bytes(fat((0x01000007, thin(0x01000007)), (0x0100000c, thin())))
            with self.assertRaises(ValueError):
                support.audit_native_files(root)
            self.assertEqual(len(support.audit_native_files(root, require_thin=False)), 1)

    def test_native_audit_refuses_no_native_payload_and_escaping_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                support.audit_native_files(root)
            (root / 'a').write_bytes(thin())
            (root / 'outside').symlink_to(root.parent)
            with self.assertRaisesRegex(ValueError, 'escapes'):
                support.audit_native_files(root)

    def test_version_tuple_is_not_lexicographic(self):
        self.assertGreater(support.version_tuple('12.0'), support.version_tuple('11.9.99'))
        self.assertEqual(support.version_tuple('12'), (12, 0, 0))
        for bad in ('12.a', '12.1.2.3', '', ' 12', '-12'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                support.version_tuple(bad)

    def test_bundle_identity_and_executable_permissions_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / support.EXECUTABLE
            executable.parent.mkdir(parents=True)
            executable.write_bytes(thin())
            executable.chmod(0o755)
            identity = {'version': '0.1.1', 'release_number': 2}
            info = {'CFBundleExecutable': 'CacheCAD', 'CFBundleIdentifier': support.BUNDLE_ID,
                    'CFBundlePackageType': 'APPL', 'LSMinimumSystemVersion': '12.0',
                    'NSHighResolutionCapable': True, 'LSBackgroundOnly': False, 'CFBundleVersion': '2',
                    'CFBundleShortVersionString': '0.1.1'}
            path = root / 'Contents/Info.plist'
            path.write_bytes(plistlib.dumps(info))
            self.assertEqual(support.verify_bundle_info(root, identity), info)
            info['LSMinimumSystemVersion'] = '11.0'
            path.write_bytes(plistlib.dumps(info))
            with self.assertRaises(ValueError):
                support.verify_bundle_info(root, identity)

    def test_smoke_environment_cannot_inherit_accounts_plugins_or_python(self):
        with tempfile.TemporaryDirectory() as directory:
            hostile = {'HOME': '/real-user', 'PYTHONPATH': '/hostile', 'DYLD_LIBRARY_PATH': '/injected',
                       'GOOGLE_APPLICATION_CREDENTIALS': '/secret', 'QT_PLUGIN_PATH': '/hostile',
                       'HTTP_PROXY': 'proxy', 'CACHECAD_GOOGLE_CLIENT_CONFIG': '/real-client', 'LANG': 'en_US.UTF-8'}
            with patch.dict(os.environ, hostile, clear=True):
                result = support.isolated_environment(Path(directory))
            self.assertEqual(result['HOME'], str(Path(directory).resolve()))
            self.assertEqual(result['PATH'], '/usr/bin:/bin:/usr/sbin:/sbin')
            self.assertEqual(result['LANG'], 'en_US.UTF-8')
            self.assertFalse(set(hostile) - {'HOME', 'LANG'} & result.keys())

    def test_bundle_snapshot_tracks_links_and_executable_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'a').write_text('data')
            (root / 'b').symlink_to('a')
            before = support.snapshot(root)
            self.assertEqual(before['b'], {'symlink': 'a'})
            (root / 'a').chmod(0o755)
            self.assertNotEqual(before, support.snapshot(root))

    def test_zip_checks_reject_traversal_and_external_symlinks(self):
        with patch.dict(sys.modules, {'macos_support': support}):
            builder = load('macos_packaging_build', PACKAGING / 'build.py')
        with patch.dict(sys.modules, {'macos_support': support, 'build': builder}):
            app_test = load('macos_app_test', PACKAGING / 'test_app.py')
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'app.zip'
            for bad in ('../escape', '/absolute', 'CacheCAD.app/../../escape', 'unexpected.txt'):
                with zipfile.ZipFile(archive, 'w') as output:
                    output.writestr(f'{support.APP_NAME}/{support.EXECUTABLE}', b'app')
                    output.writestr(bad, b'bad')
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    app_test.validate_archive(archive)
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr(f'{support.APP_NAME}/{support.EXECUTABLE}', b'app')
                info = zipfile.ZipInfo('CacheCAD.app/Contents/outside')
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                output.writestr(info, '../../../outside')
            with self.assertRaises(ValueError):
                app_test.validate_archive(archive)
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr(f'{support.APP_NAME}/{support.EXECUTABLE}', b'app')
            app_test.validate_archive(archive)

    def test_native_harness_checks_network_denial_clean_exit_and_modern_backend(self):
        text = (PACKAGING / 'test_app.py').read_text()
        for required in ("('offscreen', 'cocoa')", '(deny network*)', "'trash_backend'",
                         'send2trash.mac.modern', 'validate_application_smoke', 'snapshot(bundle) != initial',
                         "'clean_exit': True", "'--profile-dir', profile", 'resolve(strict=True)'):
            self.assertIn(required, text)
        self.assertNotIn('xattr', text)
        self.assertNotIn('spctl', text)

    def harness(self):
        with patch.dict(sys.modules, {'macos_support': support}):
            builder = load('macos_packaging_build', PACKAGING / 'build.py')
        with patch.dict(sys.modules, {'macos_support': support, 'build': builder}):
            return load('macos_app_test', PACKAGING / 'test_app.py')

    def startup_result(self):
        return {'mode': 'application', 'actual_gui': 'gui.main_window.MainWindow',
                'qt_platform': 'cocoa', 'frozen': True, 'source_loader': 'verified-source',
                'signed_in': False, 'has_credentials': False, 'drive_client_created': False,
                'network_disabled': True, 'active_threads': 0, 'setup_required': True,
                'sdk_imports_verified': True, 'certificate_bundle_present': True,
                'drive_discovery_present': True, 'rendered_size': [813, 518],
                'trash_backend': 'send2trash.mac.modern', 'errors': [], 'external_attempts': [],
                'development_modules_loaded': [], 'forbidden_modules_loaded': [], 'credential_files': []}

    def test_final_profile_result_uses_same_strict_native_validator(self):
        app_test = self.harness()
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            result = self.startup_result()
            report = profile / 'startup-check-result.json'
            report.write_text(json.dumps(result))
            self.assertEqual(app_test.read_startup_result('', profile, 'cocoa'), (result, 'profile'))
            stdout = 'CACHECAD_APPLICATION_RESULT=' + json.dumps(result)
            self.assertEqual(app_test.read_startup_result(stdout, profile, 'cocoa'),
                             (result, 'stdout-and-profile'))
            result['qt_platform'] = 'offscreen'
            report.write_text(json.dumps(result))
            with self.assertRaises(ValueError):
                app_test.read_startup_result('', profile, 'cocoa')

    def test_stdout_and_profile_mismatch_and_duplicate_records_are_rejected(self):
        app_test = self.harness()
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            result = self.startup_result()
            stdout = 'CACHECAD_APPLICATION_RESULT=' + json.dumps(result)
            result['rendered_size'] = [900, 600]
            (profile / 'startup-check-result.json').write_text(json.dumps(result))
            for text in (stdout, stdout + '\n' + stdout):
                with self.assertRaises(ValueError):
                    app_test.read_startup_result(text, profile, 'cocoa')

    def test_missing_partial_oversized_linked_profile_report_never_implies_success(self):
        app_test = self.harness()
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            path = profile / 'startup-check-result.json'
            with self.assertRaises(ValueError):
                app_test.read_startup_result('', profile, 'cocoa')
            for content in ('{', 'x' * 65537, 'null', '{}'):
                path.write_text(content)
                with self.subTest(content=content[:8]), self.assertRaises(ValueError):
                    app_test.read_startup_result('', profile, 'cocoa')
            path.write_text('null')
            with self.assertRaises(ValueError):
                app_test.read_startup_result('CACHECAD_APPLICATION_RESULT=' + json.dumps(self.startup_result()), profile, 'cocoa')
            path.unlink()
            target = profile / 'other.json'
            target.write_text(json.dumps(self.startup_result()))
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                app_test.read_startup_result('', profile, 'cocoa')

    def test_phase_evidence_is_bounded_allowlisted_and_survives_profile_cleanup(self):
        app_test = self.harness()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / 'profile'
            profile.mkdir()
            phases = profile / 'startup-check-phases.json'
            phases.write_text(json.dumps(['profile_ready', 'before_qapplication']))
            result = app_test.preserve_startup_evidence(profile, root, 'cocoa')
            self.assertEqual(result['phases'], ['profile_ready', 'before_qapplication'])
            phases.write_text(json.dumps(['private user text']))
            result = app_test.preserve_startup_evidence(profile, root, 'bad')
            self.assertEqual(result['phases'], [])
            self.assertEqual(result['rejected_reports'], 1)
            self.assertNotIn('private user text', (root / 'bad.evidence.json').read_text())
            phases.unlink()
            profile.rmdir()
            self.assertEqual(json.loads((root / 'cocoa.evidence.json').read_text())['phases'],
                             ['profile_ready', 'before_qapplication'])

    def test_failure_probe_is_diagnostic_only_and_uses_unchanged_network_denial(self):
        app_test = self.harness()
        from types import SimpleNamespace
        fake = SimpleNamespace(returncode=0,
            stdout='CACHECAD_QT_PROBE=before_import\nCACHECAD_QT_PROBE=after_import\nprivate output\n',
            stderr='private error details')
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app_test.subprocess, 'run', return_value=fake) as run:
                result = app_test.diagnose_cocoa(Path('/usr/bin/sandbox-exec'), Path(directory),
                                               env={}, cwd=directory)
            self.assertFalse(result['counts_as_native_app_pass'])
            self.assertEqual(result['phases'], ['before_import', 'after_import'])
            self.assertEqual(run.call_args.args[0][2], '(version 1)(allow default)(deny network*)')
            self.assertNotIn('private', (Path(directory) / 'cocoa-source-probe.json').read_text())

    def test_hash_locks_match_official_wheel_evidence_and_are_arm_compatible(self):
        evidence = json.loads((PACKAGING / 'dependency-artifacts.json').read_text())
        for filename in ('runtime-macos.lock', 'macos-build.lock'):
            lock = ROOT / 'requirements' / filename
            names = support.locked_distributions(lock)
            records = [item for item in evidence['artifacts'] if item['lock'] == filename]
            self.assertEqual(set(names), {item['name'] for item in records})
            for item in records:
                self.assertIn(f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}", lock.read_text())
                self.assertTrue(item['metadata_url'].startswith('https://pypi.org/pypi/'))
                self.assertTrue(item['wheel_url'].startswith('https://files.pythonhosted.org/'))
                self.assertTrue(item['filename'].endswith(('-any.whl', '_arm64.whl', '_universal2.whl')))
        runtime = support.locked_distributions(ROOT / 'requirements/runtime-macos.lock')
        self.assertIn('pyside6-essentials', runtime)
        self.assertIn('pyobjc-core', runtime)
        self.assertIn('pyobjc-framework-cocoa', runtime)
        self.assertNotIn('pyside6-addons', runtime)
        self.assertNotIn('pyobjc', runtime)
        self.assertNotIn('google-cloud-logging', runtime)


if __name__ == '__main__':
    unittest.main()
