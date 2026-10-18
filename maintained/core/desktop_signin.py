"""Verified Google Desktop authorization, separate from recovered auth code.

No import-time effects, Qt, token storage, refresh, or remote userinfo call.
``DesktopSignIn`` remains an explicit identity-only development helper.
``DesktopAuthorization`` uses app-owned client configuration and returns verified
identity with real OAuth credentials for the production session manager. It does
not persist them. Injected effects are for offline tests; production defaults use
the hash-locked official Google libraries.

Cancellation polls the loopback listener and is checked around HTTPS operations.
HTTPS has bounded connect/read timeouts; requests/DNS do not provide a hard total
wall-clock cancellation guarantee. A GUI must ignore results after cancellation.
"""

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import logging
import math
from pathlib import Path
import re
import secrets
import socket
import stat
import threading
import time
from urllib.parse import parse_qsl, urlsplit
import webbrowser


AUTH_URI = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URI = 'https://oauth2.googleapis.com/token'
CERTS_URI = 'https://www.googleapis.com/oauth2/v1/certs'
SCOPES = ('openid', 'https://www.googleapis.com/auth/userinfo.email')
DEFAULT_DRIVE_SCOPES = ('openid', 'email', 'https://www.googleapis.com/auth/drive')
_MAX_CONFIG_BYTES = 65536
_POLL = 0.1
_NETWORK_TIMEOUT = 5.0
_ERRORS = {
    'configuration': 'Choose a valid Google Desktop OAuth client JSON file.',
    'dependencies': 'The Google sign-in dependencies are unavailable.',
    'busy': 'A sign-in attempt is already running.',
    'browser': 'The browser could not be opened. Please try again.',
    'callback': 'The sign-in response could not be validated. Please try again.',
    'denied': 'Google sign-in was declined.',
    'provider': 'Google could not complete sign-in. Please try again.',
    'identity': 'Google identity verification failed. Please try again.',
    'cancelled': 'Sign-in was cancelled.',
    'timeout': 'Sign-in timed out. Please try again.',
    'failed': 'Sign-in could not be completed. Please try again.',
}


class DesktopSignInError(Exception):
    """A fixed, token-safe code/message. Never expose a provider exception."""

    def __init__(self, code='failed'):
        self.code = code if code in _ERRORS else 'failed'
        super().__init__(_ERRORS[self.code])


class SignInCancelled(DesktopSignInError):
    def __init__(self):
        super().__init__('cancelled')


class SignInTimeout(DesktopSignInError):
    def __init__(self):
        super().__init__('timeout')


@dataclass(frozen=True)
class SignInIdentity:
    email: str
    sub: str


@dataclass(frozen=True)
class AuthorizationResult:
    """Verified identity with credentials deliberately excluded from repr."""

    identity: SignInIdentity
    credentials: object = field(repr=False)


def validate_scopes(scopes):
    """App-owner configuration only: OIDC plus explicit Google API scopes."""
    try:
        if isinstance(scopes, (str, bytes)):
            raise ValueError
        result = tuple(scopes)
        if not 2 <= len(result) <= 32 or len(set(result)) != len(result):
            raise ValueError
        if 'openid' not in result or not {'email', SCOPES[1]}.intersection(result):
            raise ValueError
        for scope in result:
            if scope not in ('openid', 'email') and not re.fullmatch(
                    r'https://www\.googleapis\.com/auth/[A-Za-z0-9._-]{1,128}', scope):
                raise ValueError
        return result
    except Exception:
        pass
    raise DesktopSignInError('configuration')


def canonical_scopes(scopes):
    return frozenset(SCOPES[1] if value == 'email' else value for value in scopes)


