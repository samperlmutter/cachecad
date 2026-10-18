"""Security boundary tests. Trust/process fakes live only in this test module.

Native Windows tests never execute the unsigned fixture. Installer upgrade CI is
separate and cannot bypass the production Authenticode verifier.
"""
import ctypes
from dataclasses import asdict, dataclass
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'maintained/core/windows_update_install.py').is_file():
    raise unittest.SkipTest('Maintained update verifier absent from wheel fixture')
from maintained.core import windows_update_install as win
from maintained.utils.profile_lock import ProfileLock


PIN = hashlib.sha256(b'synthetic certificate').hexdigest()
HASH = hashlib.sha256(b'synthetic installer').hexdigest()


class Function:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


class TrustFixture:
    """Simulate successful Windows provider *state*, never sign real binaries."""
    def __init__(self, status=0):
        self.status, self.calls = status, []
        self.der = ctypes.create_string_buffer(b'synthetic certificate')
        self.certificate = win._CertContext(1, ctypes.addressof(self.der), len(self.der) - 1,
                                           None, None)
        self.provider_certificate = win._ProviderCert()
        self.provider_certificate.certificate = ctypes.pointer(self.certificate)
        self.signer_info = win._SignerInfoPrefix()
        self.signer_info.digest_algorithm.oid = b'2.16.840.1.101.3.4.2.1'
        self.signer = win._ProviderSigner()
        self.signer.certificate_count, self.signer.counter_count = 1, 1
        self.signer.signer = ctypes.pointer(self.signer_info)
        self.counter = win._ProviderSigner()
        self.counter.certificate_count = 1
        self.counter.signer = ctypes.pointer(self.signer_info)
        self.WinVerifyTrust = Function(self.verify)
        self.WTHelperProvDataFromStateData = Function(lambda state: 123)
        self.WTHelperGetProvSignerFromChain = Function(
            lambda data, index, counter, ci: ctypes.pointer(self.counter if counter else self.signer))
        self.WTHelperGetProvCertFromChain = Function(
            lambda signer, index: ctypes.pointer(self.provider_certificate))

    def verify(self, hwnd, action, data_pointer):
        data = data_pointer._obj
        self.calls.append((data.action, data.ui, data.revocation, data.flags,
                           data.file.contents.handle, data.signatures.contents.index,
                           data.signatures.contents.flags))
        data.state = 123
        return self.status if data.action == 1 else 0


