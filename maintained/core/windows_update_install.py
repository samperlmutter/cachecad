"""Fail-closed Windows update verification and cooperative install exclusion.

No network-supplied command, signer name, executable argument or trust override is
accepted here. Certificate pins come from the reviewed bundled owner policy.
WinVerifyTrust authenticates the *held file handle*, and its successful provider
state supplies the actual signer certificate. Holding the file and its directory
chain without write/delete sharing narrows path substitution races; CreateProcess
still takes a path, so this is not an atomic handle-to-execution primitive or a
defence against arbitrary code already running as the same Windows user.

The three global named objects intentionally cover every profile and session of
this product. Failure to access an object is a refusal, never evidence of absence.
The installer must implement the matching Install/Running protocol in cachecad.iss.
"""
import ctypes
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid


_PREFIX = 'Global\\CacheCADReconstructed.D24539B1-3941-4273-9C44-A62A05A91623.'
RUNNING_MUTEX = _PREFIX + 'Running'
INSTALL_MUTEX = _PREFIX + 'Install'
HANDOFF_MUTEX = _PREFIX + 'Handoff'
# Interactive installation, no forced application close, reboot or auto-launch.
INSTALLER_ARGUMENTS = ('/NORESTART', '/NOCLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_HANDLE = ctypes.c_void_p
_POINTER = ctypes.c_void_p
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_SYNCHRONIZE = 0x00100000


class UpdateInstallError(Exception):
    """Sanitized, stable failure codes; no local paths or certificate data."""
    _messages = {
        'platform': 'Verified application installation requires Windows.',
        'policy': 'The application publisher has not configured update signing trust.',
        'path': 'The update file could not be opened safely.',
        'integrity': 'The update file does not match its authenticated release.',
        'signature': 'Windows could not verify the update signature and timestamp.',
        'publisher': 'The update was not signed by an approved publisher certificate.',
        'busy': 'Close the other CacheCAD windows before installing this update.',
        'gate': 'Application installation is already active or could not be locked safely.',
        'state': 'The verified update is no longer available.',
        'process': 'The old application process could not be identified or did not exit.',
        'launch': 'The verified installer could not be started.',
        'handoff': 'The update helper did not confirm a safe handoff.',
    }

    def __init__(self, code):
        self.code = code if code in self._messages else 'state'
        super().__init__(self._messages[self.code])


@dataclass(frozen=True)
class SignerPolicy:
    approved_certificate_sha256: frozenset

    def __post_init__(self):
        pins = self.approved_certificate_sha256
        if (not isinstance(pins, (frozenset, tuple, list, set)) or not pins
                or len(pins) > 16 or any(not isinstance(p, str) or not _SHA256.fullmatch(p)
                                       for p in pins)):
            raise UpdateInstallError('policy')
        object.__setattr__(self, 'approved_certificate_sha256', frozenset(pins))


def _require_windows():
    if sys.platform != 'win32' or os.name != 'nt':
        raise UpdateInstallError('platform')


def _system_dll(name):
    _require_windows()
    # LOAD_LIBRARY_SEARCH_SYSTEM32, never a DLL alongside a downloaded installer.
    return ctypes.WinDLL(name, use_last_error=True, winmode=0x00000800)


def _require_non_elevated():
    """Per-user updates never inherit administrator authority or request UAC."""
    kernel, security = _system_dll('kernel32.dll'), _system_dll('advapi32.dll')
    kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], _HANDLE
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [_HANDLE], _BOOL
    security.OpenProcessToken.argtypes = [_HANDLE, _DWORD, ctypes.POINTER(_HANDLE)]
    security.OpenProcessToken.restype = _BOOL
    security.GetTokenInformation.argtypes = [_HANDLE, ctypes.c_int, _POINTER,
                                             _DWORD, ctypes.POINTER(_DWORD)]
    security.GetTokenInformation.restype = _BOOL
    token, elevated, returned = _HANDLE(), _DWORD(), _DWORD()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
        raise UpdateInstallError('process')
    try:
        if (not security.GetTokenInformation(token, 20, ctypes.byref(elevated),
                                             ctypes.sizeof(elevated), ctypes.byref(returned))
                or returned.value != ctypes.sizeof(elevated) or elevated.value):
            raise UpdateInstallError('process')
    finally:
        kernel.CloseHandle(token)


