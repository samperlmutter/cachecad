"""Production session contract, synthetic tokens/keys and mocked HTTP only."""
import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

if not (Path(__file__).resolve().parents[1] / 'maintained/core/google_session.py').is_file():
    raise unittest.SkipTest('Maintained production session is absent from wheel fixture')

from maintained.core import desktop_signin as auth
from maintained.core import google_session as session
from maintained.utils.windows_token_store import TokenStoreError
from tests.recovered_harness import deny_external_effects
from tests.test_desktop_signin import CONFIG, CLIENT_ID, FakeFlow, FakeServer, claims

try:
    from google.oauth2.credentials import Credentials
    import google.auth.crypt
    import google.auth.jwt
    import google_auth_oauthlib.flow
    import requests
    import rsa
except ImportError:
    HAS_RUNTIME = False
else:
    HAS_RUNTIME = True


class MemoryStore:
    def __init__(self):
        self.record = None
        self.saves = 0
        self.clears = 0
        self.on_save = None

    def load(self):
        return copy.deepcopy(self.record)

    def save(self, value):
        self.record = copy.deepcopy(value)
        self.saves += 1
        if self.on_save:
            self.on_save()

    def clear(self):
        self.record = None
        self.clears += 1


@unittest.skipUnless(HAS_RUNTIME, 'Requires pinned Google runtime')
class ProductionAuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.public_key, cls.private_key = rsa.newkeys(1024)

    def setUp(self):
        self.guard = deny_external_effects()
        self.guard.__enter__()
        self.addCleanup(self.guard.__exit__, None, None, None)
        self.urls = []
        self.requests = []
        self.scope = ' '.join(auth.DEFAULT_DRIVE_SCOPES)
        self.token_status = 200
        self.updates = {}

    def browser(self, url):
        self.urls.append(url)
        self.params = parse_qs(urlsplit(url).query)
        return True

    def send(self, request, **kwargs):
        self.requests.append((request, kwargs))
        response = requests.Response()
        response.request, response.url = request, request.url
        response.headers['Content-Type'] = 'application/json'
        response.headers['Location'] = 'https://example.invalid/never-follow'
        if request.url == auth.TOKEN_URI:
            response.status_code = self.token_status
            signer = google.auth.crypt.RSASigner.from_string(
                self.private_key.save_pkcs1(), key_id='synthetic-key')
            token = google.auth.jwt.encode(signer, claims(self.params['nonce'][0],
                                                         **self.updates)).decode()
            payload = {'access_token': 'SYNTHETIC-ACCESS', 'refresh_token': 'SYNTHETIC-REFRESH',
                       'id_token': token, 'expires_in': 3600, 'token_type': 'Bearer',
                       'scope': self.scope}
        elif request.url == auth.CERTS_URI:
            response.status_code = 200
            payload = {'synthetic-key': self.public_key.save_pkcs1().decode()}
        else:
            self.fail('Unexpected network target')
        response._content = json.dumps(payload).encode()
        return response

    def run_auth(self, **kwargs):
        with patch.object(requests.Session, 'send', lambda _, request, **kw: self.send(request, **kw)):
            return auth.DesktopAuthorization(
                CONFIG, browser_open=self.browser, callbackserver_factory=FakeServer,
                **kwargs).run(timeout=5)

    def test_real_sdk_signed_identity_credentials_offline_full_drive(self):
        result = self.run_auth()
        self.assertIsInstance(result, auth.AuthorizationResult)
        self.assertIsInstance(result.credentials, Credentials)
        self.assertTrue(result.credentials.valid)
        self.assertEqual(result.credentials.refresh_token, 'SYNTHETIC-REFRESH')
        self.assertEqual(result.identity.email, 'synthetic@example.invalid')
        self.assertNotIn('SYNTHETIC-REFRESH', repr(result))
        self.assertEqual(self.params['access_type'], ['offline'])
        self.assertEqual(self.params['prompt'], ['consent select_account'])
        self.assertEqual(set(self.params['scope'][0].split()), set(auth.DEFAULT_DRIVE_SCOPES))
        self.assertNotIn('include_granted_scopes', self.params)
        for _, kwargs in self.requests:
            self.assertFalse(kwargs['allow_redirects'])
            self.assertLessEqual(kwargs['timeout'], 5)

    def test_canonical_email_alias_accepted_without_extra_scopes(self):
        self.scope = 'openid https://www.googleapis.com/auth/userinfo.email ' + \
                     'https://www.googleapis.com/auth/drive'
        self.assertIsInstance(self.run_auth().credentials, Credentials)

    def test_partial_extra_scope_or_unverified_identity_never_returns_credentials(self):
        for scopes in ('openid email', 'openid email https://www.googleapis.com/auth/drive ' +
                       'https://www.googleapis.com/auth/calendar'):
            self.scope = scopes
            with self.assertRaises(auth.DesktopSignInError):
                self.run_auth()
        self.scope = ' '.join(auth.DEFAULT_DRIVE_SCOPES)
        self.updates = {'nonce': 'wrong'}
        with self.assertRaises(auth.DesktopSignInError):
            self.run_auth()

    def test_redirect_does_not_transmit_authorization_code_twice(self):
        self.token_status = 307
        with self.assertRaises(auth.DesktopSignInError):
            self.run_auth()
        self.assertEqual([request.url for request, _ in self.requests], [auth.TOKEN_URI])

    def test_config_loaded_once_and_no_user_config_reread(self):
        signer = auth.DesktopAuthorization(CONFIG, browser_open=lambda _: False,
                                          callbackserver_factory=FakeServer)
        with patch.object(auth, 'load_client_config', side_effect=AssertionError('No re-read')):
            with self.assertRaises(auth.DesktopSignInError) as error:
                signer.run()
        self.assertEqual(error.exception.code, 'browser')


