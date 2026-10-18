from PySide6.QtCore import QObject, QRunnable, QEventLoop
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
import os
import webbrowser
import traceback
import sys
import socket
import logging
import threading
from core.signal_manager import signal_manager
from core.thread_manager import thread_pool
from utils.oauth_credential_import import OauthCredentialImport
from utils.paths import APP_DATA_DIR
from core.oauth_server import OAuthServer

SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/userinfo.email', 'openid']

class AuthManager(QObject):
    def __init__(self):
        super().__init__()
        self.token_file = APP_DATA_DIR / 'token.json'
        self.cred_path = APP_DATA_DIR / 'creds.json'
        self.creds = None
        self._auth_in_progress = False
        self._current_runnable = None

    def get_creds(self):
        return self.creds

    def check_creds(self, retry=True):
        try:
            logging.debug('checking creds')
            if self.creds and self.creds.valid:
                return True
            if self.creds and self.creds.expired and self.creds.refresh_token:
                refresh_result = False
                loop = QEventLoop()
                def on_refresh_finished(result, _):
                    nonlocal refresh_result
                    refresh_result = result
                    loop.quit()
                signal_manager.token_refreshed.connect(on_refresh_finished)
                thread_pool.start(_FunctionRunnable(lambda: self.refresh_token(self.creds)))
                loop.exec()
                signal_manager.token_refreshed.disconnect(on_refresh_finished)
                return refresh_result
            print('not eligible for refresh')
            if os.path.exists(self.token_file):
                with open(self.token_file, 'rb') as token:
                    self.creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)
            if retry:
                print('retrying cred check')
                return self.check_creds(False)
            return False
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f'Exception in check_creds:{e}\n{tb}')
            return False

    def refresh_token(self, creds):
        try:
            creds.refresh(Request())
            self.creds = creds
            with open(self.token_file, 'w') as token:
                token.write(creds.to_json())
            signal_manager.token_refreshed.emit(self.creds.valid, self.creds)
        except RefreshError as e:
            logging.error(f'RefreshError in refresh_token: {e}')
            self.sign_out()
            signal_manager.token_refreshed.emit(False, None)
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f'Exception in refresh_token:\n{tb}')
            signal_manager.token_refreshed.emit(False, None)

    def has_internet(self, host='8.8.8.8', port=53, timeout=3):
        try:
            socket.setdefaulttimeout(timeout)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.connect((host, port))
            print('Internet is connected')
            signal_manager.has_internet.emit(True)
            return True
        except OSError:
            print('No Internet')
            signal_manager.has_internet.emit(False)
            return False

    def sign_out(self):
        try:
            self.token_file.unlink()
        except Exception as e:
            tb = traceback.format_exc()
            print(f'Exception in auth_task:\n{tb}', file=sys.stderr)

    def authorize(self):
        """
        Starts the authorization process in a new, daemon thread.
        This thread will be automatically terminated when the main
        application window is closed.
        """
        signal_manager.auth_started.emit()
        self._auth_in_progress = True
        self._auth_thread = threading.Thread(target=self._auth_worker, daemon=True)
        self._auth_thread.start()

    def _auth_worker(self):
        if not self.has_internet():
            print('Auth stopped - no internet')
            return
        try:
            creds = None
            if os.path.exists(self.token_file):
                with open(self.token_file, 'rb') as token:
                    creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)
            if creds and creds.valid:
                self.creds = creds
                signal_manager.auth_success.emit(creds)
                return
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self.creds = creds
                with open(self.token_file, 'w') as token:
                    token.write(creds.to_json())
                signal_manager.auth_success.emit(creds)
                return
            importer = OauthCredentialImport(self.cred_path)
            flow = InstalledAppFlow.from_client_secrets_file(self.cred_path, SCOPES)
            importer.delete()
            flow.redirect_uri = 'http://localhost:8765/'
            auth_url, _ = flow.authorization_url(prompt='consent')
            webbrowser.open(auth_url)
            server = server = OAuthServer(port=8765)
            code = server.wait_for_code(timeout=120)
            if not code:
                signal_manager.auth_failed.emit('Authorization timed out or was cancelled.')
                return
            flow.fetch_token(code=code)
            creds = flow.credentials
            self.creds = creds
            with open(self.token_file, 'w') as token:
                token.write(creds.to_json())
            signal_manager.auth_success.emit(creds)
            return creds
        except RefreshError as e:
            logging.error(f'RefreshError in auth_task: {e}')
            self.sign_out()
            self.authorize()
        except Exception as e:
            tb = traceback.format_exc()
            if hasattr(e, 'response') and hasattr(e.response, 'status'):
                tb += f'\nHTTP Status: {e.response.status}'
            print(f'Exception in auth_task:\n{tb}', file=sys.stderr)
            signal_manager.auth_failed.emit(f'{str(e)}\n{tb}')
        finally:
            self._auth_in_progress = False

class _FunctionRunnable(QRunnable):
    """Simple wrapper to run any function in a thread."""
    def __init__(self, func):
        super().__init__()
        self.func = func

    def run(self):
        try:
            self.func()
        except Exception as e:
            logging.error(f'Error in _FunctionRunnable: {e}\n{traceback.format_exc()}')
