# Reviewed replacement of recovered auth lifecycle: actual verified Google sessions.
from PySide6.QtCore import QObject, QRunnable, Signal, QTimer, Qt
import threading
from core.signal_manager import signal_manager

SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/userinfo.email', 'openid']

SAFE_MESSAGES = {
    'configuration': 'Google sign-in is not configured by this application’s owner. Contact the distributor.',
    'dependencies': 'Google sign-in dependencies are unavailable. Reinstall this application.',
    'platform': 'Secure remembered sign-in requires Windows for this build.',
    'storage': 'The protected sign-in could not be saved or read. Try again or contact support.',
    'reauth_required': 'Your Google session needs a new sign-in.',
    'network': 'Google could not be reached. Check your connection and try again.',
    'revoked': 'Google access was revoked. Sign in again to reconnect.',
    'signed_out': 'Signed out on this computer.',
    'busy': 'An account operation is already running.',
    'browser': 'Your browser could not be opened. Try again.',
    'callback': 'The Google response could not be validated. Try again.',
    'denied': 'Google access was declined. You can try again.',
    'provider': 'Google could not complete sign-in. Try again.',
    'identity': 'Google identity verification failed. Try again.',
    'cancelled': 'Sign-in was cancelled.',
    'timeout': 'Sign-in timed out. Try again.',
    'failed': 'The account operation could not be completed. Try again.',
}


