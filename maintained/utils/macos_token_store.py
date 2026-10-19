"""App/profile-owned Google sessions in the current user's macOS Keychain.

Direct Security.framework calls only: no shell, plaintext files, imported caches,
Keychain creation/unlock, access-list changes, or synchronization. The existing
file-based Keychain supports unsigned builds without provisioning entitlements.
Its default item ACL is retained. A changed app signature can therefore require
user repair; denied/locked access fails closed rather than widening that ACL.

SecItem uses the file-based implementation unless data protection or syncing is
requested (Apple TN3137). Deprecated SecKeychain APIs are used only to select the
user-domain default and suppress process-local UI, not to change system settings.
https://developer.apple.com/documentation/technotes/tn3137-on-mac-keychains
"""
from contextlib import contextmanager, ExitStack
import ctypes
import hashlib
import json
from pathlib import Path
import sys
import threading

from .windows_token_store import TokenStoreError, _unique

_SERVICE = 'CacheCAD.Reconstructed.GoogleSession.v1'
_MAX_BYTES = 131072
_NOT_FOUND = -25300
_DUPLICATE = -25299
_KEYCHAIN_LOCK = threading.RLock()


class MacOSKeychain:
    """Minimal native binding; every query has an exact service/account/keychain."""

    def __init__(self):
        if sys.platform != 'darwin':
            raise TokenStoreError('platform')
        failed = False
        try:
            self._security = ctypes.CDLL(
                '/System/Library/Frameworks/Security.framework/Security')
            self._cf = ctypes.CDLL(
                '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
            ptr = ctypes.c_void_p
            out = ctypes.POINTER(ptr)
            self._bind(self._security, 'SecKeychainCopyDomainDefault', [ctypes.c_int, out],
                       ctypes.c_int32)
            self._bind(self._security, 'SecKeychainGetStatus',
                       [ptr, ctypes.POINTER(ctypes.c_uint32)], ctypes.c_int32)
            self._bind(self._security, 'SecKeychainGetUserInteractionAllowed',
                       [ctypes.POINTER(ctypes.c_ubyte)], ctypes.c_int32)
            self._bind(self._security, 'SecKeychainSetUserInteractionAllowed',
                       [ctypes.c_ubyte], ctypes.c_int32)
            self._bind(self._security, 'SecItemCopyMatching', [ptr, out], ctypes.c_int32)
            self._bind(self._security, 'SecItemAdd', [ptr, out], ctypes.c_int32)
            self._bind(self._security, 'SecItemUpdate', [ptr, ptr], ctypes.c_int32)
            self._bind(self._security, 'SecItemDelete', [ptr], ctypes.c_int32)
            self._bind(self._cf, 'CFRelease', [ptr], None)
            self._bind(self._cf, 'CFStringCreateWithBytes',
                       [ptr, ptr, ctypes.c_long, ctypes.c_uint32, ctypes.c_ubyte], ptr)
            self._bind(self._cf, 'CFDataCreate', [ptr, ptr, ctypes.c_long], ptr)
            self._bind(self._cf, 'CFDataGetLength', [ptr], ctypes.c_long)
            self._bind(self._cf, 'CFDataGetBytePtr', [ptr], ptr)
            self._bind(self._cf, 'CFDataGetTypeID', [], ctypes.c_ulong)
            self._bind(self._cf, 'CFGetTypeID', [ptr], ctypes.c_ulong)
            self._bind(self._cf, 'CFDictionaryCreate',
                       [ptr, out, out, ctypes.c_long, ptr, ptr], ptr)
            self._bind(self._cf, 'CFArrayCreate', [ptr, out, ctypes.c_long, ptr], ptr)
            self._keys = {name: ptr.in_dll(self._security, name).value for name in (
                'kSecClass', 'kSecClassGenericPassword', 'kSecAttrService', 'kSecAttrAccount',
                'kSecAttrSynchronizable', 'kSecMatchSearchList', 'kSecUseKeychain',
                'kSecMatchLimit', 'kSecMatchLimitOne', 'kSecReturnData', 'kSecValueData')}
            self._true = ptr.in_dll(self._cf, 'kCFBooleanTrue').value
            self._false = ptr.in_dll(self._cf, 'kCFBooleanFalse').value
            self._dict_keys = ctypes.c_byte.in_dll(self._cf, 'kCFTypeDictionaryKeyCallBacks')
            self._dict_values = ctypes.c_byte.in_dll(self._cf, 'kCFTypeDictionaryValueCallBacks')
            self._array_values = ctypes.c_byte.in_dll(self._cf, 'kCFTypeArrayCallBacks')
            if not all(self._keys.values()) or not self._true or not self._false:
                raise ValueError
        except Exception:
            failed = True
        if failed:
            raise TokenStoreError()

    @staticmethod
    def _bind(library, name, arguments, result):
        method = getattr(library, name)
        method.argtypes, method.restype = arguments, result

    @staticmethod
    def _check(status):
        if status != 0:
            raise TokenStoreError()

    @contextmanager
    def _without_ui(self):
        # This is an in-process UI policy, not a persistent Keychain preference.
        # Serialize our calls and restore exactly the prior value on every exit.
        with _KEYCHAIN_LOCK:
            old = ctypes.c_ubyte()
            self._check(self._security.SecKeychainGetUserInteractionAllowed(ctypes.byref(old)))
            self._check(self._security.SecKeychainSetUserInteractionAllowed(0))
            try:
                yield
            finally:
                self._check(self._security.SecKeychainSetUserInteractionAllowed(old.value))

    def _own(self, stack, reference):
        if not reference:
            raise TokenStoreError()
        stack.callback(self._cf.CFRelease, reference)
        return reference

    def _dictionary(self, stack, values):
        keys = (ctypes.c_void_p * len(values))(*(self._keys[key] for key in values))
        items = (ctypes.c_void_p * len(values))(*values.values())
        return self._own(stack, self._cf.CFDictionaryCreate(
            None, keys, items, len(values), ctypes.byref(self._dict_keys),
            ctypes.byref(self._dict_values)))

    def _string(self, stack, value):
        encoded = value.encode('utf-8')
        return self._own(stack, self._cf.CFStringCreateWithBytes(
            None, encoded, len(encoded), 0x08000100, 0))  # kCFStringEncodingUTF8

    def _perform(self, operation, service, account, payload=None):
        with self._without_ui(), ExitStack() as stack:
            keychain = ctypes.c_void_p()
            # kSecPreferencesDomainUser = 0, never the System/common search list.
            status = self._security.SecKeychainCopyDomainDefault(0, ctypes.byref(keychain))
            if keychain.value:
                self._own(stack, keychain.value)
            self._check(status)
            if not keychain.value:
                raise TokenStoreError()
            state = ctypes.c_uint32()
            self._check(self._security.SecKeychainGetStatus(keychain, ctypes.byref(state)))
            if not state.value & 1:  # kSecUnlockStateStatus
                raise TokenStoreError()
            keychains = (ctypes.c_void_p * 1)(keychain.value)
            search = self._own(stack, self._cf.CFArrayCreate(
                None, keychains, 1, ctypes.byref(self._array_values)))
            identity = {'kSecClass': self._keys['kSecClassGenericPassword'],
                        'kSecAttrService': self._string(stack, service),
                        'kSecAttrAccount': self._string(stack, account),
                        'kSecAttrSynchronizable': self._false}
            query = dict(identity, kSecMatchSearchList=search)
            if operation == 'load':
                query.update(kSecReturnData=self._true,
                             kSecMatchLimit=self._keys['kSecMatchLimitOne'])
                result = ctypes.c_void_p()
                status = self._security.SecItemCopyMatching(
                    self._dictionary(stack, query), ctypes.byref(result))
                if result.value:
                    self._own(stack, result.value)
                if status == _NOT_FOUND:
                    return None
                self._check(status)
                if (not result.value or self._cf.CFGetTypeID(result) !=
                        self._cf.CFDataGetTypeID()):
                    raise TokenStoreError()
                size = self._cf.CFDataGetLength(result)
                pointer = self._cf.CFDataGetBytePtr(result)
                if not 1 <= size <= _MAX_BYTES or not pointer:
                    raise TokenStoreError()
                return ctypes.string_at(pointer, size)
            if operation == 'clear':
                status = self._security.SecItemDelete(self._dictionary(stack, query))
                if status != _NOT_FOUND:
                    self._check(status)
                return None
            if operation != 'save' or not isinstance(payload, bytes) or not 1 <= len(payload) <= _MAX_BYTES:
                raise TokenStoreError()
            # The only new native plaintext copy is released with its CFData.
            data = self._own(stack, self._cf.CFDataCreate(None, payload, len(payload)))
            change = self._dictionary(stack, {'kSecValueData': data})
            match = self._dictionary(stack, query)
            status = self._security.SecItemUpdate(match, change)
            if status == _NOT_FOUND:
                attributes = dict(identity, kSecUseKeychain=keychain.value, kSecValueData=data)
                status = self._security.SecItemAdd(self._dictionary(stack, attributes), None)
                if status == _DUPLICATE:
                    # A competing app instance added this exact item. Never delete it.
                    status = self._security.SecItemUpdate(match, change)
            self._check(status)
        return None

    def load(self, service, account):
        return self._perform('load', service, account)

    def save(self, service, account, payload):
        self._perform('save', service, account, payload)

    def clear(self, service, account):
        self._perform('clear', service, account)


class MacOSTokenStore:
    """The supplied path is an identity only; no token file is ever created/read."""

    def __init__(self, path=None, *, keychain=None):
        failed = False
        try:
            if path is None:
                from .application_mode import default_profile_root
                path = default_profile_root() / 'app_data' / 'google-session.keychain'
            # Resolve only the owned profile identity, never an existing token cache.
            identity = str(Path(path).expanduser().resolve(strict=False))
            self._account = 'profile-' + hashlib.sha256(identity.encode('utf-8')).hexdigest()
            self._keychain = keychain if keychain is not None else MacOSKeychain()
        except TokenStoreError:
            raise
        except Exception:
            failed = True
        if failed:
            raise TokenStoreError()

    def __repr__(self):
        return '<MacOSTokenStore protected>'

    def load(self):
        result, failed = None, False
        try:
            plain = self._keychain.load(_SERVICE, self._account)
            if plain is None:
                return None
            if not isinstance(plain, bytes) or not 1 <= len(plain) <= _MAX_BYTES:
                raise ValueError
            result = json.loads(plain.decode('utf-8'), object_pairs_hook=_unique,
                                parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
            if not isinstance(result, dict):
                raise ValueError
        except Exception:
            failed = True
        if failed:
            raise TokenStoreError()
        return result

    def save(self, record):
        failed = False
        try:
            if not isinstance(record, dict):
                raise ValueError
            payload = json.dumps(record, separators=(',', ':'), allow_nan=False).encode('utf-8')
            if not 1 <= len(payload) <= _MAX_BYTES:
                raise ValueError
            self._keychain.save(_SERVICE, self._account, payload)
        except Exception:
            failed = True
        if failed:
            raise TokenStoreError()

    def clear(self):
        failed = False
        try:
            self._keychain.clear(_SERVICE, self._account)
        except Exception:
            failed = True
        if failed:
            raise TokenStoreError()
