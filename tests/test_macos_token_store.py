"""Synthetic sessions only; native Keychain access requires an explicit CI opt-in."""
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

if not (Path(__file__).resolve().parents[1] / 'maintained/utils/macos_token_store.py').is_file():
    raise unittest.SkipTest('Maintained token store is absent from wheel fixture')

from maintained.utils import application_mode, macos_token_store as store, windows_token_store


def reference(value):
    return value.value if isinstance(value, ctypes.c_void_p) else value


class FakeFrameworks:
    """Exercise production query construction without loading macOS frameworks."""
    def __init__(self):
        self.heap, self.released, self.calls, self.records = {}, [], [], {}
        self.next_ref = 100
        self.ui = 1
        self.unlocked = True
        self.statuses = {}
        self.native = object.__new__(store.MacOSKeychain)
        names = ('kSecClass', 'kSecClassGenericPassword', 'kSecAttrService', 'kSecAttrAccount',
                 'kSecAttrSynchronizable', 'kSecMatchSearchList', 'kSecUseKeychain',
                 'kSecMatchLimit', 'kSecMatchLimitOne', 'kSecReturnData', 'kSecValueData')
        self.native._keys = {name: index for index, name in enumerate(names, 1)}
        self.native._true, self.native._false = 30, 31
        self.native._dict_keys = self.native._dict_values = self.native._array_values = ctypes.c_byte()
        self.native._cf = SimpleNamespace(
            CFRelease=lambda value: self.released.append(reference(value)),
            CFStringCreateWithBytes=lambda _, value, length, encoding, external: self.new(value.decode()),
            CFDataCreate=lambda _, value, length: self.new(value),
            CFDictionaryCreate=lambda _, keys, values, count, *callbacks: self.new(
                {self.heap.get(keys[i], keys[i]): values[i] for i in range(count)}),
            CFArrayCreate=lambda _, values, count, callback: self.new(list(values[:count])),
            CFDataGetTypeID=lambda: 99,
            CFGetTypeID=lambda value: 99 if isinstance(self.heap[reference(value)], bytes) else 98,
            CFDataGetLength=lambda value: len(self.heap[reference(value)]),
            CFDataGetBytePtr=self.data_pointer,
        )
        self.native._security = SimpleNamespace(
            SecKeychainGetUserInteractionAllowed=self.get_ui,
            SecKeychainSetUserInteractionAllowed=self.set_ui,
            SecKeychainCopyDomainDefault=self.default,
            SecKeychainGetStatus=self.state,
            SecItemCopyMatching=self.load,
            SecItemUpdate=self.update,
            SecItemAdd=self.add,
            SecItemDelete=self.clear,
        )

    def new(self, value):
        self.next_ref += 1
        self.heap[self.next_ref] = value
        return self.next_ref

    def data_pointer(self, ref):
        self.buffer = ctypes.create_string_buffer(self.heap[reference(ref)])
        return ctypes.addressof(self.buffer)

    def status(self, operation, default=0):
        return self.statuses.get(operation, []).pop(0) if self.statuses.get(operation) else default

    def get_ui(self, output):
        output._obj.value = self.ui
        return self.status('get_ui')

    def set_ui(self, value):
        self.calls.append(('ui', value))
        status = self.status('set_ui')
        if status == 0:
            self.ui = value
        return status

    def default(self, domain, output):
        assert domain == 0 and self.ui == 0
        output._obj.value = 50
        return self.status('default')

    def state(self, keychain, output):
        assert reference(keychain) == 50 and self.ui == 0
        output._obj.value = 7 if self.unlocked else 6
        return self.status('state')

    def query(self, operation, query):
        assert self.ui == 0
        values = self.heap[query]
        keys = self.native._keys
        assert values[keys['kSecClass']] == keys['kSecClassGenericPassword']
        assert values[keys['kSecAttrSynchronizable']] == self.native._false
        if operation == 'add':
            assert values[keys['kSecUseKeychain']] == 50
            assert keys['kSecMatchSearchList'] not in values
        else:
            assert self.heap[values[keys['kSecMatchSearchList']]] == [50]
            assert keys['kSecUseKeychain'] not in values
        identity = (self.heap[values[keys['kSecAttrService']]],
                    self.heap[values[keys['kSecAttrAccount']]])
        self.calls.append((operation, identity))
        return values, identity

    def load(self, query, output):
        values, identity = self.query('load', query)
        assert values[self.native._keys['kSecReturnData']] == self.native._true
        assert values[self.native._keys['kSecMatchLimit']] == self.native._keys['kSecMatchLimitOne']
        status = self.status('load', 0 if identity in self.records else store._NOT_FOUND)
        if status == 0:
            output._obj.value = self.new(self.records[identity])
        return status

    def update(self, query, change):
        _, identity = self.query('update', query)
        status = self.status('update', 0 if identity in self.records else store._NOT_FOUND)
        if status == 0:
            self.records[identity] = self.heap[self.heap[change][self.native._keys['kSecValueData']]]
        return status

    def add(self, query, output):
        values, identity = self.query('add', query)
        assert output is None
        status = self.status('add', store._DUPLICATE if identity in self.records else 0)
        if status == 0:
            self.records[identity] = self.heap[values[self.native._keys['kSecValueData']]]
        return status

    def clear(self, query):
        _, identity = self.query('clear', query)
        status = self.status('clear', 0 if identity in self.records else store._NOT_FOUND)
        if status == 0:
            self.records.pop(identity, None)
        return status


class MacOSTokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'profile' / 'google-session.keychain'
        self.frameworks = FakeFrameworks()
        self.cache = store.MacOSTokenStore(self.path, keychain=self.frameworks.native)
        self.record = {'refresh_token': 'SYNTHETIC-REFRESH', 'schema': 1}

    def assert_safe_error(self, operation):
        with self.assertRaises(store.TokenStoreError) as caught:
            operation()
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn('SYNTHETIC', str(caught.exception))
        self.assertEqual(caught.exception.code, 'storage')
        self.assertFalse(self.path.exists())

    def test_scoped_native_queries_roundtrip_rotation_and_idempotent_clear(self):
        self.assertIsNone(self.cache.load())
        self.cache.save(self.record)
        self.assertEqual(self.cache.load(), self.record)
        self.cache.save({'refresh_token': 'SYNTHETIC-ROTATED'})
        self.assertEqual(self.cache.load(), {'refresh_token': 'SYNTHETIC-ROTATED'})
        self.cache.clear()
        self.cache.clear()
        self.assertIsNone(self.cache.load())
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.frameworks.ui, 1)
        self.assertEqual(set(self.frameworks.heap), set(self.frameworks.released) - {50})
        self.assertTrue(all(identity[0] == store._SERVICE for operation, identity in
                            self.frameworks.calls if operation != 'ui'))

    def test_different_profiles_do_not_read_update_or_delete_each_other(self):
        other = store.MacOSTokenStore(self.path.parent / 'other', keychain=self.frameworks.native)
        self.cache.save(self.record)
        self.assertIsNone(other.load())
        other.save({'refresh_token': 'SYNTHETIC-OTHER'})
        other.clear()
        self.assertEqual(self.cache.load(), self.record)
        self.assertNotIn(str(self.path), self.cache._account)
        self.assertEqual(len(self.cache._account), len('profile-') + 64)
        self.assertNotIn(str(self.path), repr(self.cache))

    def test_duplicate_add_race_updates_exact_item_once_without_deleting(self):
        self.frameworks.statuses.update(update=[store._NOT_FOUND, 0], add=[store._DUPLICATE])
        self.cache.save(self.record)
        self.assertEqual(self.cache.load(), self.record)
        operations = [name for name, _ in self.frameworks.calls if name != 'ui']
        self.assertEqual(operations, ['update', 'add', 'update', 'load'])

    def test_locked_keychain_fails_before_any_item_operation(self):
        self.frameworks.unlocked = False
        for operation in (self.cache.load, lambda: self.cache.save(self.record), self.cache.clear):
            self.assert_safe_error(operation)
            self.assertEqual(self.frameworks.ui, 1)
        self.assertTrue(all(name == 'ui' for name, _ in self.frameworks.calls))

    def test_denied_cancelled_interaction_and_unavailable_never_mean_absent(self):
        for code in (-25293, -128, -25308, -25291):
            for name, operation in [('load', self.cache.load),
                                    ('update', lambda: self.cache.save(self.record)),
                                    ('clear', self.cache.clear), ('default', self.cache.load)]:
                with self.subTest(code=code, operation=name):
                    self.frameworks.statuses[name] = [code]
                    self.assert_safe_error(operation)
                    self.assertEqual(self.frameworks.ui, 1)
        self.assertFalse(any(name == 'add' for name, _ in self.frameworks.calls))

    def test_failed_update_preserves_existing_item_and_restores_prior_ui(self):
        self.frameworks.ui = 0
        self.cache.save(self.record)
        self.frameworks.statuses['update'] = [-25293]
        self.assert_safe_error(lambda: self.cache.save({'refresh_token': 'SYNTHETIC-NEW'}))
        self.assertEqual(self.cache.load(), self.record)
        self.assertEqual(self.frameworks.ui, 0)

    def test_ui_guard_failure_never_starts_keychain_work_and_restore_failure_surfaces(self):
        for where in ('get_ui', 'set_ui'):
            self.frameworks.statuses[where] = [-25293]
            self.assert_safe_error(self.cache.load)
        self.assertTrue(all(name == 'ui' for name, _ in self.frameworks.calls))
        self.frameworks.statuses['set_ui'] = [0, -25293]
        self.assert_safe_error(self.cache.load)

    def test_wrong_native_type_oversize_and_invalid_json_fail_closed(self):
        for value in ('not CFData', b'', b'[]', b'{', b'{"a":1,"a":2}',
                      b'{"a":NaN}', b'{"a":Infinity}', b'\xff', b'x' * (store._MAX_BYTES + 1)):
            with self.subTest(value_type=type(value), size=len(value)):
                self.frameworks.records[(store._SERVICE, self.cache._account)] = value
                self.assert_safe_error(self.cache.load)
                self.assertEqual(self.frameworks.ui, 1)

    def test_invalid_save_does_not_reach_keychain(self):
        for value in ([], None, {'a': float('nan')}, {'a': object()},
                      {'a': 'x' * (store._MAX_BYTES + 1)}):
            self.assert_safe_error(lambda: self.cache.save(value))
        self.assertEqual(self.frameworks.calls, [])

    def test_backend_exceptions_never_leak_secrets_or_chains(self):
        for name, operation in [('load', self.cache.load), ('save', lambda: self.cache.save(self.record)),
                                ('clear', self.cache.clear)]:
            with patch.object(self.cache._keychain, name, side_effect=ValueError('SYNTHETIC-TOKEN')):
                self.assert_safe_error(operation)

    def test_native_backend_rejects_other_platform_before_loading_frameworks(self):
        with patch.object(store.sys, 'platform', 'linux'), patch.object(store.ctypes, 'CDLL') as dll:
            with self.assertRaises(store.TokenStoreError) as caught:
                store.MacOSKeychain()
            self.assertEqual(caught.exception.code, 'platform')
            dll.assert_not_called()

    def test_platform_factory_is_lazy_and_preserves_explicit_profile(self):
        with patch.object(windows_token_store.sys, 'platform', 'darwin'), \
                patch.object(store, 'MacOSTokenStore') as mac:
            self.assertIs(windows_token_store.SecureTokenStore(self.path), mac.return_value)
            mac.assert_called_once_with(self.path)
        with patch.object(windows_token_store.sys, 'platform', 'win32'), \
                patch.object(windows_token_store, 'WindowsTokenStore') as win:
            self.assertIs(windows_token_store.SecureTokenStore(self.path), win.return_value)
            win.assert_called_once_with(self.path)
        with patch.object(windows_token_store.sys, 'platform', 'linux'):
            with self.assertRaises(store.TokenStoreError) as caught:
                windows_token_store.SecureTokenStore(self.path)
            self.assertEqual(caught.exception.code, 'platform')

    def test_mac_profile_and_default_store_identity_match_explicit_application_path(self):
        with patch.object(application_mode.sys, 'platform', 'darwin'), \
                patch.object(application_mode.Path, 'home', return_value=Path(self.folder.name)), \
                patch.dict(os.environ, {'XDG_DATA_HOME': '/unused', 'LOCALAPPDATA': '/unused'}):
            profile = application_mode.default_profile_root()
            self.assertEqual(profile, Path(self.folder.name) / 'Library' / 'Application Support' /
                             'CacheCADReconstructed' / 'v1')
            config = application_mode.ApplicationConfig(profile, profile / 'client.json')
            self.assertEqual(config.session_path.name, 'google-session.keychain')
            implicit = store.MacOSTokenStore(keychain=Mock())
            explicit = store.MacOSTokenStore(config.session_path, keychain=Mock())
            self.assertEqual(implicit._account, explicit._account)
        self.assertFalse(profile.exists())

    def test_google_session_uses_platform_factory_when_store_not_injected(self):
        from maintained.core import google_session
        from tests.test_desktop_signin import CONFIG
        if google_session._Credentials is None:
            self.skipTest('Google runtime is absent')
        with patch.object(google_session, 'SecureTokenStore') as factory:
            session = google_session.GoogleSession(CONFIG)
            self.assertIs(session._store, factory.return_value)
            factory.assert_called_once_with()
            session.close()


