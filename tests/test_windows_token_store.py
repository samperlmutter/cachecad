"""Synthetic tokens only. No real credential/cache paths or network."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

if not (Path(__file__).resolve().parents[1] / 'maintained/utils/windows_token_store.py').is_file():
    raise unittest.SkipTest('Maintained token store is absent from wheel fixture')

from maintained.utils import windows_token_store as store


class TestProtector:
    """Test-only keyed integrity + reversible transform; never production DPAPI."""
    def protect(self, value):
        encrypted = bytes(c ^ 0xA5 for c in value)
        return hmac.digest(b'synthetic-only-test-key', encrypted, 'sha256') + encrypted

    def unprotect(self, value):
        if not hmac.compare_digest(value[:32], hmac.digest(
                b'synthetic-only-test-key', value[32:], 'sha256')):
            raise ValueError('SYNTHETIC-TOKEN-CONTENT')
        return bytes(c ^ 0xA5 for c in value[32:])


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'profile' / 'google-session.dpapi'
        self.cache = store.WindowsTokenStore(self.path, protector=TestProtector())
        self.record = {'refresh_token': 'SYNTHETIC-REFRESH', 'schema': 1}

    def test_atomic_encrypted_roundtrip_and_clear(self):
        self.assertIsNone(self.cache.load())
        self.cache.save(self.record)
        raw = self.path.read_bytes()
        self.assertNotIn(b'SYNTHETIC-REFRESH', raw)
        self.assertTrue(raw.startswith(store._MAGIC))
        self.assertEqual(self.cache.load(), self.record)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.cache.clear()
        self.cache.clear()
        self.assertIsNone(self.cache.load())

    def test_save_encrypts_before_any_file_is_opened(self):
        def protect(value):
            self.assertFalse(self.path.parent.exists())
            raise ValueError('SYNTHETIC-REFRESH')
        with patch.object(self.cache._protector, 'protect', protect):
            with self.assertRaises(store.TokenStoreError) as error:
                self.cache.save(self.record)
        self.assertIsNone(error.exception.__context__)
        self.assertNotIn('SYNTHETIC', str(error.exception))
        self.assertFalse(self.path.parent.exists())

    def test_failed_replace_preserves_old_cache_and_removes_temporary(self):
        self.cache.save(self.record)
        before = self.path.read_bytes()
        with patch.object(store.os, 'replace', side_effect=OSError('SYNTHETIC-TOKEN')):
            with self.assertRaises(store.TokenStoreError):
                self.cache.save({'refresh_token': 'SYNTHETIC-ROTATED'})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_tampered_plaintext_truncated_and_wrong_json_fail_closed(self):
        self.cache.save(self.record)
        valid = self.path.read_bytes()
        cases = [b'{"refresh_token":"SYNTHETIC"}', valid[:16], valid[:-1] + b'!']
        for plain in (b'[]', b'{"a":1,"a":2}', b'{'):
            cases.append(store._MAGIC + TestProtector().protect(plain))
        for value in cases:
            self.path.write_bytes(value)
            with self.subTest(value=value[:20]), self.assertRaises(store.TokenStoreError) as error:
                self.cache.load()
            self.assertIsNone(error.exception.__context__)

    @unittest.skipIf(os.name == 'nt', 'Symlink privilege varies on Windows')
    def test_symlink_target_or_parent_never_followed(self):
        target = Path(self.folder.name) / 'target'
        target.write_bytes(b'UNRELATED')
        self.path.parent.mkdir()
        self.path.symlink_to(target)
        for operation in (self.cache.load, lambda: self.cache.save(self.record), self.cache.clear):
            with self.assertRaises(store.TokenStoreError):
                operation()
        self.assertEqual(target.read_bytes(), b'UNRELATED')

    @unittest.skipIf(os.name == 'nt', 'Non-Windows failure contract')
    def test_no_plaintext_fallback_on_other_platforms(self):
        with self.assertRaises(store.TokenStoreError) as error:
            store.WindowsTokenStore(self.path)
        self.assertEqual(error.exception.code, 'platform')
        self.assertFalse(self.path.exists())


@unittest.skipUnless(os.name == 'nt', 'Actual CurrentUser DPAPI requires Windows')
class ActualWindowsDPAPITests(unittest.TestCase):
    def test_actual_dpapi_disposable_synthetic_roundtrip_and_tamper(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = store.WindowsTokenStore(Path(folder) / 'synthetic.dpapi')
            record = {'refresh_token': 'SYNTHETIC-NOT-A-REAL-CREDENTIAL'}
            cache.save(record)
            self.assertEqual(cache.load(), record)
            raw = cache.path.read_bytes()
            self.assertNotIn(record['refresh_token'].encode(), raw)
            cache.path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
            with self.assertRaises(store.TokenStoreError):
                cache.load()
            cache.clear()