@unittest.skipUnless(HAS_RUNTIME, 'Requires pinned Google runtime')
class GoogleSessionTests(unittest.TestCase):
    def setUp(self):
        self.guard = deny_external_effects()
        self.guard.__enter__()
        self.addCleanup(self.guard.__exit__, None, None, None)
        self.store = MemoryStore()
        self.notifications = []
        self.session = session.GoogleSession(CONFIG, store=self.store,
                                             on_invalidation=self.notifications.append)
        self.identity = auth.SignInIdentity('synthetic@example.invalid', 'synthetic-subject')

    def source(self):
        return Credentials(token='SYNTHETIC-ACCESS', refresh_token='SYNTHETIC-REFRESH',
                           token_uri=auth.TOKEN_URI, client_id=CLIENT_ID,
                           client_secret='SYNTHETIC-CLIENT', scopes=session.DEFAULT_SCOPES,
                           expiry=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1))

    def authorize(self, event=None):
        result = auth.AuthorizationResult(self.identity, self.source())
        with patch.object(session, 'DesktopAuthorization', return_value=SimpleNamespace(
                run=lambda **kwargs: result)):
            return self.session.authorize(event)

    def refresh_payload(self, **changes):
        value = {'access_token': 'SYNTHETIC-NEW-ACCESS', 'refresh_token': 'SYNTHETIC-ROTATED',
                 'expires_in': 3600, 'token_type': 'Bearer',
                 'scope': ' '.join(session.DEFAULT_SCOPES)}
        value.update(changes)
        return value

    def test_authorize_save_restore_and_official_client_contract(self):
        result = self.authorize()
        self.assertIsInstance(result.credentials, Credentials)
        self.assertIsInstance(result.credentials, session.ManagedCredentials)
        self.assertEqual(self.store.record['client_id'], CLIENT_ID)
        self.assertNotIn('SYNTHETIC', repr(result.credentials))
        self.assertNotIn('SYNTHETIC', repr(self.session))
        other = session.GoogleSession(CONFIG, store=self.store)
        with patch.object(session, '_post_form', side_effect=AssertionError('No startup network')):
            restored = other.restore()
        self.assertEqual(restored.identity, self.identity)
        headers = {}
        restored.credentials.before_request(None, 'GET', 'https://www.googleapis.com/drive/v3/files',
                                             headers)
        self.assertEqual(headers['authorization'], 'Bearer SYNTHETIC-ACCESS')

    def test_actual_drive_service_accepts_managed_credentials_without_network(self):
        from googleapiclient.discovery import build
        credentials = self.authorize().credentials
        service = build('drive', 'v3', credentials=credentials,
                        cache_discovery=False, static_discovery=True)
        self.assertIs(service._http.credentials, credentials)
        self.assertTrue(callable(service.files().list))

    def test_expired_cache_restores_without_network_and_refreshes_lazily(self):
        self.authorize()
        self.store.record['expiry'] = time.time() - 3600
        owner = session.GoogleSession(CONFIG, store=self.store)
        with patch.object(session, '_post_form', side_effect=AssertionError('No startup network')):
            result = owner.restore()
        self.assertFalse(result.credentials.valid)
        with patch.object(session, '_post_form', return_value=(200, self.refresh_payload())) as post:
            result.credentials.before_request(None, 'GET', 'unused', {})
        self.assertEqual(post.call_count, 1)
        self.assertTrue(result.credentials.valid)

    def test_explicit_fresh_login_can_replace_unreadable_old_cache(self):
        with patch.object(self.store, 'load', side_effect=TokenStoreError()):
            self.assertTrue(self.authorize().credentials.valid)

    def test_schema_client_scopes_or_identity_tamper_rejected(self):
        self.authorize()
        original = copy.deepcopy(self.store.record)
        changes = ({'schema': 2}, {'client_id': 'other.apps.googleusercontent.com'},
                   {'scopes': ['openid', 'email']}, {'expiry': float('nan')},
                   {'identity': {'email': 'bad\n@example.invalid', 'sub': 'x'}},
                   {'refresh_token': ''}, {'unrecognized': 'x'})
        for change in changes:
            self.store.record = dict(original, **change)
            candidate = session.GoogleSession(CONFIG, store=self.store)
            with self.subTest(change=change), self.assertRaises(session.SessionError) as error:
                candidate.restore()
            self.assertEqual(error.exception.code, 'storage')
            self.assertIsNone(candidate.credentials)

    def test_refresh_persists_rotated_token_before_exposing_it(self):
        credentials = self.authorize().credentials
        credentials.expiry = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        observed = []
        self.store.on_save = lambda: observed.append(credentials.token)
        with patch.object(session, '_post_form', return_value=(200, self.refresh_payload())) as post:
            headers = {}
            credentials.before_request(None, 'GET', 'unused', headers)
        self.assertEqual(observed, ['SYNTHETIC-ACCESS'])
        self.assertEqual(self.store.record['refresh_token'], 'SYNTHETIC-ROTATED')
        self.assertEqual(headers['authorization'], 'Bearer SYNTHETIC-NEW-ACCESS')
        self.assertEqual(post.call_count, 1)

    def test_concurrent_api_refresh_is_single_flight(self):
        credentials = self.authorize().credentials
        credentials.expiry = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        def post(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return 200, self.refresh_payload()
        def request():
            try:
                headers = {}
                credentials.before_request(None, 'GET', 'unused', headers)
                results.append(headers)
            except Exception as error:
                errors.append(error)
        with patch.object(session, '_post_form', side_effect=post) as transport:
            workers = [threading.Thread(target=request) for _ in range(6)]
            for worker in workers:
                worker.start()
            self.assertTrue(entered.wait(2))
            release.set()
            for worker in workers:
                worker.join(2)
            self.assertEqual(transport.call_count, 1)
        self.assertFalse(errors)
        self.assertEqual(len(results), 6)

    def test_invalid_grant_clears_session_notifies_and_does_not_loop(self):
        credentials = self.authorize().credentials
        with patch.object(session, '_post_form', return_value=(400, {
                'error': 'invalid_grant', 'error_description': 'SYNTHETIC-TOKEN-SECRET'})) as post:
            with self.assertRaises(session.SessionError) as error:
                credentials.refresh(None)
            self.assertEqual(error.exception.code, 'reauth_required')
            self.assertIsNone(error.exception.__context__)
            with self.assertRaises(session.SessionError):
                credentials.refresh(None)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.notifications, ['reauth_required'])
        self.assertIsNone(self.store.record)
        self.assertIsNone(self.session.credentials)
        self.assertNotIn('SECRET', str(error.exception))

    def test_late_401_workers_reuse_just_refreshed_token(self):
        credentials = self.authorize().credentials
        with patch.object(session, '_post_form', return_value=(200, self.refresh_payload())) as post:
            for _ in range(6):
                credentials.refresh(None)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(credentials.valid)

    def test_failed_refresh_save_never_exposes_new_access(self):
        credentials = self.authorize().credentials
        with patch.object(session, '_post_form', return_value=(200, self.refresh_payload())), \
                patch.object(self.store, 'save', side_effect=TokenStoreError()):
            with self.assertRaises(session.SessionError) as error:
                credentials.refresh(None)
        self.assertEqual(error.exception.code, 'storage')
        self.assertIsNone(credentials.token)
        self.assertFalse(credentials.valid)
        self.assertEqual(self.notifications, ['storage'])

    def test_network_errors_are_safe_and_concurrent_retries_cool_down(self):
        credentials = self.authorize().credentials
        with patch.object(session, '_post_form', side_effect=ValueError('SYNTHETIC-SECRET')) as post:
            for _ in range(3):
                with self.assertRaises(session.SessionError) as error:
                    credentials.refresh(None)
                self.assertNotIn('SECRET', str(error.exception))
                self.assertIsNone(error.exception.__context__)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(credentials.valid)

    def test_cancel_during_save_rolls_back_before_return(self):
        old = self.authorize()
        previous = copy.deepcopy(self.store.record)
        event = threading.Event()
        self.store.on_save = event.set
        with self.assertRaises(session.SessionError) as error:
            self.authorize(event)
        self.assertEqual(error.exception.code, 'cancelled')
        self.assertEqual(self.store.record, previous)
        self.assertIs(self.session.credentials, old.credentials)

    def test_cancel_during_first_save_leaves_no_cache(self):
        event = threading.Event()
        self.store.on_save = event.set
        with self.assertRaises(session.SessionError):
            self.authorize(event)
        self.assertIsNone(self.store.record)
        self.assertIsNone(self.session.credentials)

    def test_sign_out_is_local_reusable_and_invalidates_retained_credentials(self):
        credentials = self.authorize().credentials
        with patch.object(session, '_post_form', side_effect=AssertionError('No revoke')):
            self.session.sign_out()
        self.assertIsNone(self.store.record)
        with self.assertRaises(session.SessionError):
            credentials.apply({})
        self.assertTrue(self.authorize().credentials.valid)

    def test_disconnect_uses_only_explicit_revoke_then_clears(self):
        credentials = self.authorize().credentials
        with patch.object(session, '_post_form', return_value=(200, {})) as post:
            self.session.disconnect()
        self.assertEqual(post.call_args.args[0], session.REVOKE_URI)
        self.assertEqual(post.call_args.args[1], {'token': 'SYNTHETIC-REFRESH'})
        self.assertFalse(credentials.valid)
        self.assertIsNone(self.store.record)

    def test_failed_disconnect_preserves_cache(self):
        self.authorize()
        before = copy.deepcopy(self.store.record)
        with patch.object(session, '_post_form', return_value=(503, {})):
            with self.assertRaises(session.SessionError):
                self.session.disconnect()
        self.assertEqual(self.store.record, before)
        self.assertIsNotNone(self.session.credentials)

    def test_close_nonblocking_during_refresh_preserves_cache_and_prevents_use(self):
        credentials = self.authorize().credentials
        entered, release = threading.Event(), threading.Event()
        errors = []
        def post(*args):
            entered.set()
            release.wait(2)
            return 200, self.refresh_payload()
        def refresh():
            try:
                credentials.refresh(None)
            except session.SessionError as error:
                errors.append(error)
        with patch.object(session, '_post_form', side_effect=post):
            worker = threading.Thread(target=refresh)
            worker.start()
            self.assertTrue(entered.wait(2))
            start = time.monotonic()
            self.session.close()
            self.assertLess(time.monotonic() - start, 0.25)
            release.set()
            worker.join(2)
        self.assertTrue(errors)
        self.assertIsNotNone(self.store.record)
        self.assertIsNone(self.session.credentials)
        self.assertFalse(credentials.valid)
        with self.assertRaises(session.SessionError):
            credentials.apply({})

    def test_clear_failure_still_invalidates_and_allows_retry(self):
        credentials = self.authorize().credentials
        with patch.object(self.store, 'clear', side_effect=TokenStoreError()):
            with self.assertRaises(session.SessionError) as error:
                self.session.sign_out()
        self.assertEqual(error.exception.code, 'storage')
        self.assertFalse(credentials.valid)
        self.session.sign_out()
        self.assertIsNone(self.store.record)

    def test_close_during_restore_cannot_return_a_live_session(self):
        self.authorize()
        fresh = session.GoogleSession(CONFIG, store=self.store)
        original = self.store.load
        def load():
            fresh.close()
            return original()
        with patch.object(self.store, 'load', side_effect=load):
            with self.assertRaises(session.SessionError) as error:
                fresh.restore()
        self.assertEqual(error.exception.code, 'signed_out')
        self.assertIsNone(fresh.credentials)


