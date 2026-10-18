"""Identity-only Google sign-in UI, separate from recovered auth/Drive signals.

The backend owns verification. A worker never touches Qt widgets, and its result
is delivered through a queued signal only after that worker has exited. Cancelling
or closing invalidates the attempt before signalling cancellation. There are no
credential, configuration, or identity writes here.
"""
from pathlib import Path
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from core.desktop_signin import DesktopSignIn, DesktopSignInError, SignInIdentity


SAFE_CODES = frozenset(('configuration', 'dependencies', 'busy', 'browser', 'callback',
                        'denied', 'provider', 'identity', 'cancelled', 'timeout', 'failed'))

SAFE_MESSAGES = {
    'cancelled': 'Sign-in cancelled. You can try again.',
    'denied': 'Google sign-in was declined. You can try again.',
    'dependencies': 'The Google sign-in dependencies are unavailable.',
    'timeout': 'Sign-in timed out. You can try again.',
    'invalid_config': 'Choose your own Google OAuth Desktop app client JSON file.',
    'configuration': 'Choose your own Google OAuth Desktop app client JSON file.',
    'browser': 'The browser could not be opened. Check your default browser and retry.',
    'browser_open': 'The browser could not be opened. Check your default browser and retry.',
}


class _Completion(QObject):
    ready = Signal(int, object, str)


def _run_backend(factory, path, cancellation, timeout, generation, completion):
    """No widget or controller references cross into this daemon worker."""
    identity, code = None, ''
    try:
        identity = factory(path).run(cancel_event=cancellation, timeout=timeout)
        if not isinstance(identity, SignInIdentity):
            identity, code = None, 'identity'
    except DesktopSignInError as error:
        code = error.code if isinstance(error.code, str) and error.code in SAFE_CODES else 'failed'
    except Exception:
        # Transport/provider/configuration errors can contain URLs or tokens.
        code = 'failed'
    if cancellation.is_set():
        identity, code = None, 'cancelled'
    try:
        completion.ready.emit(generation, identity, code)
    except RuntimeError:
        # The user may already have closed the app. Never access a dead widget.
        pass