class AuthenticodePolicyTests(unittest.TestCase):
    def test_trust_success_uses_exact_held_handle_primary_signature_and_chain_policy(self):
        fixture = TrustFixture()
        with patch.object(win, '_system_dll', return_value=fixture):
            result = win._trusted_certificate_hash(types.SimpleNamespace(path=Path('fixture.exe'), handle=42))
        self.assertEqual(result, PIN)
        self.assertEqual(fixture.calls[0], (1, 2, 1, 0x6080, 42, 0, 1))
        self.assertEqual(fixture.calls[-1][0], 2)
        self.assertEqual(fixture.WinVerifyTrust.restype, ctypes.c_int32)

    def test_every_nonzero_status_including_positive_is_failure_and_state_is_closed(self):
        for status in (1, -2146762496, -2146885613):
            fixture = TrustFixture(status)
            with self.subTest(status=status), patch.object(win, '_system_dll', return_value=fixture):
                with self.assertRaises(win.UpdateInstallError) as error:
                    win._trusted_certificate_hash(types.SimpleNamespace(path=Path('fixture.exe'), handle=42))
                self.assertEqual(error.exception.code, 'signature')
            self.assertEqual([call[0] for call in fixture.calls], [1, 2])

    def test_missing_timestamp_wrong_algorithm_and_signer_errors_are_rejected(self):
        changes = [lambda f: setattr(f.signer, 'counter_count', 0),
                   lambda f: setattr(f.signer, 'error', 5),
                   lambda f: setattr(f.counter, 'error', 9),
                   lambda f: setattr(f.counter, 'certificate_count', 0),
                   lambda f: setattr(f.provider_certificate, 'error', 7),
                   lambda f: setattr(f.signer_info.digest_algorithm, 'oid', b'1.3.14.3.2.26')]
        for change in changes:
            fixture = TrustFixture()
            change(fixture)
            with patch.object(win, '_system_dll', return_value=fixture):
                with self.assertRaises(win.UpdateInstallError):
                    win._trusted_certificate_hash(types.SimpleNamespace(path=Path('fixture.exe'), handle=42))
            self.assertEqual(fixture.calls[-1][0], 2)

    def test_identity_policy_never_accepts_subject_name_empty_or_malformed_pins(self):
        for pins in ((), [], None, ['Publisher Incorporated'], ['a' * 63], ['A' * 64], 'a' * 64):
            with self.subTest(pins=pins), self.assertRaises(win.UpdateInstallError):
                win.SignerPolicy(pins)
        self.assertEqual(win.SignerPolicy([PIN]).approved_certificate_sha256, frozenset([PIN]))

    @unittest.skipIf(sys.platform == 'win32', 'Non-Windows refusal')
    def test_non_windows_never_loads_native_verifier(self):
        with patch.object(win, '_PinnedFile') as opened:
            with self.assertRaises(win.UpdateInstallError) as error:
                win.VerifiedInstaller.open('fixture.exe', expected_sha256=HASH,
                    expected_length=19, policy=win.SignerPolicy([PIN]))
            self.assertEqual(error.exception.code, 'platform')
            opened.assert_not_called()

    def test_paths_reject_devices_unc_ads_parent_aliases_and_trailing_dots(self):
        for path in ('relative.exe', r'\\server\share\a.exe', r'\\?\C:\a.exe',
                     r'C:\cache\a.exe:stream', r'C:\cache\..\a.exe', r'C:\cache.\a.exe',
                     r'C:a.exe', 'C:\\a\x00.exe'):
            with self.subTest(path=path), self.assertRaises(win.UpdateInstallError):
                win._validated_path(path)