def validate_client_config(config):
    """Validate and copy a Desktop configuration; discard unneeded metadata.

    Google's older official authorization URI is accepted and canonicalized.
    A Desktop client is public; its optional client_secret is not a server secret.
    An empty value supports the pinned library's required dictionary key.
    """
    try:
        if not isinstance(config, Mapping) or set(config) != {'installed'}:
            raise ValueError
        installed = config['installed']
        allowed = {'client_id', 'client_secret', 'auth_uri', 'token_uri', 'redirect_uris',
                   'project_id', 'auth_provider_x509_cert_url'}
        if not isinstance(installed, Mapping) or set(installed) - allowed:
            raise ValueError
        client_id = installed['client_id']
        if not isinstance(client_id, str) or not re.fullmatch(
                r'[A-Za-z0-9_-]{1,256}\.apps\.googleusercontent\.com', client_id):
            raise ValueError
        if installed['auth_uri'] not in (AUTH_URI, 'https://accounts.google.com/o/oauth2/auth'):
            raise ValueError
        if installed['token_uri'] != TOKEN_URI:
            raise ValueError
        if installed.get('auth_provider_x509_cert_url', CERTS_URI) != CERTS_URI:
            raise ValueError
        secret = installed.get('client_secret', '')
        if not isinstance(secret, str) or len(secret) > 4096 or any(
                ord(char) < 33 or ord(char) > 126 for char in secret):
            raise ValueError
        redirects = installed['redirect_uris']
        if not isinstance(redirects, (list, tuple)) or not 1 <= len(redirects) <= 8:
            raise ValueError
        for uri in redirects:
            if not isinstance(uri, str) or len(uri) > 256:
                raise ValueError
            parsed = urlsplit(uri)
            if (parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1')
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                    or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
                raise ValueError
        return {'installed': {'client_id': client_id, 'client_secret': secret,
                              'auth_uri': AUTH_URI, 'token_uri': TOKEN_URI,
                              'redirect_uris': list(redirects)}}
    except Exception:
        pass
    raise DesktopSignInError('configuration')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def load_client_config(path):
    """Read only the caller-selected bounded JSON file, without copying it."""
    try:
        if not stat.S_ISREG(Path(path).stat().st_mode):
            raise ValueError
        with Path(path).open('rb') as stream:
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ValueError
        config = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
        return validate_client_config(config)
    except Exception:
        pass
    raise DesktopSignInError('configuration')


def _check_pending(cancel_event, deadline):
    if cancel_event.is_set():
        raise SignInCancelled()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SignInTimeout()
    return remaining


def _parse_callback(target, expected_state):
    """Validate the exact callback path, singular state and code/error pair."""
    try:
        if not isinstance(target, str) or len(target) > 8192 or not target.startswith('/'):
            raise ValueError
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.path != '/' or parsed.fragment:
            raise ValueError
        if re.search(r'%(?![0-9a-fA-F]{2})', parsed.query):
            raise ValueError
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True,
                          errors='strict', max_num_fields=24)
        params = _unique_object(pairs)
        state = params.get('state', '')
        if not state.isascii() or not hmac.compare_digest(state, expected_state):
            raise ValueError
        # Unknown response fields are ignored, per RFC 6749; never logged.
        if ('code' in params) == ('error' in params):
            raise ValueError
        if 'error' in params:
            return None, 'denied' if params['error'] == 'access_denied' else 'provider'
        code = params['code']
        if not 1 <= len(code) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in code):
            raise ValueError
        return code, None
    except Exception:
        pass
    raise DesktopSignInError('callback')