class AuthManager(QObject):
    status_changed = Signal(str, str)
    _completed = Signal(int, str, object, str)
    _invalidated = Signal(int, str)

    def __init__(self, configuration=None):
        super().__init__()
        from utils.application_mode import current
        self.configuration = configuration or current()
        if self.configuration is None:
            raise RuntimeError('Start the real application with application/launch.py')
        self.creds = None
        self.identity = None
        self._session = None
        self._auth_in_progress = False
        self._current_runnable = None
        self._thread = None
        self._cancel_event = None
        self._pending_result = None
        self._generation = 0
        self._closed = False
        self._clear_pending = False
        self._restart_required = False
        self.last_invalidation = None
        self.state = 'signed_out' if self.configuration.client_config_path.is_file() else 'setup_missing'
        self.message = (SAFE_MESSAGES['configuration'] if self.state == 'setup_missing'
                        else 'Sign in to connect your Google Drive.')
        self._completed.connect(self._receive, Qt.QueuedConnection)
        self._invalidated.connect(self._on_invalidated, Qt.QueuedConnection)
        self._poll = QTimer(self)
        self._poll.setInterval(25)
        self._poll.timeout.connect(self._settle)

    def get_creds(self):
        return self.creds

    def check_creds(self, retry=True):
        # Refresh is owned by ManagedCredentials at the actual Google API call.
        # This method never blocks Qt or recursively opens a browser.
        return self.creds is not None and not self._closed and self.state == 'signed_in'

    def refresh_token(self, creds=None):
        # Compatibility entrypoint: re-read the owned protected session only.
        return self.restore()

    def has_internet(self, host='8.8.8.8', port=53, timeout=3):
        # Do not probe unrelated DNS services or mutate global socket defaults.
        return self.check_creds()

    def sign_out(self):
        return self._start('sign_out')

    def authorize(self):
        return self._start('authorize')

    def _auth_worker(self, generation, operation, cancel_event):
        result, code = None, ''
        known_errors = ()
        try:
            from core.google_session import GoogleSession, SessionError, AuthorizationResult
            from core.desktop_signin import DesktopSignInError
            from utils.windows_token_store import SecureTokenStore, TokenStoreError
            known_errors = (DesktopSignInError, SessionError, TokenStoreError)
            if self._session is None:
                self._session = GoogleSession(self.configuration.client_config_path,
                    store=SecureTokenStore(self.configuration.session_path))
            if self._closed:
                self._session.close()
                raise SessionError('cancelled')
            if operation == 'authorize':
                result = self._session.authorize(cancel_event=cancel_event, timeout=120)
            elif operation == 'restore':
                result = self._session.restore()
            elif operation == 'disconnect':
                self._session.disconnect(cancel_event=cancel_event)
            elif operation == 'sign_out':
                self._session.sign_out()
            else:
                raise RuntimeError('Unknown account operation')
            if result is not None and not isinstance(result, AuthorizationResult):
                result, code = None, 'identity'
        except Exception as error:
            # Only fixed allowlisted codes cross the worker boundary.
            proposed = getattr(error, 'code', '')
            code = proposed if isinstance(error, known_errors) and proposed in SAFE_MESSAGES else ('dependencies' if isinstance(error, ImportError) else 'failed')
        if cancel_event.is_set() and operation == 'restore':
            result, code = None, 'cancelled'
        try:
            self._completed.emit(generation, operation, result, code)
        except RuntimeError:
            pass  # The owner has already been destroyed during shutdown.

    def restore(self):
        return self._start('restore')

    def disconnect(self):
        return self._start('disconnect')

    def _start(self, operation):
        if self._closed or self._auth_in_progress or self._restart_required:
            return False
        if operation in ('authorize', 'restore') and not self.configuration.client_config_path.is_file():
            self._set_state('setup_missing', SAFE_MESSAGES['configuration'])
            return False
        if operation in ('sign_out', 'disconnect') and self.creds is None and not (operation == 'sign_out' and self._clear_pending):
            return False
        self._generation += 1
        self._cancel_event = threading.Event()
        self._pending_result = None
        self._auth_in_progress = True
        labels = {'authorize': 'Finish signing in in your browser. You can cancel here.',
                  'restore': 'Restoring your protected Google session…',
                  'sign_out': 'Signing out on this computer…',
                  'disconnect': 'Disconnecting this application from Google…'}
        self._set_state(operation, labels[operation])
        if operation == 'authorize':
            signal_manager.auth_started.emit()
        self._thread = threading.Thread(target=self._auth_worker,
            args=(self._generation, operation, self._cancel_event),
            name='cachecad-google-account', daemon=True)
        self._thread.start()
        self._poll.start()
        return True

    def cancel(self):
        if self._closed or not self._auth_in_progress or self.state not in ('authorize', 'restore', 'cancelling'):
            return False
        self._cancel_event.set()
        if self._session is not None:
            self._session.cancel()
        self._set_state('cancelling', 'Cancelling sign-in…')
        return True

    def _receive(self, generation, operation, result, code):
        if self._closed or generation != self._generation or not self._auth_in_progress or self._pending_result is not None:
            return
        self._pending_result = (operation, result, code)
        self._settle()

    def _settle(self):
        if self._closed or self._pending_result is None or self._thread is None or self._thread.is_alive():
            return
        self._poll.stop()
        operation, result, code = self._pending_result
        self._pending_result = None
        self._thread = None
        self._auth_in_progress = False
        cancelled_after_commit = self._cancel_event.is_set() and operation == 'authorize' and result is not None
        if self._cancel_event.is_set() and operation == 'restore':
            result, code = None, 'cancelled'
        self._cancel_event = None
        if code:
            if operation in ('sign_out', 'disconnect') and self._session is not None and self._session.credentials is None:
                self.creds = self.identity = None
                self._clear_pending = code == 'storage'
                signal_manager.sign_out.emit()
                message = ('Signed out here, but the saved sign-in could not be removed. Retry forgetting this computer.'
                           if self._clear_pending else SAFE_MESSAGES.get(code, SAFE_MESSAGES['failed']))
                self._set_state('cleanup_required' if self._clear_pending else 'error', message)
            else:
                state = 'signed_in' if operation in ('disconnect', 'sign_out') and self.creds is not None else 'error'
                self._set_state(state, SAFE_MESSAGES.get(code, SAFE_MESSAGES['failed']))
            signal_manager.auth_failed.emit(self.message)
            return
        if operation in ('sign_out', 'disconnect') or result is None:
            self.creds = self.identity = None
            self._clear_pending = False
            message = ('Google access disconnected.' if operation == 'disconnect'
                       else 'Signed out on this computer. Google access is unchanged.' if operation == 'sign_out'
                       else 'Sign in to connect your Google Drive.')
            self._set_state('signed_out', message)
            signal_manager.sign_out.emit()
            return
        self.creds, self.identity = result.credentials, result.identity
        self._clear_pending = False
        self._session.on_invalidation = lambda code, ticket=self._generation: self._invalidated.emit(ticket, code)
        self._set_state('signed_in', 'Sign-in completed before cancellation could take effect.' if cancelled_after_commit else 'Connected to Google Drive.')
        signal_manager.auth_success.emit(self.creds)

    def _on_invalidated(self, generation, code):
        if self._closed or generation != self._generation:
            return
        self.creds = self.identity = None
        self._clear_pending = code == 'storage'
        self.last_invalidation = code
        signal_manager.sign_out.emit()
        message = SAFE_MESSAGES.get(code, SAFE_MESSAGES['reauth_required'])
        if self._restart_required:
            message = 'Google access expired during a transfer. Close and reopen the application to sign in safely.'
        self._set_state('restart_required' if self._restart_required else 'cleanup_required' if self._clear_pending else 'error', message)
        signal_manager.auth_failed.emit(message)

    def require_restart(self):
        self._restart_required = True
        self._set_state('restart_required', 'Google access expired during a transfer. Close and reopen the application to sign in safely.')

    def _set_state(self, state, message):
        self.state, self.message = state, message
        if not self._closed:
            self.status_changed.emit(state, message)

    def shutdown(self, timeout=1.5):
        if not self._closed:
            self._closed = True
            self._generation += 1
            self._poll.stop()
            self._pending_result = None
            self.creds = self.identity = None
            if self._cancel_event is not None:
                self._cancel_event.set()
            if self._session is not None:
                self._session.close()
        if self._thread is not None:
            self._thread.join(max(0.0, min(float(timeout), 1.5)))
            if self._thread.is_alive():
                return False
            self._thread = None
        return True


class _FunctionRunnable(QRunnable):
    """Simple wrapper to run any function in a thread."""
    def __init__(self, func):
        super().__init__()
        self.func = func

    def run(self):
        try:
            self.func()
        except Exception:
            pass