class _FileTime(ctypes.Structure):
    _fields_ = [('low', _DWORD), ('high', _DWORD)]


class _FileInformation(ctypes.Structure):
    _fields_ = [('attributes', _DWORD), ('created', _FileTime),
                ('accessed', _FileTime), ('written', _FileTime),
                ('volume', _DWORD), ('size_high', _DWORD), ('size_low', _DWORD),
                ('links', _DWORD), ('index_high', _DWORD), ('index_low', _DWORD)]


class _Kernel:
    def __init__(self):
        self.dll = _system_dll('kernel32.dll')
        signatures = {
            'CreateFileW': ([ctypes.c_wchar_p, _DWORD, _DWORD, _POINTER, _DWORD,
                             _DWORD, _HANDLE], _HANDLE),
            'CloseHandle': ([_HANDLE], _BOOL),
            'GetFileInformationByHandle': ([_HANDLE, ctypes.POINTER(_FileInformation)], _BOOL),
            'SetFilePointerEx': ([_HANDLE, ctypes.c_int64, _POINTER, _DWORD], _BOOL),
            'ReadFile': ([_HANDLE, _POINTER, _DWORD, ctypes.POINTER(_DWORD), _POINTER], _BOOL),
            'CreateMutexW': ([_POINTER, _BOOL, ctypes.c_wchar_p], _HANDLE),
            'OpenMutexW': ([_DWORD, _BOOL, ctypes.c_wchar_p], _HANDLE),
            'ReleaseMutex': ([_HANDLE], _BOOL),
            'WaitForSingleObject': ([_HANDLE, _DWORD], _DWORD),
            'OpenProcess': ([_DWORD, _BOOL, _DWORD], _HANDLE),
            'GetProcessTimes': ([_HANDLE, ctypes.POINTER(_FileTime),
                                ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
                                ctypes.POINTER(_FileTime)], _BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.dll, name)
            function.argtypes, function.restype = args, result
            setattr(self, name, function)

    def information(self, handle):
        info = _FileInformation()
        if not self.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise UpdateInstallError('path')
        return info

    def present(self, name):
        ctypes.set_last_error(0)
        handle = self.OpenMutexW(_SYNCHRONIZE, False, name)
        if handle:
            self.CloseHandle(handle)
            return True
        if ctypes.get_last_error() == 2:  # ERROR_FILE_NOT_FOUND only.
            return False
        raise UpdateInstallError('gate')


def _validated_path(value):
    text = os.fspath(value)
    if not isinstance(text, str) or '\x00' in text:
        raise UpdateInstallError('path')
    drive, tail = ntpath.splitdrive(text)
    if (not re.fullmatch(r'[A-Za-z]:', drive) or not tail.startswith(('\\', '/'))
            or any(p in ('.', '..') or p.endswith((' ', '.')) or ':' in p
                   for p in re.split(r'[\\/]+', tail) if p)):
        # Refuse UNC/device paths, alternate data streams and lexical aliases.
        raise UpdateInstallError('path')
    return Path(ntpath.normpath(text))


class _PinnedFile:
    """Own the read handle and non-reparse ancestor handles until close."""
    def __init__(self, path):
        self.path = _validated_path(path)
        self.kernel = _Kernel()
        self.handles = []
        self.handle = None
        try:
            # Top-down pinning prevents an opened parent being renamed while a
            # descendant is checked. The root itself cannot be renamed.
            for parent in reversed(self.path.parents):
                handle = self.kernel.CreateFileW(str(parent), 0x80, 0x1, None, 3,
                                                0x02200000, None)
                if handle in (None, _INVALID_HANDLE):
                    raise UpdateInstallError('path')
                self.handles.append(handle)
                info = self.kernel.information(handle)
                if info.attributes & 0x400 or not info.attributes & 0x10:
                    raise UpdateInstallError('path')
            handle = self.kernel.CreateFileW(str(self.path), 0x80000000, 0x1,
                                            None, 3, 0x00200000, None)
            if handle in (None, _INVALID_HANDLE):
                raise UpdateInstallError('path')
            self.handles.append(handle)
            self.handle = handle
            self.info = self.kernel.information(handle)
            if self.info.attributes & (0x400 | 0x10) or self.info.links != 1:
                raise UpdateInstallError('path')
        except BaseException:
            self.close()
            raise

    def digest(self, expected_length):
        if self.handle is None:
            raise UpdateInstallError('state')
        info = self.kernel.information(self.handle)
        if ((info.size_high << 32) | info.size_low) != expected_length:
            raise UpdateInstallError('integrity')
        if not self.kernel.SetFilePointerEx(self.handle, 0, None, 0):
            raise UpdateInstallError('path')
        digest = hashlib.sha256()
        buffer = ctypes.create_string_buffer(1024 * 1024)
        total = 0
        while True:
            count = _DWORD()
            if not self.kernel.ReadFile(self.handle, buffer, len(buffer), ctypes.byref(count), None):
                raise UpdateInstallError('path')
            if not count.value:
                break
            total += count.value
            if total > expected_length:
                raise UpdateInstallError('integrity')
            digest.update(buffer.raw[:count.value])
        if total != expected_length:
            raise UpdateInstallError('integrity')
        return digest.hexdigest()

    def close(self):
        self.handle = None
        for handle in reversed(self.handles):
            self.kernel.CloseHandle(handle)
        self.handles.clear()

    def copy_to(self, destination):
        """Copy the held object rather than re-opening a replaceable source path."""
        if self.handle is None or not self.kernel.SetFilePointerEx(self.handle, 0, None, 0):
            raise UpdateInstallError('state')
        buffer = ctypes.create_string_buffer(1024 * 1024)
        with Path(destination).open('xb') as output:
            while True:
                count = _DWORD()
                if not self.kernel.ReadFile(self.handle, buffer, len(buffer),
                                            ctypes.byref(count), None):
                    raise UpdateInstallError('path')
                if not count.value:
                    break
                output.write(buffer.raw[:count.value])
            output.flush()
            os.fsync(output.fileno())


class _Guid(ctypes.Structure):
    _fields_ = [('data1', _DWORD), ('data2', ctypes.c_uint16),
                ('data3', ctypes.c_uint16), ('data4', ctypes.c_ubyte * 8)]


class _WintrustFile(ctypes.Structure):
    _fields_ = [('size', _DWORD), ('path', ctypes.c_wchar_p),
                ('handle', _HANDLE), ('subject', _POINTER)]


class _SignatureSettings(ctypes.Structure):
    _fields_ = [('size', _DWORD), ('index', _DWORD), ('flags', _DWORD),
                ('secondary_count', _DWORD), ('verified_index', _DWORD),
                ('crypto_policy', _POINTER)]


class _WintrustData(ctypes.Structure):
    _fields_ = [('size', _DWORD), ('policy', _POINTER), ('sip', _POINTER),
                ('ui', _DWORD), ('revocation', _DWORD), ('choice', _DWORD),
                ('file', ctypes.POINTER(_WintrustFile)), ('action', _DWORD),
                ('state', _HANDLE), ('url', ctypes.c_wchar_p), ('flags', _DWORD),
                ('context', _DWORD), ('signatures', ctypes.POINTER(_SignatureSettings))]


class _Blob(ctypes.Structure):
    _fields_ = [('size', _DWORD), ('data', _POINTER)]


class _Algorithm(ctypes.Structure):
    _fields_ = [('oid', ctypes.c_char_p), ('parameters', _Blob)]


class _SignerInfoPrefix(ctypes.Structure):
    _fields_ = [('version', _DWORD), ('issuer', _Blob), ('serial', _Blob),
                ('digest_algorithm', _Algorithm)]


class _CertContext(ctypes.Structure):
    _fields_ = [('encoding', _DWORD), ('encoded', _POINTER), ('length', _DWORD),
                ('info', _POINTER), ('store', _HANDLE)]


class _ProviderCert(ctypes.Structure):
    _fields_ = [('size', _DWORD), ('certificate', ctypes.POINTER(_CertContext)),
                ('commercial', _BOOL), ('trusted_root', _BOOL), ('self_signed', _BOOL),
                ('test', _BOOL), ('revoked_reason', _DWORD), ('confidence', _DWORD),
                ('error', _DWORD), ('trust_list', _POINTER), ('trust_list_signer', _BOOL),
                ('ctl', _POINTER), ('ctl_error', _DWORD), ('cyclic', _BOOL),
                ('chain_element', _POINTER)]


class _ProviderSigner(ctypes.Structure):
    pass


_ProviderSigner._fields_ = [
    ('size', _DWORD), ('verified_at', _FileTime), ('certificate_count', _DWORD),
    ('certificates', ctypes.POINTER(_ProviderCert)), ('signer_type', _DWORD),
    ('signer', ctypes.POINTER(_SignerInfoPrefix)), ('error', _DWORD),
    ('counter_count', _DWORD), ('counter_signers', ctypes.POINTER(_ProviderSigner)),
    ('chain_context', _POINTER),
]


def _trusted_certificate_hash(pinned):
    """Return SHA-256 of the primary trusted leaf certificate DER.

    Policy: Authenticode generic verify V2, SHA-256 content digest, noninteractive,
    online-capable revocation checking for the whole non-root chain, Windows
    downloaded-file policy, and a verified SHA-256 timestamp countersigner.
    Offline/unavailable revocation data and missing timestamp both defer updates.
    A timestamp is evaluated by Windows policy, not by parsing displayed text.
    """
    trust = _system_dll('wintrust.dll')
    trust.WinVerifyTrust.argtypes = [_HANDLE, ctypes.POINTER(_Guid), ctypes.POINTER(_WintrustData)]
    trust.WinVerifyTrust.restype = ctypes.c_int32  # LONG; success is exactly zero.
    trust.WTHelperProvDataFromStateData.argtypes = [_HANDLE]
    trust.WTHelperProvDataFromStateData.restype = _POINTER
    trust.WTHelperGetProvSignerFromChain.argtypes = [_POINTER, _DWORD, _BOOL, _DWORD]
    trust.WTHelperGetProvSignerFromChain.restype = ctypes.POINTER(_ProviderSigner)
    trust.WTHelperGetProvCertFromChain.argtypes = [ctypes.POINTER(_ProviderSigner), _DWORD]
    trust.WTHelperGetProvCertFromChain.restype = ctypes.POINTER(_ProviderCert)
    action = _Guid.from_buffer_copy(uuid.UUID('00AAC56B-CD44-11d0-8CC2-00C04FC295EE').bytes_le)
    file_info = _WintrustFile(ctypes.sizeof(_WintrustFile), str(pinned.path), pinned.handle, None)
    signatures = _SignatureSettings(ctypes.sizeof(_SignatureSettings), 0, 1, 0, 0, None)
    data = _WintrustData()
    data.size, data.ui, data.revocation, data.choice = ctypes.sizeof(data), 2, 1, 1
    data.file, data.action = ctypes.pointer(file_info), 1
    data.flags = 0x80 | 0x2000 | 0x4000
    data.context, data.signatures = 1, ctypes.pointer(signatures)
    try:
        if trust.WinVerifyTrust(_INVALID_HANDLE, ctypes.byref(action), ctypes.byref(data)) != 0:
            raise UpdateInstallError('signature')
        provider = trust.WTHelperProvDataFromStateData(data.state)
        if not provider or signatures.verified_index != 0:
            raise UpdateInstallError('signature')
        signer = trust.WTHelperGetProvSignerFromChain(provider, 0, False, 0)
        if (not signer or signer.contents.error or not signer.contents.certificate_count
                or not signer.contents.counter_count or not signer.contents.signer
                or signer.contents.signer.contents.digest_algorithm.oid != b'2.16.840.1.101.3.4.2.1'):
            raise UpdateInstallError('signature')
        counter = trust.WTHelperGetProvSignerFromChain(provider, 0, True, 0)
        if (not counter or counter.contents.error or not counter.contents.certificate_count
                or not counter.contents.signer
                or counter.contents.signer.contents.digest_algorithm.oid != b'2.16.840.1.101.3.4.2.1'):
            raise UpdateInstallError('signature')
        certificate = trust.WTHelperGetProvCertFromChain(signer, 0)
        if not certificate or certificate.contents.error or not certificate.contents.certificate:
            raise UpdateInstallError('signature')
        leaf = certificate.contents.certificate.contents
        if not leaf.encoded or not 0 < leaf.length <= 1024 * 1024:
            raise UpdateInstallError('signature')
        return hashlib.sha256(ctypes.string_at(leaf.encoded, leaf.length)).hexdigest()
    finally:
        data.action = 2  # WTD_STATEACTION_CLOSE, including failed verification.
        trust.WinVerifyTrust(_INVALID_HANDLE, ctypes.byref(action), ctypes.byref(data))


class VerifiedInstaller:
    """A live, read-only verified file capability. Always close when dismissed."""
    def __init__(self):
        raise TypeError('Use VerifiedInstaller.open')

    @classmethod
    def open(cls, path, *, expected_sha256, expected_length, policy):
        _require_windows()
        if not isinstance(policy, SignerPolicy):
            raise UpdateInstallError('policy')
        if (not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256)
                or type(expected_length) is not int or not 0 < expected_length <= 1024**3):
            raise UpdateInstallError('integrity')
        pinned = None
        try:
            pinned = _PinnedFile(path)
            if not hmac.compare_digest(pinned.digest(expected_length), expected_sha256):
                raise UpdateInstallError('integrity')
            fingerprint = _trusted_certificate_hash(pinned)
            if fingerprint not in policy.approved_certificate_sha256:
                raise UpdateInstallError('publisher')
            result = object.__new__(cls)
            result._file, result.path = pinned, pinned.path
            result.sha256, result.length = expected_sha256, expected_length
            result.signer_certificate_sha256, result._policy = fingerprint, policy
            result._launched = False
            return result
        except BaseException:
            if pinned is not None:
                pinned.close()
            raise

    @property
    def closed(self):
        return self._file is None

    def reverify(self):
        if self.closed or self._launched:
            raise UpdateInstallError('state')
        if not hmac.compare_digest(self._file.digest(self.length), self.sha256):
            raise UpdateInstallError('integrity')
        fingerprint = _trusted_certificate_hash(self._file)
        if fingerprint not in self._policy.approved_certificate_sha256:
            raise UpdateInstallError('publisher')

    def close(self):
        pinned, self._file = self._file, None
        if pinned is not None:
            pinned.close()

    def __enter__(self):
        if self.closed:
            raise UpdateInstallError('state')
        return self

    def __exit__(self, *unused):
        self.close()

    def __del__(self):
        if hasattr(self, '_file'):
            self.close()