class LoopbackCallbackServer:
    """One-shot, prebound IPv4 loopback receiver with bounded HTTP parsing.

    No HTTP access logging, threads, global socket defaults or reflected content.
    Slow/malformed/unrelated requests cannot extend the authorization deadline.
    """

    def __init__(self, *, expected_state):
        self._state = expected_state
        self._consumed = False
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self._socket.bind(('127.0.0.1', 0))
            self._socket.listen(5)
            self._socket.settimeout(_POLL)
            self._host = '127.0.0.1:' + str(self._socket.getsockname()[1])
            self.redirect_uri = 'http://' + self._host + '/'
        except Exception:
            self.close()
            raise DesktopSignInError('callback') from None

    def close(self):
        self._state = ''
        self._consumed = True
        self._socket.close()

    @staticmethod
    def _reply(connection, status):
        body = (b'Sign-in response received. Return to CacheCAD to check the result.'
                if status == 200 else b'This sign-in response was not accepted.')
        response = (
            f'HTTP/1.1 {status} Response\r\nContent-Type: text/plain; charset=utf-8\r\n'
            f'Content-Length: {len(body)}\r\nCache-Control: no-store\r\n'
            "Content-Security-Policy: default-src 'none'; frame-ancestors 'none'\r\n"
            'Referrer-Policy: no-referrer\r\nX-Content-Type-Options: nosniff\r\n'
            'Connection: close\r\n\r\n').encode('ascii') + body
        try:
            connection.settimeout(_POLL)
            connection.sendall(response)
        except OSError:
            pass

    def _read_target(self, connection, cancel_event, deadline):
        request_deadline = min(deadline, time.monotonic() + 1.0)
        data = bytearray()
        while b'\r\n\r\n' not in data:
            remaining = _check_pending(cancel_event, deadline)
            if time.monotonic() >= request_deadline or len(data) >= 8192:
                raise DesktopSignInError('callback')
            connection.settimeout(min(_POLL, remaining))
            try:
                chunk = connection.recv(min(2048, 8192 - len(data)))
            except socket.timeout:
                continue
            if not chunk:
                raise DesktopSignInError('callback')
            data.extend(chunk)
        try:
            header = bytes(data).split(b'\r\n\r\n', 1)[0].decode('ascii')
            lines = header.split('\r\n')
            method, target, version = lines[0].split(' ')
            if method != 'GET' or version not in ('HTTP/1.0', 'HTTP/1.1'):
                raise ValueError
            headers = {}
            for line in lines[1:]:
                name, value = line.split(':', 1)
                name = name.lower()
                if name in headers or not re.fullmatch(r'[a-z0-9-]+', name):
                    raise ValueError
                headers[name] = value.strip()
            if headers.get('host') != self._host:
                raise ValueError
            if headers.get('content-length', '0') != '0' or 'transfer-encoding' in headers:
                raise ValueError
            return target
        except Exception:
            pass
        raise DesktopSignInError('callback')

    def wait_for_code(self, *, cancel_event, deadline):
        if self._consumed:
            raise DesktopSignInError('callback')
        attempts = 0
        while attempts < 64:
            _check_pending(cancel_event, deadline)
            try:
                connection, address = self._socket.accept()
            except socket.timeout:
                continue
            attempts += 1
            result = self._consume(connection, address, cancel_event, deadline)
            if result is not None:
                return result
        raise DesktopSignInError('callback')

    def _consume(self, connection, address, cancel_event, deadline):
        with connection:
            try:
                if address[0] != '127.0.0.1':
                    raise DesktopSignInError('callback')
                target = self._read_target(connection, cancel_event, deadline)
                code, error = _parse_callback(target, self._state)
            except (SignInCancelled, SignInTimeout):
                raise
            except (DesktopSignInError, OSError):
                self._reply(connection, 400)
                return None
            self._consumed = True
            self._state = ''
            self._reply(connection, 200)
            if error:
                raise DesktopSignInError(error)
            return code


@contextmanager
def _quiet_oauth_logs():
    """The official SDK logs token bodies at DEBUG: suppress this worker only."""
    prefixes = ('oauthlib', 'requests_oauthlib', 'google.auth', 'google.oauth2',
                'google_auth_oauthlib', 'urllib3', 'requests')
    names = set(logging.Logger.manager.loggerDict)
    # Include lazy loggers before default dependencies have been imported.
    names.update(('requests_oauthlib.oauth2_session', 'google.auth.transport.requests',
                  'google_auth_oauthlib.flow', 'oauthlib.oauth2.rfc6749.clients.base',
                  'oauthlib.oauth2.rfc6749.clients.web_application',
                  'oauthlib.oauth2.rfc6749.parameters', 'urllib3.connectionpool'))
    thread_id = threading.get_ident()

    class WorkerFilter(logging.Filter):
        def filter(self, record):
            return record.thread != thread_id

    filt = WorkerFilter()
    loggers = [logging.getLogger(name) for name in names
               if any(name == prefix or name.startswith(prefix + '.') for prefix in prefixes)]
    for logger in loggers:
        logger.addFilter(filt)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(filt)


def _default_flow_factory(config, **kwargs):
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise DesktopSignInError('dependencies') from None
    return InstalledAppFlow.from_client_config(config, **kwargs)


