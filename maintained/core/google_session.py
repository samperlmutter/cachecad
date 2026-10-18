"""Production Google Drive sessions: verified OAuth, DPAPI persistence, safe refresh.

No live grant or app-owned client is provided by this module. The distributor must
supply its own reviewed Desktop OAuth client and scopes. All network operations
use fixed Google endpoints, no redirects/proxies, TLS verification, five-second
connect/read bounds and no hidden retries. Requests/DNS are not hard real-time
cancellable. Sign-out invalidates retained credential objects and removes only the
local cache. Revocation is a separate explicit disconnect operation.
"""

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
import threading
import time

from .desktop_signin import (AuthorizationResult, DesktopAuthorization, DesktopSignInError,
                             SignInIdentity, DEFAULT_DRIVE_SCOPES,
                             TOKEN_URI, canonical_scopes, load_client_config,
                             validate_client_config, validate_scopes, _quiet_oauth_logs)
if __package__ == 'core':
    from utils.windows_token_store import WindowsTokenStore, TokenStoreError
else:
    from ..utils.windows_token_store import WindowsTokenStore, TokenStoreError

try:
    from google.oauth2.credentials import Credentials as _Credentials
    from google.auth.exceptions import RefreshError as _RefreshError
except ImportError:
    _Credentials = None
    _RefreshError = Exception


DEFAULT_SCOPES = DEFAULT_DRIVE_SCOPES
REVOKE_URI = 'https://oauth2.googleapis.com/revoke'
_SCHEMA = 1
_MAX_RESPONSE = 131072
_MESSAGES = {
    'configuration': 'The app-owned Google sign-in configuration is unavailable or invalid.',
    'dependencies': 'The Google sign-in dependencies are unavailable.',
    'platform': 'Protected Google sign-in is supported on Windows only.',
    'storage': 'The protected Google sign-in cache could not be read or updated.',
    'reauth_required': 'Google access expired or was revoked. Sign in again.',
    'network': 'Google could not be reached. Please try again.',
    'provider': 'Google could not complete the request. Please try again.',
    'signed_out': 'Sign in to Google to continue.',
    'cancelled': 'Google sign-in was cancelled.',
    'busy': 'A Google sign-in operation is already running.',
}


class SessionError(_RefreshError):
    """Only a fixed safe code/message may leave this boundary."""

    def __init__(self, code='provider'):
        self.code = code if code in _MESSAGES else 'provider'
        super().__init__(_MESSAGES[self.code])


def _check_cancel(event):
    if event.is_set():
        raise SessionError('cancelled')


def _token(value):
    return (isinstance(value, str) and 1 <= len(value) <= 32768
            and all(33 <= ord(char) <= 126 for char in value))


def _json_unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _post_form(endpoint, data, cancel_event):
    """One HTTPS request, with bounded response and no credential-bearing redirects."""
    if endpoint not in (TOKEN_URI, REVOKE_URI):
        raise SessionError('configuration')
    _check_cancel(cancel_event)
    failure = 'network'
    result = None
    try:
        import requests
        with _quiet_oauth_logs(), requests.Session() as session:
            session.trust_env = False
            with session.post(endpoint, data=data, timeout=(5.0, 5.0), allow_redirects=False,
                              verify=True, stream=True) as response:
                _check_cancel(cancel_event)
                raw = bytearray()
                deadline = time.monotonic() + 10.0
                for part in response.iter_content(chunk_size=4096):
                    _check_cancel(cancel_event)
                    raw.extend(part)
                    if len(raw) > _MAX_RESPONSE or time.monotonic() >= deadline:
                        raise SessionError('provider')
                _check_cancel(cancel_event)
                payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_json_unique) if raw else {}
                if not isinstance(payload, dict):
                    raise SessionError('provider')
                result = response.status_code, payload
    except SessionError as error:
        failure = error.code
    except Exception:
        pass
    if result is None:
        raise SessionError(failure)
    return result


