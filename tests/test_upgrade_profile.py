"""Preserved-profile checks are explicit disposable fixtures, never live sign-in."""
from contextlib import redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'application/launch.py'
if not MODULE.is_file():
    raise unittest.SkipTest('Maintained launcher absent from scaffold wheel')
spec = importlib.util.spec_from_file_location('_upgrade_profile_launcher', MODULE)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class PreservedProfileTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.profile = self.root / 'fixture'
        self.profile.mkdir()
        (self.profile / '.cachecad-upgrade-fixture.json').write_text(
            '{"fixture":"cachecad-upgrade-ci","schema":1}')
        self.arguments = ['--app-source', str(self.root / 'source'), '--startup-check',
                          '--profile-dir', str(self.profile), '--preserved-profile-check',
                          '--client-config', str(self.root / 'missing-client.json')]

    def parse(self, arguments=None, enabled=True):
        with patch.dict(os.environ, {'CACHECAD_ENABLE_UPGRADE_TESTS': '1' if enabled else ''}), redirect_stderr(io.StringIO()):
            return launcher.parse_arguments(arguments or self.arguments)

    def test_explicit_marked_temporary_profile_and_missing_config_are_required(self):
        self.assertTrue(self.parse().preserved_profile_check)
        with self.assertRaises(SystemExit):
            self.parse(enabled=False)
        for remove in ('--startup-check', '--preserved-profile-check'):
            arguments = list(self.arguments)
            arguments.remove(remove)
            if remove == '--startup-check':
                with self.assertRaises(SystemExit):
                    self.parse(arguments)
            else:
                self.assertFalse(self.parse(arguments).preserved_profile_check)
        (self.root / 'missing-client.json').write_text('{}')
        with self.assertRaises(SystemExit):
            self.parse()

    def test_missing_wrong_or_linked_fixture_marker_is_rejected(self):
        marker = self.profile / '.cachecad-upgrade-fixture.json'
        for contents in ('{}', '{"fixture":"other","schema":1}', 'not json'):
            marker.write_text(contents)
            with self.assertRaises(SystemExit):
                self.parse()
        marker.unlink()
        with self.assertRaises(SystemExit):
            self.parse()

    def test_profile_outside_temp_and_fault_injection_combinations_are_rejected(self):
        with patch.object(launcher.tempfile, 'gettempdir', return_value=str(self.root / 'different')):
            with self.assertRaises(SystemExit):
                self.parse()
        with patch.dict(os.environ, {'CACHECAD_ENABLE_DIAGNOSTIC_TESTS': '1'}):
            with self.assertRaises(SystemExit):
                self.parse(self.arguments + ['--diagnostic-test', 'main'])

    def test_ordinary_startup_keeps_empty_profile_rule_and_fixture_preserves_bytes(self):
        marker = self.profile / 'application-profile.json'
        marker.write_text('{"application":"cachecad-reconstructed","schema":1}')
        data = self.profile / 'app_data'
        data.mkdir()
        (data / 'settings.json').write_text(json.dumps({'working_directory': str(self.root)}))
        (data / 'google-session.dpapi').write_bytes(b'CacheCAD-DPAPI-v1\0SYNTHETIC')
        before = {path: path.read_bytes() for path in self.profile.rglob('*') if path.is_file()}
        with self.assertRaisesRegex(ValueError, 'new empty'):
            launcher.prepare_profile(self.profile, startup_check=True)
        launcher.prepare_profile(self.profile, startup_check=True, preserved_profile_check=True)
        self.assertEqual(before, {path: path.read_bytes() for path in self.profile.rglob('*') if path.is_file()})
        marker.write_text('{"schema":1}')
        with self.assertRaisesRegex(ValueError, 'legacy'):
            launcher.prepare_profile(self.profile, startup_check=True, preserved_profile_check=True)


if __name__ == '__main__':
    unittest.main()