@unittest.skipUnless(sys.platform == 'darwin' and os.environ.get('GITHUB_ACTIONS') == 'true'
                     and os.environ.get('CACHECAD_TEST_MACOS_KEYCHAIN') == '1',
                     'Native synthetic item requires explicit disposable macOS CI opt-in')
class ActualMacOSKeychainTests(unittest.TestCase):
    def test_actual_keychain_disposable_synthetic_roundtrip_rotation_and_clear(self):
        # The randomized temporary profile hashes to a unique app-owned item.
        # Never change/default/create/unlock a Keychain or alter an item ACL.
        with tempfile.TemporaryDirectory(prefix='cachecad-keychain-ci-') as folder:
            path = Path(folder) / 'synthetic.keychain'
            cache = store.MacOSTokenStore(path)
            try:
                self.assertIsNone(cache.load())
                cache.save({'refresh_token': 'SYNTHETIC-CI-NOT-A-CREDENTIAL', 'schema': 1})
                self.assertEqual(cache.load()['schema'], 1)
                cache.save({'refresh_token': 'SYNTHETIC-CI-ROTATED', 'schema': 1})
                self.assertEqual(cache.load()['refresh_token'], 'SYNTHETIC-CI-ROTATED')
                self.assertFalse(path.exists())
            finally:
                cache.clear()
            cache.clear()
            self.assertIsNone(cache.load())


if __name__ == '__main__':
    unittest.main()