class VerifiedFileTests(unittest.TestCase):
    def setUp(self):
        self.file = Mock(path=Path('fixture.exe'), handle=42)
        self.file.digest.return_value = HASH
        for name, value in (('_require_windows', None), ('_require_non_elevated', None), ('_PinnedFile', self.file),
                            ('_trusted_certificate_hash', PIN)):
            mock = patch.object(win, name, return_value=value)
            self.addCleanup(mock.stop)
            mock.start()

    def open(self):
        return win.VerifiedInstaller.open('fixture.exe', expected_sha256=HASH,
            expected_length=19, policy=win.SignerPolicy([PIN]))

    def test_verified_capability_retains_handle_and_close_is_idempotent(self):
        with self.open() as verified:
            self.assertFalse(verified.closed)
            self.assertIs(verified._file, self.file)
            self.file.close.assert_not_called()
            verified.reverify()
        self.assertTrue(verified.closed)
        verified.close()
        self.file.close.assert_called_once()
        with self.assertRaises(win.UpdateInstallError):
            verified.reverify()

    def test_changed_digest_or_wrong_publisher_closes_without_launch(self):
        for digest, signer in [('0' * 64, PIN), (HASH, 'f' * 64)]:
            self.file.digest.return_value = digest
            self.file.close.reset_mock()
            with patch.object(win, '_trusted_certificate_hash', return_value=signer):
                with self.assertRaises(win.UpdateInstallError):
                    self.open()
            self.file.close.assert_called_once()

    def test_invalid_length_and_digest_refuse_before_open(self):
        for length, digest in ((True, HASH), (0, HASH), (-1, HASH), (1024**3 + 1, HASH),
                               (19, HASH.upper()), (19, None)):
            with self.assertRaises(win.UpdateInstallError):
                win.VerifiedInstaller.open('fixture.exe', expected_sha256=digest,
                    expected_length=length, policy=win.SignerPolicy([PIN]))

    def test_launch_requires_handoff_and_absence_of_every_app_then_fixed_arguments(self):
        verified = self.open()
        self.addCleanup(verified.close)
        handoff = win.HandoffLease()
        handoff._presence = Mock()
        gate = Mock()
        gate.kernel.present.return_value = False
        mutex = Mock()
        mutex.__enter__ = Mock(return_value=gate)
        mutex.__exit__ = Mock(return_value=False)
        process = Mock()

        def wait():
            self.assertFalse(verified.closed)
            self.file.close.assert_not_called()
            return 2  # User cancellation is preserved, not reported as success.

        process.wait.side_effect = wait
        with patch.object(win, '_Mutex', return_value=mutex), patch.object(win.subprocess, 'Popen', return_value=process) as launch:
            self.assertEqual(win.launch_verified_installer(verified, handoff), 2)
            args, kwargs = launch.call_args
            self.assertEqual(args[0], ['fixture.exe', *win.INSTALLER_ARGUMENTS])
            self.assertFalse(kwargs['shell'])
            self.assertTrue(kwargs['close_fds'])
            self.assertEqual(kwargs['executable'], 'fixture.exe')
            self.assertTrue(verified._launched)
            with self.assertRaises(win.UpdateInstallError):
                win.launch_verified_installer(verified, handoff)
        self.file.close.assert_not_called()

    def test_other_profile_presence_prevents_installer_launch(self):
        verified, handoff = self.open(), win.HandoffLease()
        self.addCleanup(verified.close)
        handoff._presence = Mock()
        mutex = Mock(__enter__=Mock(return_value=Mock(kernel=Mock(present=Mock(return_value=True)))),
                     __exit__=Mock(return_value=False))
        with patch.object(win, '_Mutex', return_value=mutex), patch.object(win.subprocess, 'Popen') as launch:
            with self.assertRaises(win.UpdateInstallError) as error:
                win.launch_verified_installer(verified, handoff)
            self.assertEqual(error.exception.code, 'busy')
            launch.assert_not_called()


class PathHandleProtocolTests(unittest.TestCase):
    def kernel(self, *, bad_directory=False, bad_file=False, links=1):
        kernel = Mock()
        handles = iter(range(100, 110))
        kernel.CreateFileW.side_effect = lambda *args: next(handles)

        def information(handle):
            # The synthetic /cache/fixture.exe path has / then /cache parents.
            return types.SimpleNamespace(attributes=(0x10 | (0x400 if bad_directory else 0))
                if handle < 102 else (0x400 if bad_file else 0), links=links)

        kernel.information.side_effect = information
        return kernel

    def test_directory_chain_is_pinned_top_down_without_write_delete_sharing(self):
        kernel = self.kernel()
        with patch.object(win, '_validated_path', return_value=PurePosixPath('/cache/fixture.exe')), \
                patch.object(win, '_Kernel', return_value=kernel):
            pinned = win._PinnedFile('synthetic')
        calls = kernel.CreateFileW.call_args_list
        self.assertEqual([call.args[0] for call in calls], ['/', '/cache', '/cache/fixture.exe'])
        self.assertTrue(all(call.args[2] == 1 for call in calls))
        self.assertTrue(all(call.args[5] & 0x00200000 for call in calls))
        pinned.close()
        self.assertEqual([call.args[0] for call in kernel.CloseHandle.call_args_list], [102, 101, 100])

    def test_reparse_directory_file_or_hardlink_refusal_releases_all_handles(self):
        for options in ({'bad_directory': True}, {'bad_file': True}, {'links': 2}):
            kernel = self.kernel(**options)
            with patch.object(win, '_validated_path', return_value=PurePosixPath('/cache/fixture.exe')), \
                    patch.object(win, '_Kernel', return_value=kernel):
                with self.assertRaises(win.UpdateInstallError) as error:
                    win._PinnedFile('synthetic')
            self.assertEqual(error.exception.code, 'path')
            self.assertEqual(kernel.CloseHandle.call_count, kernel.CreateFileW.call_count)