def _pin_token_transport(flow, cancel_event, deadline, *, scopes=None):
    """Apply redirect policy at requests' layer, not OAuth token parameters.

    OAuth2Session.fetch_token treats unknown kwargs as form fields. In particular,
    passing allow_redirects there does NOT prevent credential-bearing redirects.
    """
    session = flow.oauth2session
    original_request = session.request
    session.trust_env = False
    session.verify = True

    def token_request(method, url, **kwargs):
        if method.upper() != 'POST' or url != TOKEN_URI:
            raise DesktopSignInError('configuration')
        kwargs['allow_redirects'] = False
        kwargs['timeout'] = min(_NETWORK_TIMEOUT, _check_pending(cancel_event, deadline))
        kwargs['verify'] = True
        response = original_request(method, url, **kwargs)
        _check_pending(cancel_event, deadline)
        if response.status_code != 200:
            raise DesktopSignInError('provider')
        if scopes is not None:
            # Google may return the canonical userinfo.email URI for "email".
            # Normalize only this documented equivalent before OAuthlib's exact
            # scope check. Never relax or add a denied/unrequested permission.
            if len(response.content) > 131072:
                raise DesktopSignInError('provider')
            payload = json.loads(response.content, object_pairs_hook=_unique_object)
            if not isinstance(payload, dict):
                raise DesktopSignInError('provider')
            if 'scope' in payload:
                granted = payload['scope']
                if (not isinstance(granted, str)
                        or canonical_scopes(granted.split()) != canonical_scopes(scopes)):
                    raise DesktopSignInError('provider')
                payload['scope'] = ' '.join(scopes)
                response._content = json.dumps(payload).encode('utf-8')
        return response

    session.request = token_request


def _verify_google_id_token(token, *, audience, timeout):
    from google.auth.transport.requests import Request
    from google.oauth2.id_token import verify_oauth2_token
    import requests

    with requests.Session() as session:
        session.trust_env = False
        request = Request(session=session)

        def certificates_only(url, method='GET', **kwargs):
            if url != CERTS_URI or method != 'GET':
                raise DesktopSignInError('identity')
            # Only Google's public certificate URL is fetched. The ID token
            # is verified locally; it is never sent to a tokeninfo service.
            response = request(url, method=method, timeout=timeout, allow_redirects=False)
            # google-auth's optional JWK/PyJWT branch performs its own HTTP
            # request. This pinned x509 endpoint must never take that branch.
            if response.status != 200 or len(response.data) > 262144:
                raise DesktopSignInError('identity')
            certs = json.loads(response.data)
            if (not isinstance(certs, dict) or not certs or 'keys' in certs
                    or any(not isinstance(value, str) for value in certs.values())):
                raise DesktopSignInError('identity')
            return response

        return verify_oauth2_token(token, certificates_only, audience=audience)


def _identity_from_claims(claims, *, client_id, nonce):
    try:
        if not isinstance(claims, Mapping):
            raise ValueError
        if claims['iss'] not in ('accounts.google.com', 'https://accounts.google.com'):
            raise ValueError
        if claims['aud'] != client_id or claims.get('azp', client_id) != client_id:
            raise ValueError
        now = time.time()
        if (type(claims['exp']) is not int or type(claims['iat']) is not int
                or claims['exp'] <= now or claims['iat'] > now or claims['iat'] > claims['exp']):
            raise ValueError
        returned_nonce = claims['nonce']
        if (not isinstance(returned_nonce, str) or not returned_nonce.isascii()
                or not hmac.compare_digest(returned_nonce, nonce)):
            raise ValueError
        email, sub = claims['email'], claims['sub']
        verified = claims.get('email_verified')
        if verified is not True and verified != 'true':
            raise ValueError
        if not isinstance(sub, str) or not re.fullmatch(r'[\x21-\x7e]{1,255}', sub):
            raise ValueError
        if (not isinstance(email, str) or len(email) > 254
                or not re.fullmatch(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+', email)):
            raise ValueError
        return SignInIdentity(email=email, sub=sub)
    except Exception:
        pass
    raise DesktopSignInError('identity')


def _check_authorization_url(url, *, state, nonce, verifier, redirect_uri, client_id,
                             scopes=SCOPES, access_type='online', prompt='select_account'):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme + '://' + parsed.netloc + parsed.path != AUTH_URI
                or parsed.fragment):
            raise ValueError
        params = _unique_object(parse_qsl(parsed.query, strict_parsing=True))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest())
        expected = {'state': state, 'nonce': nonce, 'client_id': client_id,
                    'redirect_uri': redirect_uri, 'response_type': 'code',
                    'access_type': access_type, 'prompt': prompt,
                    'code_challenge_method': 'S256',
                    'code_challenge': challenge.decode('ascii').rstrip('=')}
        if any(params.get(key) != value for key, value in expected.items()):
            raise ValueError
        if set(params.get('scope', '').split()) != set(scopes):
            raise ValueError
        if 'include_granted_scopes' in params or 'client_secret' in params:
            raise ValueError
    except Exception:
        raise DesktopSignInError('configuration') from None