def _valid_record(record, config, scopes):
    """Reject malformed, stale-schema, cross-client or cross-scope caches."""
    try:
        if not isinstance(record, dict) or set(record) != {
                'schema', 'client_id', 'scopes', 'identity', 'access_token',
                'refresh_token', 'expiry'}:
            raise ValueError
        if type(record['schema']) is not int or record['schema'] != _SCHEMA:
            raise ValueError
        if record['client_id'] != config['installed']['client_id']:
            raise ValueError
        stored_scopes = record['scopes']
        if (not isinstance(stored_scopes, list) or len(stored_scopes) != len(scopes)
                or canonical_scopes(stored_scopes) != canonical_scopes(scopes)):
            raise ValueError
        identity = record['identity']
        if not isinstance(identity, dict) or set(identity) != {'email', 'sub'}:
            raise ValueError
        # Identity is authenticated by DPAPI and was verified before first save.
        import re
        if (not isinstance(identity['email'], str) or len(identity['email']) > 254
                or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+",
                                    identity['email'])
                or not isinstance(identity['sub'], str)
                or not re.fullmatch(r'[\x21-\x7e]{1,255}', identity['sub'])):
            raise ValueError
        if not _token(record['access_token']) or not _token(record['refresh_token']):
            raise ValueError
        expiry = record['expiry']
        if (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                or not math.isfinite(expiry) or not 0 < expiry <= time.time() + 2678400):
            raise ValueError
        return SignInIdentity(**identity), datetime.fromtimestamp(expiry, timezone.utc).replace(
            tzinfo=None)
    except Exception:
        pass
    raise SessionError('storage')


class ManagedCredentials(_Credentials if _Credentials is not None else object):
    """Actual google.oauth2 Credentials with locked, persist-before-use refresh."""

    def __init__(self, source, owner):
        if _Credentials is None:
            raise SessionError('dependencies')
        super().__init__(token=source.token, refresh_token=source.refresh_token,
                         token_uri=TOKEN_URI, client_id=owner._config['installed']['client_id'],
                         client_secret=owner._config['installed']['client_secret'],
                         scopes=owner._scopes, expiry=source.expiry)
        self._owner = owner
        self._enabled = True
        self._generation = 0
        self._failure_until = 0.0
        self._failure_code = None
        self._last_refresh_at = 0.0

    def __repr__(self):
        return '<ManagedCredentials protected>'

    @property
    def valid(self):
        return self._enabled and not self._owner._closed.is_set() and super().valid

    def _require_active(self):
        if not self._enabled or self._owner._closed.is_set():
            raise SessionError('signed_out')
        _check_cancel(self._owner._lifecycle_event)

    def _invalidate(self):
        self._enabled = False
        self.token = None
        self._refresh_token = None
        self._id_token = None
        self.expiry = None

    def apply(self, headers, token=None):
        with self._owner._lock:
            self._require_active()
            super().apply(headers, token=token)

    def before_request(self, request, method, url, headers):
        with self._owner._lock:
            self._require_active()
        # Official SDK checks expiry and invokes this object's locked refresh().
        # apply() rechecks liveness, so sign-out between these steps fails closed.
        super().before_request(request, method, url, headers)

    def refresh(self, request):
        # A caller-supplied transport cannot redirect/send our refresh token elsewhere.
        generation = self._generation
        failure = None
        with self._owner._lock:
            try:
                self._require_active()
                if self.valid and (generation != self._generation
                                   or time.monotonic() - self._last_refresh_at < 2.0):
                    # HTTP workers can report 401 for the previous token after a
                    # sibling already refreshed it. Reuse that just-issued token
                    # rather than serializing many redundant grant requests.
                    return
                if time.monotonic() < self._failure_until:
                    raise SessionError(self._failure_code)
                self._refresh_locked()
                return
            except SessionError as error:
                failure = error.code
            except Exception:
                failure = 'provider'
            if failure in ('network', 'provider'):
                self._failure_until = time.monotonic() + 2.0
                self._failure_code = failure
        if failure in ('reauth_required', 'storage'):
            self._owner._notify_invalidation(failure)
        raise SessionError(failure)

    def _refresh_locked(self):
        owner = self._owner
        status, payload = _post_form(TOKEN_URI, {
            'grant_type': 'refresh_token', 'refresh_token': self._refresh_token,
            'client_id': self._client_id, 'client_secret': self._client_secret,
            'scope': ' '.join(owner._scopes),
        }, owner._lifecycle_event)
        self._require_active()
        if payload.get('error') == 'invalid_grant':
            self._invalidate()
            owner._credentials = None
            owner._identity = None
            try:
                owner._store.clear()
            except TokenStoreError:
                raise SessionError('storage') from None
            raise SessionError('reauth_required')
        if status != 200:
            raise SessionError('network' if status == 429 or status >= 500 else 'provider')
        access = payload.get('access_token')
        refresh = payload.get('refresh_token', self._refresh_token)
        expires = payload.get('expires_in')
        granted = payload.get('scope', ' '.join(owner._scopes))
        if (not _token(access) or not _token(refresh)
                or payload.get('token_type', '').lower() != 'bearer'
                or isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not math.isfinite(expires) or not 0 < expires <= 2678400
                or not isinstance(granted, str)
                or canonical_scopes(granted.split()) != canonical_scopes(owner._scopes)):
            raise SessionError('provider')
        expiry = datetime.fromtimestamp(time.time() + expires, timezone.utc).replace(tzinfo=None)
        record = owner._record(owner._identity, access, refresh, expiry)
        _check_cancel(owner._lifecycle_event)
        try:
            owner._store.save(record)
        except TokenStoreError:
            # Do not continue with a rotated refresh token that was not saved.
            self._invalidate()
            owner._credentials = None
            owner._identity = None
            raise SessionError('storage') from None
        # Shutdown can happen without acquiring our lock while disk I/O runs.
        # Keep the rotated encrypted cache but never restore cleared RAM tokens.
        self._require_active()
        self.token, self._refresh_token, self.expiry = access, refresh, expiry
        self._generation += 1
        self._last_refresh_at = time.monotonic()
        self._failure_until = 0.0
        self._failure_code = None


