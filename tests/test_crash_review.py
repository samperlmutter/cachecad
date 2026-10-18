"""Independent local-only checks of crash helper confinement and sanitization."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from recovered_harness import deny_external_effects


ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'application/crash_diagnostics.py').is_file():
    raise unittest.SkipTest('Actual application sources absent from experimental wheel')


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


diagnostics = load('_independent_diagnostics_review', 'application/crash_diagnostics.py')
launch = load('_independent_diagnostics_launch', 'application/launch.py')
locks = load('_independent_diagnostics_locks', 'maintained/utils/profile_lock.py')


class IndependentCrashReviewTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.profile = self.root / 'profile'
        self.profile.mkdir()
        self.args = SimpleNamespace(profile_dir=self.profile)
        self.marker = self.profile / 'application-profile.json'
        self.marker.write_text(json.dumps({'application': 'cachecad-reconstructed', 'schema': 1}))
        self.enterContext(patch.dict(sys.modules, {'utils.profile_lock': locks}))

    def helper(self, module=diagnostics):
        return launch.run_native_helper(self.args, module, {}, 'a' * 64)

    def test_helper_only_uses_diagnostics_under_the_marked_locked_profile(self):
        capture = Mock(return_value=0)
        with locks.ProfileLock(self.profile):
            self.assertEqual(self.helper(SimpleNamespace(capture_native=capture)), 0)
        capture.assert_called_once_with(self.profile / 'diagnostics', {}, 'a' * 64, announce=True)
        self.assertFalse((self.profile / 'diagnostics').exists())

    def test_helper_rejects_a_marker_symlink_without_following_output(self):
        outside = self.root / 'outside-marker'
        self.marker.rename(outside)
        self.marker.symlink_to(outside)
        capture = Mock(return_value=0)
        with locks.ProfileLock(self.profile):
            self.assertEqual(self.helper(SimpleNamespace(capture_native=capture)), 1)
        capture.assert_not_called()

    def test_helper_rejects_a_lock_symlink(self):
        target = self.root / 'outside-lock'
        target.write_bytes(b'unchanged')
        (self.profile / '.cachecad-profile.lock').symlink_to(target)
        capture = Mock(return_value=0)
        self.assertEqual(self.helper(SimpleNamespace(capture_native=capture)), 1)
        capture.assert_not_called()
        self.assertEqual(target.read_bytes(), b'unchanged')

    def test_helper_rejects_an_unlocked_profile(self):
        with locks.ProfileLock(self.profile):
            pass
        capture = Mock(return_value=0)
        self.assertEqual(self.helper(SimpleNamespace(capture_native=capture)), 1)
        capture.assert_not_called()

    def test_native_sink_rejects_linked_diagnostics_directory(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.profile / 'diagnostics').symlink_to(outside, target_is_directory=True)
        result = diagnostics.capture_native(self.profile / 'diagnostics', {}, '',
            io.BytesIO(b'Fatal Python error: sensitive-message\n'))
        self.assertEqual(result, 1)
        self.assertEqual(list(outside.iterdir()), [])

    def test_native_parser_retains_only_catalogued_source_and_function(self):
        filename = '/installation/src/core/reviewed.py'
        catalog = {filename: ('reviewed.py', frozenset({'allowed'}))}
        stream = io.BytesIO(('Fatal Python error: secret\n'
            f'  File "{filename}", line 7 in allowed\n'
            f'  File "{filename}", line 8 in SECRET_FUNCTION\n'
            '  File "/private/SECRET_PATH.py", line 9 in SECRET_FUNCTION\n'
            'CACHECAD_EVENT SECRET_EVENT\n').encode())
        directory = self.profile / 'diagnostics'
        self.assertEqual(diagnostics.capture_native(directory, catalog, '', stream), 0)
        raw = next(directory.glob('crash-*.json')).read_text()
        record = json.loads(raw)
        self.assertEqual(record['frames'], [
            dict(source='reviewed.py', function='allowed', line=7),
            dict(source='reviewed.py', function='<redacted>', line=8),
            dict(source='<external>', function='<redacted>', line=9)])
        for secret in ('SECRET', '/private', '/installation', 'secret'):
            self.assertNotIn(secret, raw)

    def test_report_pruning_preserves_unrelated_files_and_link_targets(self):
        directory = self.profile / 'diagnostics'
        directory.mkdir()
        unrelated = directory / 'user-document.json'
        unrelated.write_text('keep')
        outside = self.root / 'outside-report.json'
        outside.write_text('keep outside')
        link = directory / ('crash-' + 'a' * 32 + '.json')
        link.symlink_to(outside)
        owner = diagnostics.CrashDiagnostics()
        self.assertTrue(owner.activate(directory))
        for _ in range(diagnostics.MAX_REPORTS + 3):
            self.assertTrue(owner.record_event('shutdown-timeout'))
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_text(), 'keep outside')
        self.assertEqual(unrelated.read_text(), 'keep')
        self.assertEqual(sum(not item.is_symlink() for item in directory.glob('crash-*.json')),
                         diagnostics.MAX_REPORTS)


if __name__ == '__main__':
    unittest.main()