def _discard_flow(flow):
    """Best-effort reference removal, not a promise of secure Python RAM erasure."""
    if flow is None:
        return
    try:
        flow.oauth2session.token = {}
        client = getattr(flow.oauth2session, '_client', None)
        if client is not None:
            for field in ('access_token', 'refresh_token', 'code', 'code_verifier', 'id_token'):
                if hasattr(client, field):
                    setattr(client, field, None)
        flow.code_verifier = None
        flow.client_config.clear()
        flow.oauth2session.close()
    except Exception:
        pass


class DesktopSignIn:
    """Run a single identity-only sign-in at a time, with injectable effects.

    flow_factory matches InstalledAppFlow.from_client_config. verifier(token,
    audience=..., timeout=...) must cryptographically verify the ID token and
    return claims. callbackserver_factory(expected_state=...) must prebind its
    listener and expose redirect_uri, wait_for_code(cancel_event=..., deadline=...),
    and close(). Only trusted test code should replace these security effects.
    """

    def __init__(self, client_config_path, *, browser_open=None, flow_factory=None,
                 verifier=None, callbackserver_factory=None):
        self._config_source = client_config_path
        self._browser_open = browser_open or webbrowser.open
        self._flow_factory = flow_factory or _default_flow_factory
        self._verifier = verifier or _verify_google_id_token
        self._server_factory = callbackserver_factory or LoopbackCallbackServer
        self._running = threading.Lock()
        self._scopes = SCOPES
        self._access_type = 'online'
        self._prompt = 'select_account'

    def _load_config(self):
        return (validate_client_config(self._config_source)
                if isinstance(self._config_source, Mapping)
                else load_client_config(self._config_source))

    def _result(self, identity, token_response, config):
        return identity

    def run(self, cancel_event=None, timeout=120):
        """Return email/sub only; raise fixed safe errors without provider context."""
        if not self._running.acquire(blocking=False):
            raise DesktopSignInError('busy')
        error_code = 'failed'
        try:
            with _quiet_oauth_logs():
                return self._run(cancel_event or threading.Event(), timeout)
        except DesktopSignInError as error:
            error_code = error.code
        except Exception:
            pass
        finally:
            self._running.release()
        # Raise outside the except block, so provider exceptions cannot leak
        # through an exception cause/context inspected by GUI/reporting code.
        if error_code == 'cancelled':
            raise SignInCancelled()
        if error_code == 'timeout':
            raise SignInTimeout()
        raise DesktopSignInError(error_code)

    def _run(self, cancel_event, timeout):
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or not 0 < timeout <= 600):
            raise DesktopSignInError('configuration')
        deadline = time.monotonic() + timeout
        _check_pending(cancel_event, deadline)
        config = self._load_config()
        client_id = config['installed']['client_id']
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        server = flow = None
        token_response = None
        try:
            server = self._server_factory(expected_state=state)
            redirect = urlsplit(server.redirect_uri)
            if (redirect.scheme != 'http' or redirect.hostname != '127.0.0.1'
                    or not redirect.port or redirect.path != '/' or redirect.query
                    or redirect.fragment or redirect.username or redirect.password):
                raise DesktopSignInError('callback')
            flow = self._flow_factory(config, scopes=list(self._scopes), state=state,
                                      redirect_uri=server.redirect_uri, code_verifier=verifier,
                                      autogenerate_code_verifier=False)
            _pin_token_transport(flow, cancel_event, deadline,
                                 scopes=self._scopes if self._access_type == 'offline' else None)
            url, returned_state = flow.authorization_url(
                state=state, nonce=nonce, access_type=self._access_type, prompt=self._prompt)
            if returned_state != state:
                raise DesktopSignInError('callback')
            _check_authorization_url(url, state=state, nonce=nonce, verifier=verifier,
                                     redirect_uri=server.redirect_uri, client_id=client_id,
                                     scopes=self._scopes, access_type=self._access_type,
                                     prompt=self._prompt)
            _check_pending(cancel_event, deadline)
            if self._browser_open(url) is False:
                raise DesktopSignInError('browser')
            code = server.wait_for_code(cancel_event=cancel_event, deadline=deadline)
            server.close()
            remaining = _check_pending(cancel_event, deadline)
            # State has been checked by our receiver. Never disable HTTPS checks
            # globally to pass the HTTP loopback callback to OAuthlib.
            token_response = flow.fetch_token(code=code, timeout=min(_NETWORK_TIMEOUT, remaining))
            code = None
            remaining = _check_pending(cancel_event, deadline)
            token = token_response.get('id_token') if isinstance(token_response, Mapping) else None
            if not isinstance(token, str) or not token or len(token) > 32768:
                raise DesktopSignInError('identity')
            claims = self._verifier(token, audience=client_id,
                                    timeout=min(_NETWORK_TIMEOUT, remaining))
            _check_pending(cancel_event, deadline)
            identity = _identity_from_claims(claims, client_id=client_id, nonce=nonce)
            return self._result(identity, token_response, config)
        finally:
            if server is not None:
                try:
                    server.close()
                except Exception:
                    pass
            if isinstance(token_response, dict):
                token_response.clear()
            _discard_flow(flow)
            config.clear()