class _Mutex:
    def __init__(self, name, *, exclusive=False, must_be_new=False):
        self.kernel = _Kernel()
        self.handle, self.owned = None, False
        ctypes.set_last_error(0)
        handle = self.kernel.CreateMutexW(None, False, name)
        error = ctypes.get_last_error()
        if not handle:
            raise UpdateInstallError('gate')
        self.handle = handle
        try:
            if must_be_new and error == 183:
                raise UpdateInstallError('gate')
            if exclusive:
                result = self.kernel.WaitForSingleObject(handle, 0)
                # Abandonment releases ownership but is not a known healthy
                # installation state. Release our acquired ownership and refuse.
                if result == 0x80:
                    self.owned = True
                    raise UpdateInstallError('gate')
                if result != 0:
                    raise UpdateInstallError('gate')
                self.owned = True
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle is not None:
            if self.owned:
                self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle, self.owned = None, False

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


class ApplicationRunLease:
    """Hold through all application workers and shutdown, independent of profile."""
    def __init__(self):
        self._running = None

    def acquire(self):
        _require_windows()
        if self._running is not None:
            return self
        with _Mutex(INSTALL_MUTEX, exclusive=True) as gate:
            if gate.kernel.present(HANDOFF_MUTEX):
                raise UpdateInstallError('gate')
            self._running = _Mutex(RUNNING_MUTEX)
        return self

    def close(self):
        if self._running is not None:
            self._running.close()
            self._running = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *unused):
        self.close()