class HandoffProcessTests(unittest.TestCase):
    def process(self, reply=b'CACHECAD_UPDATE_READY\n'):
        process = Mock(stdin=io.BytesIO(), stdout=io.BytesIO(reply), poll=Mock(return_value=None))
        def wait(*args, **kwargs):
            process.poll.return_value = 0
            return 0
        process.wait.side_effect = wait
        return process

    def test_ready_does_not_send_commit_and_abort_only_stops_uncommitted_helper(self):
        process, verified = self.process(), Mock()
        with tempfile.TemporaryDirectory() as directory:
            handoff = win.HandoffProcess(process, verified, directory)
            handoff.wait_ready()
            self.assertEqual(process.stdin.getvalue(), b'')
            handoff.abort()
            for unused in range(100):
                if verified.close.called:
                    break
                threading.Event().wait(0.001)
            process.terminate.assert_not_called()
            verified.close.assert_called_once()

    def test_commit_requires_ready_is_single_use_and_cannot_be_terminated_afterwards(self):
        process = self.process()
        handoff = win.HandoffProcess(process, Mock(), Path('unused'))
        with self.assertRaises(win.UpdateInstallError):
            handoff.commit()
        handoff.wait_ready()
        handoff.commit()
        self.assertEqual(process.stdin.getvalue(), b'COMMIT\n')
        with self.assertRaises(win.UpdateInstallError):
            handoff.commit()
        with self.assertRaises(win.UpdateInstallError):
            handoff.abort()
        handoff.close()
        process.terminate.assert_not_called()

    def test_eof_invalid_reply_or_cancel_never_grants_commit(self):
        for reply in (b'', b'CACHECAD_UPDATE_FAILED\n', b'READY\n'):
            with tempfile.TemporaryDirectory() as directory:
                handoff = win.HandoffProcess(self.process(reply), Mock(), directory)
                with self.assertRaises(win.UpdateInstallError):
                    handoff.wait_ready()
                self.assertFalse(handoff._committed)
        cancellation = threading.Event()
        cancellation.set()
        with tempfile.TemporaryDirectory() as directory:
            handoff = win.HandoffProcess(self.process(), Mock(), directory)
            with self.assertRaises(win.UpdateInstallError):
                handoff.wait_ready(cancel=cancellation)
            self.assertFalse(handoff._committed)

    def test_cancel_does_not_kill_onefile_wrapper_or_wait_on_held_stdout_lock(self):
        process = self.process()
        finish = threading.Event()
        def wait(*unused, **kwargs):
            finish.wait(5)
            process.poll.return_value = 0
            return 0
        process.wait.side_effect = wait
        with tempfile.TemporaryDirectory() as directory:
            verified = Mock()
            handoff = win.HandoffProcess(process, verified, directory)
            handoff.abort()
            self.assertTrue(process.stdin.closed)
            self.assertFalse(process.stdout.closed)
            process.terminate.assert_not_called()
            verified.close.assert_not_called()
            with self.assertRaises(win.UpdateInstallError):
                handoff.commit()
            finish.set()
            for unused in range(100):
                if verified.close.called:
                    break
                threading.Event().wait(0.001)
            verified.close.assert_called_once()


@dataclass(frozen=True)
class OfferFixture:
    version: str = '0.1.1'
    build: int = 2
    sha256: str = HASH
    length: int = 19


def load_helper():
    """The source entrypoint normally imports the independently frozen modules."""
    build = types.ModuleType('app.app_version')
    build.APP_PRODUCT_ID, build.APP_VERSION, build.APP_RELEASE_NUMBER = 'cachecad-reconstructed', '0.1.0', 1
    build.APP_BUILD_DATE = '2026-10-18'
    updates = types.ModuleType('core.application_updates')
    updates.InstalledRelease = Mock()
    updates.UpdateService = Mock()
    updates.load_update_config = Mock()
    profile = types.ModuleType('utils.profile_lock')
    profile.ProfileLock = ProfileLock
    spec = importlib.util.spec_from_file_location('cachecad_update_helper_test', ROOT / 'application/update_helper.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'app.app_version': build,
            'core.application_updates': updates, 'core.windows_update_install': win,
            'utils.profile_lock': profile}):
        spec.loader.exec_module(module)
    return module