class DesktopAuthorization(DesktopSignIn):
    """Production authorization using an app-owned, validated Desktop client.

    No end-user client picker, bundled credential, token store or login on import.
    Offline access is necessary for remembered sign-in. Explicit consent requests
    a refresh token even when the same account authorized the application before.
    """

    def __init__(self, client_config, *, scopes=DEFAULT_DRIVE_SCOPES, **effects):
        config = (validate_client_config(client_config) if isinstance(client_config, Mapping)
                  else load_client_config(client_config))
        super().__init__(config, **effects)
        self._scopes = validate_scopes(scopes)
        self._access_type = 'offline'
        self._prompt = 'consent select_account'

    def _result(self, identity, token_response, config):
        try:
            from google.oauth2.credentials import Credentials
        except ImportError:
            raise DesktopSignInError('dependencies') from None
        try:
            access = token_response['access_token']
            refresh = token_response['refresh_token']
            for token in (access, refresh):
                if (not isinstance(token, str) or not 1 <= len(token) <= 32768
                        or any(ord(char) < 33 or ord(char) > 126 for char in token)):
                    raise ValueError
            if token_response.get('token_type', '').lower() != 'bearer':
                raise ValueError
            granted = token_response.get('scope', self._scopes)
            if isinstance(granted, str):
                granted = granted.split()
            if canonical_scopes(granted) != canonical_scopes(self._scopes):
                raise ValueError
            expires_at = token_response.get('expires_at')
            if expires_at is None:
                duration = token_response['expires_in']
                if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                    raise ValueError
                expires_at = time.time() + duration
            if (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
                    or not math.isfinite(expires_at) or expires_at <= time.time()
                    or expires_at > time.time() + 2678400):
                raise ValueError
            expiry = datetime.fromtimestamp(expires_at, timezone.utc).replace(tzinfo=None)
            installed = config['installed']
            credentials = Credentials(
                token=access, refresh_token=refresh, token_uri=TOKEN_URI,
                client_id=installed['client_id'], client_secret=installed['client_secret'],
                scopes=self._scopes, expiry=expiry)
            return AuthorizationResult(identity, credentials)
        except Exception:
            pass
        raise DesktopSignInError('provider')
