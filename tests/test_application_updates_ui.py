"""Actual offscreen Qt update consent/lifecycle tests; synthetic services only.

Every case runs in a fresh process with network and process launch tripwires.
Use CACHECAD_RUNTIME_PYTHON for the reviewed, hash-locked Qt environment.
No update endpoint, signing identity, account or user profile is contacted.
"""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = os.environ.get('CACHECAD_RUNTIME_PYTHON') or sys.executable

PRELUDE = r'''
import http.client
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'maintained'))
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from gui.application_updates import ApplicationUpdateDialog
app = QApplication([])
app.setQuitOnLastWindowClosed(False)
main_thread = threading.get_ident()
external_attempts = []
slot_errors = []
def forbidden(*args, **kwargs):
    external_attempts.append(True)
    raise AssertionError('No external activity is allowed in update UI tests')
socket.socket.connect = forbidden
socket.create_connection = forbidden
urllib.request.urlopen = forbidden
http.client.HTTPConnection.request = forbidden
subprocess.Popen = forbidden
sys.excepthook = lambda *error: slot_errors.append(error)
@dataclass(frozen=True)
class Offer:
    version: str = '0.2.0'
    build: int = 2
    channel: str = 'stable'
    length: int = 1024
    release_notes_url: str = 'https://updates.example.invalid/notes/0.2.0'
    sha256: str = 'a' * 64
@dataclass(frozen=True)
class Download:
    offer: Offer
    path: Path = Path('/fixture-only/installer.exe')
class Token:
    signer_certificate_sha256 = 'b' * 64
    def __init__(self):
        self.closed = 0
    def close(self):
        self.closed += 1
class Failure(Exception):
    def __init__(self, code='failed'):
        self.code = code
        super().__init__('SECRET URL/credential/path must never be shown')
class Service:
    configured = True
    def __init__(self, offer=Offer()):
        self.offer = offer
        self.calls = []
        self.check_impl = None
        self.download_impl = None
    def check(self, cancel):
        assert threading.get_ident() != main_thread
        self.calls.append('check')
        return self.check_impl(cancel) if self.check_impl else self.offer
    def download(self, offer, cancel, progress):
        assert threading.get_ident() != main_thread
        assert offer is self.offer
        self.calls.append('download')
        if self.download_impl:
            return self.download_impl(offer, cancel, progress)
        progress(512, offer.length)
        progress(offer.length, offer.length)
        return Download(offer)
def spin(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)
    app.processEvents()
    assert predicate(), 'Expected Qt condition did not settle'
def pump():
    for _ in range(10):
        app.processEvents()
        time.sleep(0.002)
def dialog_for(service=None, *, verify=None, install=None):
    service = Service() if service is None else service
    dialog = ApplicationUpdateDialog(service, verify_download=verify,
        request_install=install, installed_version='0.1.0')
    states = []
    def changed(state):
        assert threading.get_ident() == main_thread
        states.append(state)
    dialog.state_changed.connect(changed)
    dialog.test_states = states
    dialog.show()
    app.processEvents()
    return dialog
def verified(hold=None, token=None):
    token = token or Token()
    def verify(download, cancel):
        assert threading.get_ident() != main_thread
        assert isinstance(download, Download)
        if hold:
            hold.wait(3)
        return token
    return verify, token
def ready(dialog):
    dialog.check_button.click()
    spin(lambda: dialog.state == 'offered')
    dialog.download_button.click()
    spin(lambda: dialog.state == 'ready')
'''


@unittest.skipUnless((ROOT / 'maintained/gui/application_updates.py').is_file(),
                     'Maintained UI sources absent')
class ApplicationUpdateUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        probe = subprocess.run([RUNTIME, '-c', 'import PySide6'], capture_output=True)
        if probe.returncode:
            raise unittest.SkipTest('Reviewed Qt runtime unavailable; set CACHECAD_RUNTIME_PYTHON')

    def scenario(self, body):
        script = PRELUDE + '\n' + textwrap.dedent(body) + r'''
pump()
assert not external_attempts, external_attempts
assert not slot_errors, slot_errors
assert not [t for t in threading.enumerate() if t.name.startswith('CacheCADUpdate-')]
print('application-update-ui-pass')
'''
        result = subprocess.run([RUNTIME, '-B', '-c', script], cwd=ROOT,
            env=dict(os.environ, QT_QPA_PLATFORM='offscreen'), text=True,
            capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('application-update-ui-pass', result.stdout)

    def test_manual_only_setup_required_and_cancel_is_default(self):
        self.scenario('''
            service = Service()
            dialog = dialog_for(service)
            pump()
            assert dialog.state == 'idle' and service.calls == []
            assert dialog.cancel_button.isDefault()
            assert not dialog.install_button.isDefault()
            assert not dialog.download_button.isDefault()
            assert not dialog.install_button.isEnabled()
            QTest.keyClick(dialog, Qt.Key_Return)
            assert dialog.closed and service.calls == []
            assert dialog.shutdown()
            for service in (None, Service()):
                if service:
                    service.configured = False
                dialog = ApplicationUpdateDialog(service, installed_version='0.1.0')
                dialog.show(); app.processEvents()
                assert dialog.state == 'setup_required'
                assert dialog.check_button.isEnabled()
                dialog.check_button.click()
                assert dialog.state == 'setup_required'
                assert 'owner' in dialog.status_label.text()
                assert 'current' not in dialog.status_label.text()
                if service:
                    assert service.calls == []
                dialog.close()
                assert dialog.shutdown()
        ''')

    def test_download_integrity_and_windows_trust_are_separate_gates(self):
        self.scenario('''
            service = Service()
            hold = threading.Event()
            verify, token = verified(hold)
            calls = []
            def install(value):
                assert threading.get_ident() == main_thread
                assert value is token and not token.closed
                calls.append(value)
            dialog = dialog_for(service, verify=verify, install=install)
            dialog.check_button.click()
            spin(lambda: dialog.state == 'offered')
            text = dialog.details_label.text()
            assert '0.1.0' in text and '0.2.0' in text and '1024' in text
            assert 'stable' in text and 'https://updates.example.invalid' in text
            assert not dialog.install_button.isEnabled()
            dialog.download_button.click()
            spin(lambda: dialog.state == 'verifying')
            assert 'Download integrity verified' in dialog.status_label.text()
            assert not dialog.install_button.isEnabled() and not calls
            dialog.install_update()
            assert not calls
            hold.set()
            spin(lambda: dialog.is_ready)
            assert token.signer_certificate_sha256 in dialog.details_label.text()
            assert dialog.details_label.height() >= dialog.details_label.heightForWidth(
                dialog.details_label.width()), 'Publisher information must not be clipped'
            assert not calls
            dialog.install_button.click()
            dialog.install_update()
            assert dialog.state == 'handoff' and calls == [token]
            assert not token.closed
            dialog.close(); assert dialog.shutdown()
            assert not token.closed, 'Controller now owns the verified token'
            token.close()
        ''')

    def test_missing_verifier_never_authorizes_install(self):
        self.scenario('''
            calls = []
            dialog = dialog_for(install=lambda token: calls.append(token))
            dialog.check_button.click()
            spin(lambda: dialog.state == 'offered')
            dialog.download_button.click()
            spin(lambda: dialog.state == 'failed')
            assert 'publisher verification is unavailable' in dialog.status_label.text()
            assert not dialog.install_button.isEnabled() and not dialog.is_ready
            dialog.install_update()
            assert not calls
            dialog.close(); assert dialog.shutdown()
        ''')

    def test_repeated_clicks_cancel_wait_stale_completion_and_retry(self):
        self.scenario('''
            service = Service()
            started, release = threading.Event(), threading.Event()
            cancellations = []
            def blocked(cancel):
                cancellations.append(cancel)
                started.set(); release.wait(3)
                return Offer(version='9.9.9')  # Late result must be discarded.
            service.check_impl = blocked
            dialog = dialog_for(service)
            dialog.check_button.click(); spin(started.is_set)
            dialog.check_button.click(); dialog.check_for_updates()
            assert service.calls == ['check']
            generation = dialog._generation
            dialog.cancel_button.click()
            assert cancellations[0].is_set()
            assert dialog.state == 'cancelling' and dialog.busy
            assert not dialog.check_button.isEnabled()
            dialog.check_for_updates()
            assert service.calls == ['check']
            release.set(); spin(lambda: not dialog.busy)
            assert dialog.state == 'cancelled' and dialog.offer is None
            service.check_impl = None
            dialog.check_button.click(); spin(lambda: dialog.state == 'offered')
            assert dialog.offer is service.offer and service.calls == ['check', 'check']
            dialog._events.completed.emit(generation)
            dialog._events.progress.emit(generation, 1024, 1024)
            pump()
            assert dialog.offer is service.offer and dialog.state == 'offered'
            dialog.close(); assert dialog.shutdown()
        ''')

    def test_cancel_download_waits_and_ignores_progress_then_retries(self):
        self.scenario('''
            service = Service()
            started, release = threading.Event(), threading.Event()
            verified_calls = []
            def slow_download(offer, cancel, progress):
                started.set(); release.wait(3)
                progress(offer.length, offer.length)
                return Download(offer)
            service.download_impl = slow_download
            verify, token = verified()
            def tracked(download, cancel):
                verified_calls.append(download)
                return verify(download, cancel)
            dialog = dialog_for(service, verify=tracked)
            dialog.check_button.click(); spin(lambda: dialog.state == 'offered')
            dialog.download_button.click(); spin(started.is_set)
            dialog.download_update()
            assert service.calls.count('download') == 1
            dialog.cancel_update()
            assert dialog.state == 'cancelling'
            release.set(); spin(lambda: not dialog.busy)
            assert dialog.state == 'cancelled' and not verified_calls
            assert dialog.download_button.isEnabled()
            service.download_impl = None
            dialog.download_button.click(); spin(lambda: dialog.is_ready)
            assert len(verified_calls) == 1 and service.calls.count('download') == 2
            dialog.close(); assert dialog.shutdown() and token.closed == 1
        ''')

    def test_close_and_bounded_shutdown_retain_worker_and_close_late_token(self):
        self.scenario('''
            started, release = threading.Event(), threading.Event()
            token = Token()
            def slow_verify(download, cancel):
                started.set(); release.wait(3)
                return token
            calls = []
            dialog = dialog_for(verify=slow_verify, install=lambda value: calls.append(value))
            dialog.check_button.click(); spin(lambda: dialog.state == 'offered')
            dialog.download_button.click(); spin(started.is_set)
            task = dialog._task
            start = time.monotonic()
            assert dialog.shutdown(timeout_ms=10) is False
            assert time.monotonic() - start < 0.25
            assert dialog.closed and dialog.busy and dialog._task is task
            assert task.cancel_event.is_set() and task.thread.is_alive()
            dialog.close()
            release.set(); spin(lambda: not task.thread.is_alive())
            assert dialog.shutdown() is True
            assert token.closed == 1 and not calls and not dialog.is_ready
            assert dialog.state == 'closed'
        ''')

    def test_cancel_verification_and_finished_but_queued_result_are_discarded(self):
        self.scenario('''
            for completed_before_cancel in (False, True):
                started, release = threading.Event(), threading.Event()
                token = Token()
                def slow_verify(download, cancel):
                    started.set(); release.wait(3)
                    return token
                dialog = dialog_for(verify=slow_verify)
                dialog.check_button.click(); spin(lambda: dialog.state == 'offered')
                dialog.download_button.click(); spin(started.is_set)
                task = dialog._task
                if completed_before_cancel:
                    release.set()
                    task.thread.join(1)  # No Qt events: result is still queued.
                    assert not task.thread.is_alive()
                dialog.cancel_button.click()
                release.set(); spin(lambda: not dialog.busy)
                assert token.closed == 1 and dialog.state == 'cancelled'
                assert not dialog.is_ready and not dialog.install_button.isEnabled()
                assert dialog.download_button.isEnabled()
                second_verify, second_token = verified()
                dialog._verify_download = second_verify
                dialog.download_button.click(); spin(lambda: dialog.is_ready)
                dialog.close(); assert dialog.shutdown() and second_token.closed == 1
        ''')

    def test_escape_and_later_release_verified_handle(self):
        self.scenario('''
            for close_kind in ('escape', 'later', 'cancel', 'window', 'accept'):
                verify, token = verified()
                calls = []
                dialog = dialog_for(verify=verify, install=lambda value: calls.append(value))
                ready(dialog)
                if close_kind == 'escape':
                    QTest.keyClick(dialog, Qt.Key_Escape)
                elif close_kind == 'later':
                    dialog.later_button.click()
                elif close_kind == 'cancel':
                    dialog.cancel_button.click()
                elif close_kind == 'window':
                    dialog.close()
                else:
                    dialog.accept()
                app.processEvents()
                assert dialog.closed and token.closed == 1 and not calls
                dialog.close(); assert dialog.shutdown() and token.closed == 1
        ''')

    def test_busy_install_deferral_retry_and_reentrant_click_are_safe(self):
        self.scenario('''
            verify, token = verified()
            calls = []
            dialog = None
            def install(value):
                assert value is token and not value.closed
                calls.append(value)
                dialog.install_update()
                dialog.reject()
                assert not dialog.closed
                assert dialog.shutdown(0) is False
                return 'Transfers are still settling. Try again when they finish.' if len(calls) == 1 else None
            dialog = dialog_for(verify=verify, install=install)
            ready(dialog)
            dialog.install_button.click()
            assert len(calls) == 1 and dialog.is_ready and dialog.state == 'ready'
            assert 'Transfers' in dialog.status_label.text() and not token.closed
            dialog.install_button.click()
            assert len(calls) == 2 and dialog.state == 'handoff'
            dialog.close(); assert dialog.shutdown() and not token.closed
            token.close()
        ''')

    def test_safe_error_retry_no_update_and_new_check_closes_old_token(self):
        self.scenario('''
            service = Service()
            def error(cancel):
                raise Failure('timeout')
            service.check_impl = error
            verify, token = verified()
            dialog = dialog_for(service, verify=verify)
            dialog.check_button.click(); spin(lambda: dialog.state == 'failed')
            assert 'timed out' in dialog.status_label.text()
            assert 'SECRET' not in dialog.status_label.text()
            service.check_impl = lambda cancel: (_ for _ in ()).throw(Failure('clock'))
            dialog.check_button.click(); spin(lambda: dialog.state == 'failed')
            assert 'system date and time' in dialog.status_label.text()
            service.check_impl = None
            ready(dialog)
            service.offer = None
            dialog.check_button.click(); spin(lambda: dialog.state == 'current')
            assert token.closed == 1 and not dialog.is_ready
            assert 'verified channel' in dialog.status_label.text()
            assert not dialog.download_button.isEnabled()
            dialog.close(); assert dialog.shutdown()
        ''')

    def test_changed_offer_and_storage_errors_have_safe_recovery_messages(self):
        self.scenario('''
            for code in ('changed', 'storage', 'incompatible', 'rollback', 'configuration'):
                service = Service()
                def fail_download(offer, cancel, progress):
                    raise Failure(code)
                service.download_impl = fail_download
                dialog = dialog_for(service)
                dialog.check_button.click(); spin(lambda: dialog.state == 'offered')
                dialog.download_button.click(); spin(lambda: dialog.state == 'failed')
                assert 'SECRET' not in dialog.status_label.text()
                assert dialog.check_button.isEnabled() and not dialog.install_button.isEnabled()
                if code == 'storage':
                    assert 'disk space' in dialog.status_label.text()
                    assert dialog.download_button.isEnabled()
                else:
                    assert dialog.offer is None and not dialog.download_button.isEnabled()
                dialog.close(); assert dialog.shutdown()
        ''')

    def test_publisher_verification_failure_is_fail_closed_and_retryable(self):
        self.scenario('''
            def fail_verify(download, cancel):
                raise Failure('untrusted-signature')
            calls = []
            dialog = dialog_for(verify=fail_verify, install=lambda value: calls.append(value))
            dialog.check_button.click(); spin(lambda: dialog.state == 'offered')
            dialog.download_button.click(); spin(lambda: dialog.state == 'failed')
            assert 'Windows publisher verification failed' in dialog.status_label.text()
            assert not dialog.install_button.isEnabled() and not dialog.is_ready
            assert dialog.download_button.isEnabled()
            assert not calls and 'SECRET' not in dialog.status_label.text()
            dialog.close(); assert dialog.shutdown()
        ''')

    def test_handoff_failure_requires_a_fresh_check_and_verification(self):
        self.scenario('''
            verify, token = verified()
            service = Service()
            accepted = []
            def install(value):
                accepted.append(value)
            dialog = dialog_for(service, verify=verify, install=install)
            ready(dialog)
            dialog.install_button.click()
            assert accepted == [token] and dialog.state == 'handoff'
            token.close()  # The controller cleans up its accepted handle first.
            dialog.handoff_failed('The helper could not acquire the installation gate.')
            assert dialog.state == 'failed' and dialog.offer is None
            assert dialog.check_button.isEnabled()
            assert not dialog.install_button.isEnabled()
            assert not dialog.download_button.isEnabled()
            assert token.closed == 1
            second_verify, second_token = verified()
            dialog._verify_download = second_verify
            ready(dialog)
            assert service.calls == ['check', 'download', 'check', 'download']
            dialog.close(); assert dialog.shutdown() and second_token.closed == 1
            dialog.handoff_failed('Late failure must not reopen the dialog')
            assert dialog.closed and dialog.state == 'closed'
        ''')

    def test_synchronous_handoff_failure_cannot_leave_transferred_token_ready(self):
        self.scenario('''
            verify, token = verified()
            dialog = None
            def install(value):
                dialog.handoff_failed('The helper failed before readiness.')
                return None
            dialog = dialog_for(verify=verify, install=install)
            ready(dialog)
            dialog.install_button.click()
            assert dialog.state == 'failed' and not dialog.is_ready
            assert dialog.offer is None and token.closed == 1
            assert dialog.check_button.isEnabled()
            dialog.close(); assert dialog.shutdown()
        ''')

    def test_native_widget_deletion_cancels_worker_and_releases_late_handle(self):
        self.scenario('''
            from shiboken6 import delete
            started, release = threading.Event(), threading.Event()
            token = Token()
            def slow_verify(download, cancel):
                started.set(); release.wait(3)
                return token
            dialog = dialog_for(verify=slow_verify)
            dialog.check_button.click(); spin(lambda: dialog.state == 'offered')
            dialog.download_button.click(); spin(started.is_set)
            task = dialog._task
            delete(dialog)
            assert task.cancel_event.is_set()
            release.set(); spin(lambda: not task.thread.is_alive())
            assert token.closed == 1
            verify, second_token = verified()
            other = dialog_for(verify=verify)
            ready(other)
            delete(other)
            assert second_token.closed == 1
        ''')

    def test_untrusted_display_is_plain_text_and_progress_supports_large_files(self):
        self.scenario('''
            offer = Offer(version='<a href="file:///secret">0.2.0</a>\\n', length=5_000_000_000)
            service = Service(offer)
            started, release = threading.Event(), threading.Event()
            def download(offer, cancel, progress):
                progress(4_000_000_000, offer.length)
                started.set(); release.wait(3)
                return Download(offer)
            service.download_impl = download
            dialog = dialog_for(service)
            dialog.check_button.click(); spin(lambda: dialog.state == 'offered')
            assert dialog.details_label.textFormat() == Qt.PlainText
            assert '<a href=' in dialog.details_label.text()
            assert '\\n' in dialog.details_label.text()
            dialog.download_button.click(); spin(started.is_set)
            spin(lambda: dialog.progress_bar.value() == 80)
            dialog.cancel_update(); release.set(); spin(lambda: not dialog.busy)
            dialog.close(); assert dialog.shutdown()
        ''')


if __name__ == '__main__':
    unittest.main()