class DesktopSignInDialog(QDialog):
    identity_changed = Signal(object)
    attempt_finished = Signal(str)

    def __init__(self, parent=None, *, backend_factory=DesktopSignIn, config_picker=None,
                 identity=None, timeout=120):
        super().__init__(parent)
        self.setWindowTitle('Google sign-in only')
        self.setMinimumWidth(460)
        self.setModal(False)
        self._factory = backend_factory
        self._picker = config_picker or QFileDialog.getOpenFileName
        self._timeout = timeout
        self._config_path = None
        self._identity = identity if isinstance(identity, SignInIdentity) else None
        self._generation = 0
        self._cancel_event = None
        self._thread = None
        self._pending_result = None
        self._closed = False
        self._cancelling = False
        self._completion = _Completion(self)
        self._completion.ready.connect(self._receive, Qt.ConnectionType.QueuedConnection)
        self._poll = QTimer(self)
        self._poll.setInterval(20)
        self._poll.timeout.connect(self._settle)
        layout = QVBoxLayout(self)
        description = QLabel(
            'Choose your own Google OAuth Desktop app client JSON, then continue in your '
            'system browser. Only your verified email is displayed.\n\n'
            'Drive access, project changes, transfers, updates and vendor services stay disabled. '
            'Tokens are not saved. Signing out here only clears this local identity.')
        description.setWordWrap(True)
        layout.addWidget(description)
        self.config_label = QLabel('No Desktop client configuration selected')
        self.config_label.setTextFormat(Qt.TextFormat.PlainText)
        self.config_label.setWordWrap(True)
        layout.addWidget(self.config_label)
        self.choose_button = QPushButton('Choose Desktop client JSON…')
        self.choose_button.clicked.connect(self.choose_config)
        layout.addWidget(self.choose_button)
        self.status_label = QLabel('Ready when you are.')
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.identity_label = QLabel()
        self.identity_label.setTextFormat(Qt.TextFormat.PlainText)
        self.identity_label.setWordWrap(True)
        self.identity_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.identity_label)
        buttons = QHBoxLayout()
        self.signin_button = QPushButton('Continue in browser')
        self.signin_button.clicked.connect(self.start_signin)
        self.cancel_button = QPushButton('Cancel sign-in')
        self.cancel_button.clicked.connect(self.cancel_signin)
        self.signout_button = QPushButton('Sign out locally')
        self.signout_button.clicked.connect(self.sign_out)
        self.close_button = QPushButton('Close')
        self.close_button.clicked.connect(self.reject)
        for button in (self.signin_button, self.cancel_button, self.signout_button, self.close_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        if self._identity is not None:
            self.status_label.setText('Google identity verified. Drive remains disabled.')
        self._update_controls()

    @property
    def identity(self):
        return self._identity

    @property
    def busy(self):
        return self._thread is not None

    @property
    def closed(self):
        return self._closed

    def _update_controls(self):
        active = self.busy
        self.choose_button.setEnabled(not self._closed and not active and self._identity is None)
        self.signin_button.setEnabled(not self._closed and not active and
                                      self._config_path is not None and self._identity is None)
        self.cancel_button.setEnabled(not self._closed and active and not self._cancelling)
        self.signout_button.setEnabled(not self._closed and not active and self._identity is not None)
        self.identity_label.setText('Verified email: ' + self._identity.email if self._identity else '')

    def choose_config(self):
        if self._closed or self.busy or self._identity is not None:
            return
        filename, _ = self._picker(self, 'Choose your own Google OAuth Desktop client JSON',
                                   '', 'JSON files (*.json)')
        if filename:
            self._config_path = Path(filename).absolute()
            self.config_label.setText('Selected: ' + self._config_path.name)
            self.status_label.setText('Click Continue in browser to begin Google sign-in.')
            self._update_controls()

    def start_signin(self):
        if self._closed or self.busy or self._config_path is None or self._identity is not None:
            return
        self._generation += 1
        self._cancel_event = threading.Event()
        self._cancelling = False
        self._pending_result = None
        self.status_label.setText('Complete sign-in in your browser. You can cancel here at any time.')
        self._thread = threading.Thread(
            target=_run_backend,
            args=(self._factory, self._config_path, self._cancel_event, self._timeout,
                  self._generation, self._completion),
            name='CacheCADIdentitySignIn', daemon=True)
        self._update_controls()
        self._poll.start()
        try:
            self._thread.start()
        except RuntimeError:
            self._thread = None
            self._poll.stop()
            self.status_label.setText('Sign-in could not start. Please try again.')
            self._update_controls()
            self.attempt_finished.emit('failed')

    def _receive(self, generation, identity, code):
        if self._closed or generation != self._generation:
            return
        self._pending_result = (identity, code)
        self._settle()

    def _settle(self):
        if self._thread is None or self._thread.is_alive():
            return
        if self._closed:
            self._thread = None
            self._poll.stop()
            return
        if self._cancelling:
            self._thread = None
            self._pending_result = None
            self._poll.stop()
            self.status_label.setText(SAFE_MESSAGES['cancelled'])
            self._update_controls()
            self.attempt_finished.emit('cancelled')
            return
        if self._pending_result is None:
            return  # The queued signal can arrive after the worker exits.
        identity, code = self._pending_result
        self._pending_result = None
        self._thread = None
        self._poll.stop()
        if not code and isinstance(identity, SignInIdentity):
            self._identity = identity
            self.status_label.setText('Google identity verified. Drive remains disabled.')
            self._update_controls()
            self.identity_changed.emit(identity)
            self.attempt_finished.emit('success')
        else:
            self.status_label.setText(SAFE_MESSAGES.get(code,
                'Sign-in could not be verified. Check your Desktop client configuration and try again.'))
            self._update_controls()
            self.attempt_finished.emit(code or 'failed')

    def cancel_signin(self):
        if self._closed or not self.busy or self._cancelling:
            return
        self._generation += 1  # Any queued completion becomes stale immediately.
        self._cancelling = True
        self._pending_result = None
        self._cancel_event.set()
        self.status_label.setText('Cancelling sign-in…')
        self._update_controls()
        self._settle()

    def sign_out(self):
        if self._closed or self.busy:
            return
        self._identity = None
        self.status_label.setText('Signed out locally. No saved tokens to remove.')
        self._update_controls()
        self.identity_changed.emit(None)

    def shutdown(self, timeout=1.5):
        """Invalidate first, then bounded cooperative join; never terminate a thread.

        The launcher preserves its disposable profile if a backend fails to exit.
        Python workers are daemonized as a final process-exit safeguard.
        """
        if not self._closed:
            self._closed = True
            self._generation += 1
            self._pending_result = None
            self._identity = None
            self._config_path = None
            self.identity_label.clear()
            self.config_label.setText('No Desktop client configuration selected')
            self._poll.stop()
            if self._cancel_event is not None:
                self._cancel_event.set()
            self._update_controls()
        if self._thread is not None:
            self._thread.join(max(0.0, min(float(timeout), 1.5)))
            if not self._thread.is_alive():
                self._thread = None
        return self._thread is None

    def reject(self):
        self.shutdown(0)
        super().reject()

    def closeEvent(self, event):
        self.shutdown(0)
        super().closeEvent(event)
