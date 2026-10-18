"""Current Windows user's DPAPI-encrypted, atomic Google session cache.

Only encrypted bytes ever reach a file. DPAPI protects confidentiality/integrity
at rest, not against software already executing as this Windows user. No fallback
plaintext storage, vendor-cache import, machine-wide DPAPI, or import-time I/O.
"""

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


_MAGIC = b'CacheCAD-DPAPI-v1\x00'
_ENTROPY = hashlib.sha256(b'CacheCAD maintained Google session schema 1').digest()
_MAX_BYTES = 262144


class TokenStoreError(Exception):
    def __init__(self, code='storage'):
        self.code = code if code in ('storage', 'platform') else 'storage'
        super().__init__('Windows protected sign-in storage is unavailable.' if self.code == 'platform'
                         else 'The protected sign-in cache could not be read or updated.')


class _DataBlob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


class WindowsDPAPI:
    """CryptProtectData/UnprotectData with CurrentUser and UI forbidden."""

    def __init__(self):
        if os.name != 'nt':
            raise TokenStoreError('platform')
        self._crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        self._kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        pointer = ctypes.POINTER(_DataBlob)
        self._crypt.CryptProtectData.argtypes = [pointer, wintypes.LPCWSTR, pointer,
                                                ctypes.c_void_p, ctypes.c_void_p,
                                                wintypes.DWORD, pointer]
        self._crypt.CryptUnprotectData.argtypes = [pointer, ctypes.c_void_p, pointer,
                                                  ctypes.c_void_p, ctypes.c_void_p,
                                                  wintypes.DWORD, pointer]
        self._crypt.CryptProtectData.restype = wintypes.BOOL
        self._crypt.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel.LocalFree.restype = ctypes.c_void_p

    def _convert(self, data, protect):
        if not isinstance(data, bytes) or not 1 <= len(data) <= _MAX_BYTES:
            raise TokenStoreError()
        source = ctypes.create_string_buffer(data)
        entropy = ctypes.create_string_buffer(_ENTROPY)
        incoming = _DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
        extra = _DataBlob(len(_ENTROPY), ctypes.cast(entropy, ctypes.POINTER(ctypes.c_ubyte)))
        outgoing = _DataBlob()
        operation = self._crypt.CryptProtectData if protect else self._crypt.CryptUnprotectData
        try:
            # CRYPTPROTECT_UI_FORBIDDEN only. LOCAL_MACHINE is deliberately absent.
            if not operation(ctypes.byref(incoming), None, ctypes.byref(extra), None, None,
                             0x1, ctypes.byref(outgoing)):
                raise TokenStoreError()
            if not outgoing.pbData or not 1 <= outgoing.cbData <= _MAX_BYTES:
                raise TokenStoreError()
            return ctypes.string_at(outgoing.pbData, outgoing.cbData)
        finally:
            ctypes.memset(source, 0, len(source))
            if outgoing.pbData:
                ctypes.memset(outgoing.pbData, 0, outgoing.cbData)
                self._kernel.LocalFree(outgoing.pbData)

    def protect(self, data):
        return self._convert(data, True)

    def unprotect(self, data):
        return self._convert(data, False)


def default_cache_path():
    if os.name != 'nt':
        raise TokenStoreError('platform')
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE,
                                     wintypes.DWORD, wintypes.LPWSTR]
    shell.SHGetFolderPathW.restype = ctypes.c_long
    output = ctypes.create_unicode_buffer(32768)
    # CSIDL_LOCAL_APPDATA, current user, no environment-variable fallback.
    if shell.SHGetFolderPathW(None, 0x001C, None, 0, output) != 0 or not output.value:
        raise TokenStoreError()
    return Path(output.value) / 'CacheCAD' / 'Maintained' / 'google-session.dpapi'


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_links(path):
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise TokenStoreError()


class WindowsTokenStore:
    """One explicit app-owned path; injected protector is for offline tests only."""

    def __init__(self, path=None, *, protector=None):
        self.path = Path(path) if path is not None else default_cache_path()
        self._protector = protector if protector is not None else WindowsDPAPI()

    def __repr__(self):
        return '<WindowsTokenStore protected>'

    def load(self):
        result = None
        failed = False
        try:
            _reject_links(self.path)
            if not self.path.exists():
                return None
            if not stat.S_ISREG(self.path.stat().st_mode):
                raise ValueError
            with self.path.open('rb') as stream:
                raw = stream.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES or not raw.startswith(_MAGIC):
                raise ValueError
            plain = self._protector.unprotect(raw[len(_MAGIC):])
            if len(plain) > _MAX_BYTES:
                raise ValueError
            result = json.loads(plain.decode('utf-8'), object_pairs_hook=_unique)
            if not isinstance(result, dict):
                raise ValueError
        except Exception:
            failed = True
        if failed:
            raise TokenStoreError()
        return result

    def save(self, record):
        temporary = None
        failed = False
        try:
            if not isinstance(record, dict):
                raise ValueError
            plain = json.dumps(record, separators=(',', ':'), allow_nan=False).encode('utf-8')
            if len(plain) > _MAX_BYTES // 2:
                raise ValueError
            protected = self._protector.protect(plain)
            if not isinstance(protected, bytes) or not protected:
                raise ValueError
            payload = _MAGIC + protected
            if len(payload) > _MAX_BYTES:
                raise ValueError
            # Encrypt before opening any file. Temporary files contain ciphertext only.
            _reject_links(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _reject_links(self.path)
            descriptor, temporary = tempfile.mkstemp(prefix='.google-session-', suffix='.tmp',
                                                     dir=self.path.parent)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except Exception:
            failed = True
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except OSError:
                    pass
        if failed:
            raise TokenStoreError()

    def clear(self):
        failed = False
        try:
            _reject_links(self.path)
            self.path.unlink(missing_ok=True)
        except Exception:
            failed = True
        if failed:
            raise TokenStoreError()


SecureTokenStore = WindowsTokenStore