class HelperProtocolTests(unittest.TestCase):
    def setUp(self):
        self.helper = load_helper()
        self.offer = OfferFixture()
        self.ticket = {'schema': 1, 'parent_pid': 123, 'parent_creation_time': 456,
                       'installer_path': Path('fixture.exe'), 'cache_dir': Path('cache'),
                       'profile': Path('profile'), 'offer': asdict(self.offer)}
        self.parent = Mock()
        self.parent.__enter__ = Mock(return_value=self.parent)
        self.parent.__exit__ = Mock(return_value=False)
        self.installer = Mock()
        self.installer.__enter__ = Mock(return_value=self.installer)
        self.installer.__exit__ = Mock(return_value=False)
        self.handoff = Mock()
        self.handoff.__enter__ = Mock(return_value=self.handoff)
        self.handoff.__exit__ = Mock(return_value=False)
        patches = {
            '_require_windows': Mock(), '_require_non_elevated': Mock(),
            'load_update_config': Mock(return_value=types.SimpleNamespace(publisher_certificate_sha256=(PIN,))),
            'ParentProcess': Mock(return_value=self.parent),
            'UpdateService': Mock(return_value=Mock(check=Mock(return_value=self.offer))),
            'VerifiedInstaller': Mock(open=Mock(return_value=self.installer)),
            'HandoffLease': Mock(return_value=self.handoff),
            'launch_verified_installer': Mock(return_value=0), '_write_result': Mock(),
        }
        for name, value in patches.items():
            change = patch.object(self.helper, name, value)
            change.start()
            self.addCleanup(change.stop)
        architecture = patch.object(self.helper.platform, 'machine', return_value='AMD64')
        architecture.start()
        self.addCleanup(architecture.stop)
        version = patch.object(sys, 'getwindowsversion', return_value=types.SimpleNamespace(build=19045), create=True)
        version.start()
        self.addCleanup(version.stop)

    def run_helper(self, permission):
        output = io.BytesIO()
        result = self.helper._run(self.ticket, Path('bundled'), io.BytesIO(permission), output)
        self.assertEqual(output.getvalue(), b'CACHECAD_UPDATE_READY\n')
        return result

    def test_parent_eof_abort_or_spurious_commit_never_installs(self):
        for permission in (b'', b'ABORT\n', b'commit\n', b'COMMIT extra\n'):
            self.assertEqual(self.run_helper(permission), 3)
        self.parent.wait_for_exit.assert_not_called()
        self.helper.launch_verified_installer.assert_not_called()

    def test_commit_waits_actual_parent_exit_then_installs_and_records_exit_only(self):
        events = []
        self.parent.wait_for_exit.side_effect = lambda: events.append('parent-exited')
        self.helper.launch_verified_installer.side_effect = lambda *args: events.append('installer') or 0
        self.assertEqual(self.run_helper(b'COMMIT\n'), 0)
        self.assertEqual(events, ['parent-exited', 'installer'])
        self.helper._write_result.assert_called_once_with(Path('profile'), self.offer,
                                                         'installation_finished', 0)

    def test_parent_exit_timeout_never_launches_or_writes_held_profile(self):
        self.parent.wait_for_exit.side_effect = win.UpdateInstallError('process')
        self.assertEqual(self.run_helper(b'COMMIT\n'), 4)
        self.helper.launch_verified_installer.assert_not_called()
        self.helper._write_result.assert_not_called()

    def test_changed_or_missing_current_offer_fails_before_ready(self):
        for offer in (None, OfferFixture(build=3)):
            self.helper.UpdateService.return_value.check.return_value = offer
            output = io.BytesIO()
            with self.assertRaises(win.UpdateInstallError):
                self.helper._run(self.ticket, Path('bundled'), io.BytesIO(b'COMMIT\n'), output)
            self.assertEqual(output.getvalue(), b'')
        self.helper.VerifiedInstaller.open.assert_not_called()
        self.helper.launch_verified_installer.assert_not_called()

    def test_installer_nonzero_status_is_not_success(self):
        for code, status in ((2, 'installation_cancelled'), (5, 'installation_cancelled'),
                             (1, 'installation_failed'), (99, 'installation_failed')):
            self.helper.launch_verified_installer.return_value = code
            self.assertEqual(self.run_helper(b'COMMIT\n'), 6)
            self.helper._write_result.assert_called_with(Path('profile'), self.offer, status, code)

    def test_ticket_rejects_duplicate_keys_overrides_and_oversize(self):
        for raw in (b'{"schema":1,"schema":1}\n', b'{}\n', b'x' * 32770,
                    json.dumps(dict(self.ticket, offer={}, pins=[PIN]), default=str).encode() + b'\n'):
            with self.assertRaises(win.UpdateInstallError):
                self.helper._ticket(io.BytesIO(raw))