class GoogleSession:
    """One app-owned client, one Windows profile cache, one active account.

    restore() never starts a browser or network call. authorize() is user-initiated.
    The owning GUI must run network operations off its UI thread. cancel() is safe
    from the UI thread and prevents an in-flight authorization from committing.
    """

    def __init__(self, client_config, *, scopes=DEFAULT_SCOPES, store=None,
                 on_invalidation=None):
        if _Credentials is None:
            raise SessionError('dependencies')
        self._config = (validate_client_config(client_config) if isinstance(client_config, Mapping)
                        else load_client_config(client_config))
        self._scopes = validate_scopes(scopes)
        failure = None
        try:
            self._store = store if store is not None else WindowsTokenStore()
        except TokenStoreError as error:
            failure = error.code
        if failure:
            raise SessionError(failure)
        self._lock = threading.RLock()
        self._credentials = None
        self._identity = None
        self._attempt = None
        self._epoch = 0
        self._lifecycle_event = threading.Event()
        self._closed = threading.Event()
        self.on_invalidation = on_invalidation

    def __repr__(self):
        return '<GoogleSession protected>'

    @property
    def credentials(self):
        if self._closed.is_set():
            return None
        with self._lock:
            return self._credentials

    @property
    def identity(self):
        if self._closed.is_set():
            return None
        with self._lock:
            return self._identity

    def _notify_invalidation(self, code):
        callback = self.on_invalidation
        if callback is not None:
            try:
                callback(code)
            except Exception:
                # A UI notification must never expose token-bearing context or
                # replace the actual safe authentication failure.
                pass

    def _record(self, identity, access, refresh, expiry):
        return {'schema': _SCHEMA, 'client_id': self._config['installed']['client_id'],
                'scopes': list(self._scopes),
                'identity': {'email': identity.email, 'sub': identity.sub},
                'access_token': access, 'refresh_token': refresh,
                'expiry': expiry.replace(tzinfo=timezone.utc).timestamp()}

    def restore(self):
        failure = None
        with self._lock:
            if self._closed.is_set():
                raise SessionError('signed_out')
            if self._attempt is not None:
                raise SessionError('busy')
            try:
                record = self._store.load()
                if self._closed.is_set():
                    raise SessionError('signed_out')
                if record is None:
                    return None
                identity, expiry = _valid_record(record, self._config, self._scopes)
                source = _Credentials(token=record['access_token'],
                                      refresh_token=record['refresh_token'], expiry=expiry)
                credentials = ManagedCredentials(source, self)
                if self._credentials is not None:
                    self._credentials._invalidate()
                self._lifecycle_event = threading.Event()
                self._identity, self._credentials = identity, credentials
                return AuthorizationResult(identity, credentials)
            except TokenStoreError:
                failure = 'storage'
            except SessionError as error:
                failure = error.code
            finally:
                if self._closed.is_set():
                    if self._credentials is not None:
                        self._credentials._invalidate()
                    self._credentials = None
                    self._identity = None
        raise SessionError(failure)

    def authorize(self, cancel_event=None, timeout=120):
        event = cancel_event if cancel_event is not None else threading.Event()
        with self._lock:
            if self._closed.is_set():
                raise SessionError('signed_out')
            if self._attempt is not None:
                raise SessionError('busy')
            _check_cancel(event)
            self._attempt = event
            self._epoch += 1
            epoch = self._epoch
        failure = None
        result = None
        try:
            result = DesktopAuthorization(self._config, scopes=self._scopes).run(
                cancel_event=event, timeout=timeout)
            with self._lock:
                _check_cancel(event)
                if epoch != self._epoch:
                    raise SessionError('cancelled')
                credentials = ManagedCredentials(result.credentials, self)
                record = self._record(result.identity, credentials.token,
                                      credentials.refresh_token, credentials.expiry)
                # Preserve only an established in-memory session for rollback.
                # An unreadable/stale cache must not block explicit fresh login.
                previous_record = (self._record(
                    self._identity, self._credentials.token,
                    self._credentials.refresh_token, self._credentials.expiry)
                    if self._credentials is not None else None)
                _check_cancel(event)
                self._store.save(record)
                if event.is_set() or self._closed.is_set():
                    # An independently supplied Event may change during the atomic
                    # write. Restore the preceding session before reporting cancel.
                    if previous_record is None:
                        self._store.clear()
                    else:
                        self._store.save(previous_record)
                    credentials._invalidate()
                    raise SessionError('cancelled')
                # cancel() takes the same lock. A completed atomic commit wins or
                # a preceding cancellation prevents any write; no stale overwrite.
                if self._credentials is not None:
                    self._credentials._invalidate()
                self._lifecycle_event = threading.Event()
                self._identity, self._credentials = result.identity, credentials
                return AuthorizationResult(result.identity, credentials)
        except TokenStoreError:
            failure = SessionError('storage')
        except SessionError as error:
            failure = SessionError(error.code)
        except DesktopSignInError as error:
            failure = DesktopSignInError(error.code)
        except Exception:
            failure = SessionError('provider')
        finally:
            with self._lock:
                if self._attempt is event:
                    self._attempt = None
                if self._closed.is_set():
                    if self._credentials is not None:
                        self._credentials._invalidate()
                    self._credentials = None
                    self._identity = None
        raise failure

    def cancel(self):
        # Do not block the UI behind a network/storage operation. The Event is
        # this attempt's cancellation generation and is checked at commit.
        pending = self._attempt
        if pending is not None:
            pending.set()

    def close(self):
        """Terminal, nonblocking shutdown; preserve the encrypted remembered login."""
        self._closed.set()
        self._lifecycle_event.set()
        self.cancel()
        credentials = self._credentials
        if credentials is not None:
            credentials._invalidate()
        self._credentials = None
        self._identity = None

    def sign_out(self):
        self._lifecycle_event.set()
        self.cancel()
        failure = False
        with self._lock:
            if self._credentials is not None:
                self._credentials._invalidate()
            self._credentials = None
            self._identity = None
            try:
                self._store.clear()
            except TokenStoreError:
                failure = True
        if failure:
            raise SessionError('storage')

    def disconnect(self, cancel_event=None):
        """Explicit remote Google revocation; a failure preserves remembered login."""
        event = cancel_event if cancel_event is not None else threading.Event()
        failure = None
        with self._lock:
            if self._attempt is not None:
                raise SessionError('busy')
            if self._credentials is None:
                raise SessionError('signed_out')
            self._attempt = event
            try:
                status, payload = _post_form(REVOKE_URI,
                                             {'token': self._credentials.refresh_token}, event)
                # A previously revoked/invalid token is already disconnected.
                if status != 200 and not (status == 400 and payload.get('error') == 'invalid_token'):
                    raise SessionError('network' if status == 429 or status >= 500 else 'provider')
                self.sign_out()
                return
            except SessionError as error:
                failure = error.code
            finally:
                if self._attempt is event:
                    self._attempt = None
        raise SessionError(failure)