class HandoffLease:
    """Block new app starts until the staged helper and installer finish."""
    def __init__(self):
        self._presence = None

    def acquire(self):
        _require_windows()
        if self._presence is not None:
            return self
        with _Mutex(INSTALL_MUTEX, exclusive=True):
            self._presence = _Mutex(HANDOFF_MUTEX, must_be_new=True)
        return self

    def close(self):
        if self._presence is not None:
            self._presence.close()
            self._presence = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *unused):
        self.close()


class ParentProcess:
    """Open before READY; a stable handle and creation time prevent PID reuse."""
    def __init__(self, pid, expected_creation_time=None):
        _require_windows()
        if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
            raise UpdateInstallError('process')
        self.kernel, self.handle = _Kernel(), None
        self.handle = self.kernel.OpenProcess(_SYNCHRONIZE | 0x1000, False, pid)
        if not self.handle:
            raise UpdateInstallError('process')
        try:
            created, exited, kernel_time, user_time = (_FileTime() for _ in range(4))
            if not self.kernel.GetProcessTimes(self.handle, ctypes.byref(created),
                    ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                raise UpdateInstallError('process')
            self.creation_time = created.low | (created.high << 32)
            if expected_creation_time is not None and self.creation_time != expected_creation_time:
                raise UpdateInstallError('process')
        except BaseException:
            self.close()
            raise

    def wait_for_exit(self, timeout_ms=120000):
        if (type(timeout_ms) is not int or not 0 <= timeout_ms <= 300000
                or self.kernel.WaitForSingleObject(self.handle, timeout_ms) != 0):
            raise UpdateInstallError('process')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def launch_verified_installer(installer, handoff):
    """Run interactively after every old app has exited; wait without killing it.

    Return the actual Inno exit code, not an assertion of a healthy updated app.
    The installer acquires Install itself and rechecks Running before mutation.
    Handoff presence blocks app startup across this cooperative gate transfer.
    """
    _require_windows()
    _require_non_elevated()
    if (not isinstance(installer, VerifiedInstaller) or installer.closed
            or not isinstance(handoff, HandoffLease) or handoff._presence is None):
        raise UpdateInstallError('state')
    with _Mutex(INSTALL_MUTEX, exclusive=True) as gate:
        if gate.kernel.present(RUNNING_MUTEX):
            raise UpdateInstallError('busy')
        installer.reverify()
        try:
            process = subprocess.Popen([str(installer.path), *INSTALLER_ARGUMENTS],
                executable=str(installer.path), shell=False, close_fds=True,
                cwd=str(installer.path.parent), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            raise UpdateInstallError('launch') from None
        installer._launched = True
    # Never time out/kill an installer that may be mutating the installation.
    # Keep both Handoff and verified file handles alive throughout execution.
    return process.wait()


class HandoffProcess:
    """Parent-side pre-commit capability. EOF before COMMIT cancels safely."""
    def __init__(self, process, staged_verified, staging_dir):
        self.process, self._verified, self._directory = process, staged_verified, staging_dir
        self._committed = False
        self._ready = False
        self._aborted = False

    def wait_ready(self, *, cancel=None, timeout=120):
        received, lines = threading.Event(), []

        def read_reply():
            try:
                lines.append(self.process.stdout.readline(128))
            except (OSError, ValueError):
                lines.append(b'')
            finally:
                received.set()

        reader = threading.Thread(target=read_reply, name='update-helper-ready', daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        while not received.wait(0.05):
            if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                self.abort()
                reader.join(timeout=0.1)
                raise UpdateInstallError('handoff')
        reader.join(timeout=1)
        if (lines != [b'CACHECAD_UPDATE_READY\n'] or self.process.poll() is not None
                or (cancel is not None and cancel.is_set())):
            self.abort()
            raise UpdateInstallError('handoff')
        self._ready = True
        return self

    def commit(self):
        if not self._ready or self._committed or self._aborted or self.process.poll() is not None:
            raise UpdateInstallError('handoff')
        try:
            self.process.stdin.write(b'COMMIT\n')
            self.process.stdin.flush()
        except (OSError, ValueError):
            self.abort()
            raise UpdateInstallError('handoff') from None
        self._committed = True

    def abort(self):
        if self._committed:
            # A committed helper may already have launched a mutating installer.
            # Never terminate it or convert this state back into cancellation.
            raise UpdateInstallError('state')
        if self._aborted:
            return
        self._aborted, self._ready = True, False
        if self.process.stdin is not None:
            self.process.stdin.close()  # EOF irrevocably denies COMMIT.

        def reap_and_cleanup():
            try:
                # A onefile executable has a wrapper and a Python child. Killing
                # only the wrapper would orphan the child. EOF lets both exit;
                # blocked native verification may defer cleanup but never install.
                self.process.wait()
                self.close()
                shutil.rmtree(self._directory, ignore_errors=True)
            except Exception:
                pass

        if self.process.poll() is not None:
            reap_and_cleanup()
        else:
            threading.Thread(target=reap_and_cleanup, name='update-helper-cleanup',
                             daemon=True).start()

    def close(self):
        if self._aborted and self.process.poll() is None:
            # A separate reader may still own stdout's buffered IO lock. Do not
            # block the caller closing it while the native verifier is running.
            return
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()
        if self._verified is not None:
            self._verified.close()
            self._verified = None


def start_update_handoff(download, config, installed_root, profile, *, cache_dir,
                         old_pid=None, cancel=None, timeout=120):
    """Stage and verify the fixed helper; call only from a UI worker thread.

    READY is not installation permission. The caller must recheck its idle state,
    call commit after explicit user consent, and then complete a clean shutdown.
    Profile paths are retained only for local restart context, never passed to an
    installer. This first implementation does not auto-relaunch an unverified app.
    """
    _require_windows()
    if config is None:
        raise UpdateInstallError('policy')
    _require_non_elevated()
    policy = SignerPolicy(config.publisher_certificate_sha256)
    try:
        from core.update_helper_identity import (
            UPDATE_HELPER_FILENAME, UPDATE_HELPER_LENGTH, UPDATE_HELPER_SHA256)
    except ImportError:
        raise UpdateInstallError('handoff') from None
    if UPDATE_HELPER_FILENAME != 'CacheCADUpdateHelper.exe':
        raise UpdateInstallError('handoff')
    if cancel is not None and cancel.is_set():
        raise UpdateInstallError('handoff')
    cache = _validated_path(cache_dir)
    root = _validated_path(installed_root)
    # Cache must already be the application's prepared local cache. Never create
    # arbitrary parent directories from a ticket or choose a current directory.
    for component in (cache, *cache.parents):
        info = component.lstat()
        if not component.is_dir() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise UpdateInstallError('path')
    staging, verified, handle = None, None, None
    try:
        with ParentProcess(old_pid or os.getpid()) as parent:
            ticket = {'schema': 1, 'parent_pid': old_pid or os.getpid(),
                      'parent_creation_time': parent.creation_time,
                      'installer_path': str(_validated_path(download.path)),
                      'offer': asdict(download.offer), 'cache_dir': str(cache),
                      'profile': str(_validated_path(profile))}
        with VerifiedInstaller.open(root / UPDATE_HELPER_FILENAME,
                expected_sha256=UPDATE_HELPER_SHA256, expected_length=UPDATE_HELPER_LENGTH,
                policy=policy) as source:
            staging = Path(tempfile.mkdtemp(prefix='handoff-', dir=cache))
            destination = staging / UPDATE_HELPER_FILENAME
            source._file.copy_to(destination)
        verified = VerifiedInstaller.open(destination, expected_sha256=UPDATE_HELPER_SHA256,
                                         expected_length=UPDATE_HELPER_LENGTH, policy=policy)
        verified.reverify()
        if cancel is not None and cancel.is_set():
            raise UpdateInstallError('handoff')
        process = subprocess.Popen([str(destination)], executable=str(destination),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True, shell=False, cwd=str(staging),
            env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1'),
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        handle = HandoffProcess(process, verified, staging)
        verified = None
        message = json.dumps(ticket, sort_keys=True, separators=(',', ':')).encode('utf-8')
        if len(message) > 32768:
            raise UpdateInstallError('handoff')
        process.stdin.write(message + b'\n')
        process.stdin.flush()
        return handle.wait_ready(cancel=cancel, timeout=timeout)
    except BaseException:
        if handle is not None:
            handle.abort()
        if verified is not None:
            verified.close()
        if staging is not None and handle is None:
            shutil.rmtree(staging, ignore_errors=True)
        raise