@unittest.skipUnless(sys.platform == 'win32', 'Actual Windows kernel/trust boundary')
class NativeWindowsTests(unittest.TestCase):
    def test_unsigned_fixture_rejected_by_actual_windows_trust_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'never-execute.exe'
            raw = b'MZ' + b'UNSIGNED SYNTHETIC BYTES' * 20
            target.write_bytes(raw)
            with patch.object(win.subprocess, 'Popen') as launch:
                with self.assertRaises(win.UpdateInstallError) as error:
                    win.VerifiedInstaller.open(target, expected_length=len(raw),
                        expected_sha256=hashlib.sha256(raw).hexdigest(), policy=win.SignerPolicy([PIN]))
                self.assertEqual(error.exception.code, 'signature')
                launch.assert_not_called()

    def test_held_handle_denies_replacement_and_hardlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'fixture.exe'
            target.write_bytes(b'held bytes')
            pinned = win._PinnedFile(target)
            try:
                self.assertEqual(pinned.digest(10), hashlib.sha256(b'held bytes').hexdigest())
                with self.assertRaises(OSError):
                    target.write_bytes(b'changed')
                with self.assertRaises(OSError):
                    target.unlink()
                with self.assertRaises(OSError):
                    target.rename(target.with_name('replaced.exe'))
            finally:
                pinned.close()
            other = target.with_name('hardlink.exe')
            os.link(target, other)
            with self.assertRaises(win.UpdateInstallError):
                win._PinnedFile(target)

    def test_handoff_blocks_new_process_and_running_presence_covers_multiple_leases(self):
        command = [sys.executable, '-c',
            'from maintained.core.windows_update_install import ApplicationRunLease; '
            'lease = ApplicationRunLease().acquire(); print("started")']
        with win.ApplicationRunLease(), win.ApplicationRunLease():
            self.assertTrue(win._Kernel().present(win.RUNNING_MUTEX))
            with win.HandoffLease():
                child = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=15)
                self.assertNotEqual(child.returncode, 0)
                self.assertNotIn(b'started', child.stdout)
        self.assertFalse(win._Kernel().present(win.RUNNING_MUTEX))
        self.assertFalse(win._Kernel().present(win.HANDOFF_MUTEX))

    def test_parent_handle_creation_time_detects_pid_reuse_and_actual_exit(self):
        with win.ParentProcess(os.getpid()) as process:
            self.assertGreater(process.creation_time, 0)
            with self.assertRaises(win.UpdateInstallError):
                win.ParentProcess(os.getpid(), process.creation_time + 1)
            with self.assertRaises(win.UpdateInstallError):
                process.wait_for_exit(0)


if __name__ == '__main__':
    unittest.main()
