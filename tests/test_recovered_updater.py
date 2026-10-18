"""Execute only selected recovered updater method against disposable fake downloads."""
import hashlib
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from recovered_harness import deny_external_effects, fixture_open, load_function

ROOT = Path(__file__).resolve().parents[1]


class ScopedPath:
    """Keep definition-time Path.home() default within the disposable fixture."""
    def __init__(self, root):
        self.root = root

    def __call__(self, value):
        return Path(value)

    def home(self):
        return self.root


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        assert chunk_size == 8192
        yield self.payload[:3]
        yield self.payload[3:]


@unittest.skipUnless(
    (ROOT / "recovered/core/github_updater.py").is_file()
    and (ROOT / "maintained/core/github_updater.py").is_file(),
    "recovery and maintenance source fixtures unavailable in wheel-only tests",
)
class UpdaterRegressionTests(unittest.TestCase):
    def exercise(self, source, digest, expect_error=False):
        payload = b'disposable updater fixture, never an executable'
        with tempfile.TemporaryDirectory() as directory, deny_external_effects():
            root = Path(directory)
            calls = []

            def fake_get(url, stream):
                self.assertEqual(url, 'https://invalid.example/fixture')
                self.assertTrue(stream)
                calls.append(url)
                return FakeResponse(payload)

            def scoped_open(path, mode):
                self.assertEqual(Path(path).parent, root)
                self.assertEqual(mode, 'wb')
                return fixture_open(root)(path, mode)

            namespace = {
                'requests': SimpleNamespace(
                    get=fake_get, exceptions=SimpleNamespace(RequestException=RuntimeError)),
                'hashlib': hashlib, 'os': SimpleNamespace(path=os.path),
                'Path': ScopedPath(root), 'logging': logging, 'open': scoped_open,
            }
            method = load_function(
                ROOT / source / 'core/github_updater.py',
                'GithubUpdater.download_latest_version', namespace)
            obj = SimpleNamespace(
                tag='1.2.3', get_latest_release_info=lambda: '1.2.3',
                response={'assets': [{
                    'name': 'CacheCAD_Setup.exe', 'digest': digest,
                    'browser_download_url': 'https://invalid.example/fixture',
                }]})
            target = root / 'CacheCAD_Setup_1_2_3.exe'
            if expect_error:
                with self.assertRaisesRegex(AttributeError, "'str' object has no attribute 'delete'"):
                    method(obj, path=root)
                self.assertEqual(target.read_bytes(), payload)
                result = None
            else:
                result = method(obj, path=root)
            return result, target.exists(), calls

    def test_original_mismatch_raises_and_leaves_untrusted_file(self):
        result, exists, calls = self.exercise('recovered', 'sha256:' + '0' * 64, True)
        self.assertIsNone(result)
        self.assertTrue(exists)
        self.assertEqual(len(calls), 1)

    def test_maintained_mismatch_returns_false_and_removes_file(self):
        result, exists, calls = self.exercise('maintained', 'sha256:' + '0' * 64)
        self.assertIs(result, False)
        self.assertFalse(exists)
        self.assertEqual(len(calls), 1)

    def test_matching_digest_preserved_in_both_versions(self):
        digest = hashlib.sha256(b'disposable updater fixture, never an executable').hexdigest()
        for source in ('recovered', 'maintained'):
            with self.subTest(source=source):
                result, exists, calls = self.exercise(source, 'sha256:' + digest.upper())
                self.assertIs(result, True)
                self.assertTrue(exists)
                self.assertEqual(len(calls), 1)

    def test_missing_digest_never_downloads_in_both_versions(self):
        for source in ('recovered', 'maintained'):
            with self.subTest(source=source):
                result, exists, calls = self.exercise(source, None)
                self.assertIs(result, False)
                self.assertFalse(exists)
                self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
