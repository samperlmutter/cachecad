"""Fail-closed process-lifetime exclusion for one isolated application profile.

The lock contains no credentials. Never delete it: unlinking a held lock permits
another process to lock a different file at the same path. File handles are not
inherited. The OS releases the lock on normal close, exception, or process exit.
"""
import ctypes
from ctypes import wintypes
import errno
import os
from pathlib import Path
import stat


class ProfileLockError(Exception):
    def __init__(self, code='path'):
        self.code = code if code in ('busy', 'path', 'platform') else 'path'
        messages = {'busy': 'CacheCAD is already running with this application profile.',
                    'path': 'The application profile could not be locked safely.',
                    'platform': 'Profile locking is unavailable on this platform.'}
        super().__init__(messages[self.code])


def _check_path(path):
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ProfileLockError('path')


def _windows_open(path):
    """Open the final path without following a Windows reparse point."""
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                  ctypes.c_void_p, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL

    class AttributeTag(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('reparse_tag', wintypes.DWORD)]

    # Share read/write for another instance's lock attempt; do not share delete.
    # OPEN_ALWAYS does not truncate. OPEN_REPARSE_POINT avoids final symlink follow.
    handle = kernel.CreateFileW(str(path), 0xC0000000, 0x3, None, 4, 0x00200080, None)
    if handle == ctypes.c_void_p(-1).value:
        code = 'busy' if ctypes.get_last_error() in (32, 33) else 'path'
        raise ProfileLockError(code)
    try:
        info = AttributeTag()
        if (not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info),
                                                    ctypes.sizeof(info))
                or info.attributes & (0x400 | 0x10)):
            raise ProfileLockError('path')
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        handle = None  # open_osfhandle owns it now.
        return descriptor
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)


class ProfileLock:
    def __init__(self, profile_root):
        self.root = Path(profile_root).absolute()
        self.path = self.root / '.cachecad-profile.lock'
        self._fd = None

    @property
    def acquired(self):
        return self._fd is not None

    def acquire(self):
        if self._fd is not None:
            return self
        descriptor = None
        failure = None
        try:
            _check_path(self.path)
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _check_path(self.path)
            if os.name == 'nt':
                descriptor = _windows_open(self.path)
            elif os.name == 'posix' and hasattr(os, 'O_NOFOLLOW'):
                descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            else:
                raise ProfileLockError('platform')
            os.set_inheritable(descriptor, False)
            _check_path(self.path)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ProfileLockError('path')
            if os.name == 'nt':
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Keep a byte for Windows byte-range locking; never truncate existing files.
            if info.st_size == 0:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, b'\x00')
                os.fsync(descriptor)
            current = self.path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise ProfileLockError('path')
            self._fd, descriptor = descriptor, None
            return self
        except ProfileLockError as error:
            failure = error.code
        except OSError as error:
            failure = 'busy' if error.errno in (errno.EACCES, errno.EAGAIN) else 'path'
        except Exception:
            failure = 'path'
        finally:
            if descriptor is not None:
                os.close(descriptor)
        raise ProfileLockError(failure)

    def close(self):
        descriptor, self._fd = self._fd, None
        if descriptor is not None:
            # Closing the last process-owned handle releases either OS lock.
            os.close(descriptor)

    release = close

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *unused):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