@unittest.skipUnless(HAS_RUNTIME, 'Requires pinned Google runtime')
class BoundedTransportTests(unittest.TestCase):
    def setUp(self):
        self.guard = deny_external_effects()
        self.guard.__enter__()
        self.addCleanup(self.guard.__exit__, None, None, None)

    def test_fixed_endpoint_no_redirect_proxy_or_unbounded_body(self):
        seen = []
        def send(adapter, request, **kwargs):
            seen.append((request, kwargs))
            response = requests.Response()
            response.request, response.url, response.status_code = request, request.url, 307
            response.headers['Location'] = 'https://example.invalid/never-follow'
            response.raw = io.BytesIO(b'{}')
            return response
        with patch('requests.adapters.HTTPAdapter.send', send):
            status, body = session._post_form(auth.TOKEN_URI, {'refresh_token': 'SYNTHETIC'},
                                              threading.Event())
        self.assertEqual(status, 307)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1]['timeout'], (5.0, 5.0))
        self.assertFalse(seen[0][1]['proxies'])
        self.assertTrue(seen[0][1]['verify'])
        with self.assertRaises(session.SessionError):
            session._post_form('https://example.invalid/token', {}, threading.Event())

    def test_pre_cancel_never_starts_network(self):
        event = threading.Event()
        event.set()
        with patch.object(requests.Session, 'post', side_effect=AssertionError('No network')):
            with self.assertRaises(session.SessionError):
                session._post_form(auth.TOKEN_URI, {}, event)


if __name__ == '__main__':
    unittest.main()
