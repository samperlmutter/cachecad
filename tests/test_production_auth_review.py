"""Independent production-session security regressions, with no live accounts.

The session tests use actual pinned google-auth Credentials and requests, with
synthetic cache/transport boundaries. No doubles are part of the app payload.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'maintained/core/google_session.py').is_file():
    raise unittest.SkipTest('Maintained production session is absent from wheel fixture')

from maintained.core import google_session as session
from maintained.utils import windows_token_store as storage
from tests.recovered_harness import deny_external_effects, load_function

CONFIG = {'installed': {
    'client_id': 'independent-review.apps.googleusercontent.com',
    'client_secret': 'SYNTHETIC-PUBLIC-CLIENT-METADATA',
    'auth_uri': 'https://accounts.google.com/o/oauth2/v2/auth',
    'token_uri': 'https://oauth2.googleapis.com/token',
    'redirect_uris': ['http://localhost']}}


class MemoryStore:
    def __init__(self):
        self.record = None
        self.writes = []
        self.on_save = lambda: None

    def load(self):
        return deepcopy(self.record)

    def save(self, record):
        self.record = deepcopy(record)
        self.writes.append(deepcopy(record))
        self.on_save()

    def clear(self):
        self.record = None


@unittest.skipIf(session._Credentials is None, 'Pinned Google runtime is not installed')
class ProductionSessionReviewTests(unittest.TestCase):
    def setUp(self):
        guard = deny_external_effects()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        self.store = MemoryStore()
        self.owner = session.GoogleSession(deepcopy(CONFIG), store=self.store)
        self.identity = session.SignInIdentity('synthetic@example.invalid', 'synthetic-subject')
        self.source = session._Credentials(
            token='SYNTHETIC-ACCESS', refresh_token='SYNTHETIC-REFRESH',
            expiry=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10))
        self.authorized = session.AuthorizationResult(self.identity, self.source)

    def authorize(self, event=None):
        with patch.object(session.DesktopAuthorization, 'run', return_value=self.authorized):
            return self.owner.authorize(cancel_event=event)

    def success_payload(self, **updates):
        value = {'access_token': 'SYNTHETIC-ROTATED-ACCESS',
                 'refresh_token': 'SYNTHETIC-ROTATED-REFRESH',
                 'expires_in': 600, 'token_type': 'Bearer',
                 'scope': ' '.join(session.DEFAULT_SCOPES)}
        value.update(updates)
        return value

    def test_restore_rejects_cross_client_scope_schema_and_identity(self):
        self.authorize()
        baseline = deepcopy(self.store.record)
        cases = [dict(baseline, client_id='other.apps.googleusercontent.com'),
                 dict(baseline, scopes=['openid', 'email']),
                 dict(baseline, schema=True),
                 dict(baseline, identity={'email': 'not-an-email', 'sub': 'subject'}),
                 dict(baseline, expiry=float('inf')),
                 dict(baseline, refresh_token='SYNTHETIC\nINJECTION')]
        for record in cases:
            with self.subTest(record=record):
                self.store.record = record
                fresh = session.GoogleSession(CONFIG, store=self.store)
                with self.assertRaises(session.SessionError) as raised:
                    fresh.restore()
                self.assertEqual(raised.exception.code, 'storage')
                self.assertIsNone(fresh.credentials)

    def test_restoration_has_no_network_or_browser_and_retains_verified_identity(self):
        self.authorize()
        fresh = session.GoogleSession(CONFIG, store=self.store)
        with patch.object(session, '_post_form', side_effect=AssertionError('No network')), \
                patch.object(session.DesktopAuthorization, 'run', side_effect=AssertionError('No login')):
            restored = fresh.restore()
        self.assertEqual(restored.identity, self.identity)
        self.assertEqual(restored.credentials.refresh_token, 'SYNTHETIC-REFRESH')
        self.assertNotIn('SYNTHETIC-ACCESS', repr(restored))
        self.assertNotIn('SYNTHETIC-REFRESH', repr(restored.credentials))

    def test_local_signout_invalidates_retained_credentials_without_revocation(self):
        retained = self.authorize().credentials
        with patch.object(session, '_post_form', side_effect=AssertionError('No revocation')):
            self.owner.sign_out()
        self.assertIsNone(self.store.record)
        self.assertIsNone(retained.token)
        self.assertIsNone(retained.refresh_token)
        self.assertIsNone(self.owner.identity)
        for operation in (lambda: retained.apply({}),
                          lambda: retained.before_request(None, 'GET', 'https://www.googleapis.com/', {}),
                          lambda: retained.refresh(None)):
            with self.assertRaises(session.SessionError) as raised:
                operation()
            self.assertEqual(raised.exception.code, 'signed_out')

    def test_failed_revoke_preserves_cache_and_success_revokes_only_refresh_token(self):
        retained = self.authorize().credentials
        original = deepcopy(self.store.record)
        with patch.object(session, '_post_form', return_value=(503, {})) as post:
            with self.assertRaises(session.SessionError):
                self.owner.disconnect()
        self.assertEqual(self.store.record, original)
        self.assertIs(self.owner.credentials, retained)
        self.assertEqual(post.call_args.args[:2],
                         (session.REVOKE_URI, {'token': 'SYNTHETIC-REFRESH'}))
        with patch.object(session, '_post_form', return_value=(200, {})):
            self.owner.disconnect()
        self.assertIsNone(self.store.record)
        self.assertFalse(retained.valid)

    def test_invalid_grant_erases_cache_and_blocks_retained_credentials(self):
        retained = self.authorize().credentials
        with patch.object(session, '_post_form', return_value=(400, {'error': 'invalid_grant'})):
            with self.assertRaises(session.SessionError) as raised:
                retained.refresh(None)
        self.assertEqual(raised.exception.code, 'reauth_required')
        self.assertIsNone(self.owner.credentials)
        self.assertIsNone(self.store.record)
        with self.assertRaises(session.SessionError):
            retained.apply({})

    def test_refresh_rotation_is_persisted_before_exposure(self):
        retained = self.authorize().credentials
        seen = []
        self.store.on_save = lambda: seen.append(retained.token)
        with patch.object(session, '_post_form', return_value=(200, self.success_payload())) as post:
            retained.refresh(lambda *a, **kw: self.fail('Untrusted transport called'))
        self.assertEqual(seen, ['SYNTHETIC-ACCESS'])
        self.assertEqual(self.store.record['refresh_token'], 'SYNTHETIC-ROTATED-REFRESH')
        self.assertEqual(retained.token, 'SYNTHETIC-ROTATED-ACCESS')
        self.assertEqual(post.call_args.args[0], session.TOKEN_URI)
        self.assertEqual(post.call_args.args[1]['client_id'], CONFIG['installed']['client_id'])
        self.assertEqual(post.call_args.args[1]['grant_type'], 'refresh_token')

    def test_failed_rotation_storage_invalidates_in_memory_session(self):
        retained = self.authorize().credentials
        with patch.object(session, '_post_form', return_value=(200, self.success_payload())), \
                patch.object(self.store, 'save', side_effect=storage.TokenStoreError()):
            with self.assertRaises(session.SessionError) as raised:
                retained.refresh(None)
        self.assertEqual(raised.exception.code, 'storage')
        self.assertIsNone(retained.token)
        self.assertIsNone(self.owner.credentials)

    def test_scope_inflation_or_malformed_refresh_is_never_persisted(self):
        retained = self.authorize().credentials
        for updates in ({'scope': 'openid email https://www.googleapis.com/auth/drive.readonly'},
                        {'scope': ' '.join(session.DEFAULT_SCOPES) + ' unexpected'},
                        {'expires_in': True}, {'expires_in': float('inf')},
                        {'token_type': 'mac'}, {'access_token': 'bad\nheader'}):
            with self.subTest(updates=updates):
                retained._failure_until = 0
                with patch.object(session, '_post_form', return_value=(200, self.success_payload(**updates))):
                    with self.assertRaises(session.SessionError):
                        retained.refresh(None)
                self.assertEqual(len(self.store.writes), 1)
                self.assertEqual(retained.token, 'SYNTHETIC-ACCESS')

    def test_cancel_during_authorization_save_rolls_back_fresh_cache(self):
        event = threading.Event()
        self.store.on_save = event.set
        with self.assertRaises(session.SessionError) as raised:
            self.authorize(event)
        self.assertEqual(raised.exception.code, 'cancelled')
        self.assertIsNone(self.owner.credentials)
        self.assertIsNone(self.store.record)

    def test_cancel_during_replacement_save_preserves_previous_account(self):
        retained = self.authorize().credentials
        original = deepcopy(self.store.record)
        self.authorized = session.AuthorizationResult(
            session.SignInIdentity('other@example.invalid', 'other-subject'), self.source)
        event = threading.Event()
        self.store.on_save = event.set
        with self.assertRaises(session.SessionError):
            self.authorize(event)
        self.assertEqual(self.store.record, original)
        self.assertIs(self.owner.credentials, retained)
        self.assertEqual(self.owner.identity, self.identity)

    def test_safe_network_error_has_no_provider_exception_context(self):
        retained = self.authorize().credentials
        with patch.object(session, '_post_form', side_effect=RuntimeError('SYNTHETIC-TOKEN-DO-NOT-LEAK')):
            with self.assertRaises(session.SessionError) as raised:
                retained.refresh(None)
        self.assertNotIn('SYNTHETIC', str(raised.exception))
        self.assertIsNone(raised.exception.__context__)
        self.assertIsNone(raised.exception.__cause__)

    def test_close_during_refresh_save_cannot_restore_retained_token_values(self):
        retained = self.authorize().credentials
        self.store.on_save = self.owner.close
        with patch.object(session, '_post_form', return_value=(200, self.success_payload())):
            with self.assertRaises(session.SessionError):
                retained.refresh(None)
        self.assertIsNone(self.owner.credentials)
        self.assertIsNone(retained.token)
        self.assertIsNone(retained.refresh_token)
        with self.assertRaises(session.SessionError):
            retained.apply({})

    def test_production_flow_uses_real_sdk_and_offline_refresh_credentials(self):
        # Reuse the established cryptographic fixture, but execute the production
        # subclass using actual OAuthlib and actual RSA ID-token verification.
        from tests import test_desktop_signin as legacy
        import json
        from urllib.parse import parse_qs, urlsplit

        class ProductionFixture(legacy.OfficialRuntimeTests):
            def make_signer(fixture):
                def factory(config, **kwargs):
                    fixture.flow = legacy.google_auth_oauthlib.flow.InstalledAppFlow.from_client_config(
                        config, **kwargs)
                    return fixture.flow

                def browser(url):
                    fixture.params = parse_qs(urlsplit(url).query)
                    return True

                return legacy.auth.DesktopAuthorization(
                    deepcopy(legacy.CONFIG), browser_open=browser,
                    callbackserver_factory=legacy.FakeServer, flow_factory=factory)

            def send(fixture, transport, request, **kwargs):
                response = super().send(transport, request, **kwargs)
                if request.url == legacy.auth.TOKEN_URI:
                    payload = json.loads(response.content)
                    payload['scope'] = ' '.join(legacy.auth.DEFAULT_DRIVE_SCOPES)
                    response._content = json.dumps(payload).encode()
                return response

        ProductionFixture.setUpClass()
        fixture = ProductionFixture('test_real_library_pkce_exchange_and_signed_id_token')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result = fixture.run_signer()
        self.assertIsInstance(result, session.AuthorizationResult)
        self.assertEqual(result.identity.email, 'synthetic@example.invalid')
        self.assertIsInstance(result.credentials, session._Credentials)
        self.assertEqual(result.credentials.refresh_token, 'SYNTHETIC-REFRESH-TOKEN')
        self.assertEqual(fixture.params['access_type'], ['offline'])
        self.assertEqual(fixture.params['prompt'], ['consent select_account'])
        self.assertEqual(set(fixture.params['scope'][0].split()), set(session.DEFAULT_SCOPES))
        self.assertEqual(fixture.flow.oauth2session.token, {})


@unittest.skipIf(session._Credentials is None, 'Pinned Google runtime is not installed')
class ProductionTransportReviewTests(unittest.TestCase):
    def test_real_requests_transport_pins_tls_and_disables_redirects_and_proxy_env(self):
        import requests
        captured = []

        def send(adapter, request, **kwargs):
            captured.append((request, kwargs))
            response = requests.Response()
            response.request, response.url = request, request.url
            response.status_code = 307
            response.headers['Location'] = 'https://untrusted.example.invalid/token'
            response.raw = io.BytesIO(b'{}')
            return response

        with deny_external_effects(), patch.dict('os.environ', {'HTTPS_PROXY': 'http://untrusted.invalid:9'}), \
                patch.object(requests.adapters.HTTPAdapter, 'send', send):
            result = session._post_form(session.TOKEN_URI,
                                        {'refresh_token': 'SYNTHETIC-REFRESH'}, threading.Event())
        self.assertEqual(result, (307, {}))
        self.assertEqual(len(captured), 1)
        request, kwargs = captured[0]
        self.assertEqual(request.url, session.TOKEN_URI)
        self.assertEqual(parse_qs(request.body), {'refresh_token': ['SYNTHETIC-REFRESH']})
        self.assertIs(kwargs['verify'], True)
        self.assertFalse(kwargs['proxies'])
        self.assertEqual(kwargs['timeout'], (5.0, 5.0))

    def test_forbidden_endpoint_or_precancel_has_no_transport(self):
        import requests
        event = threading.Event()
        with patch.object(requests.Session, 'post', side_effect=AssertionError('No transport')):
            for endpoint in ('http://oauth2.googleapis.com/token', session.TOKEN_URI + '?x=1',
                             'https://oauth2.googleapis.com.untrusted.invalid/token'):
                with self.assertRaises(session.SessionError):
                    session._post_form(endpoint, {}, event)
            event.set()
            with self.assertRaises(session.SessionError) as raised:
                session._post_form(session.TOKEN_URI, {}, event)
        self.assertEqual(raised.exception.code, 'cancelled')


class ProtectedStorageReviewTests(unittest.TestCase):
    def test_non_windows_storage_has_no_plaintext_fallback(self):
        with patch.object(storage.os, 'name', 'posix'):
            with self.assertRaises(storage.TokenStoreError) as raised:
                storage.WindowsDPAPI()
        self.assertEqual(raised.exception.code, 'platform')

    def test_encrypt_failure_cannot_create_cache_or_temporary_file(self):
        class RejectingProtector:
            def protect(self, plain):
                raise RuntimeError('SYNTHETIC-SECRET')

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'session.dpapi'
            cache = storage.WindowsTokenStore(target, protector=RejectingProtector())
            with self.assertRaises(storage.TokenStoreError) as raised:
                cache.save({'refresh_token': 'SYNTHETIC-SECRET'})
            self.assertEqual(list(Path(temporary).iterdir()), [])
            self.assertNotIn('SYNTHETIC', str(raised.exception))
            self.assertIsNone(raised.exception.__context__)


class ProductionUiDeliveryReviewTests(unittest.TestCase):
    def test_committed_authorization_wins_cancel_before_queued_gui_delivery(self):
        """Never claim cancellation after the new login was durably committed."""
        event = threading.Event()
        event.set()
        result = SimpleNamespace(credentials=object(), identity=object())
        signals = SimpleNamespace(auth_success=Mock(), auth_failed=Mock(), sign_out=Mock())
        manager = SimpleNamespace(
            _closed=False, _pending_result=('authorize', result, ''),
            _thread=SimpleNamespace(is_alive=lambda: False), _poll=Mock(),
            _cancel_event=event, _session=SimpleNamespace(), _generation=1,
            _invalidated=Mock(), _set_state=Mock(), creds=None, identity=None)
        settle = load_function('maintained/core/auth_manager.py', 'AuthManager._settle',
                               {'signal_manager': signals, 'SAFE_MESSAGES': {'cancelled': 'Cancelled'}})
        with deny_external_effects():
            settle(manager)
        self.assertIs(manager.creds, result.credentials)
        self.assertIs(manager.identity, result.identity)
        self.assertEqual(manager._set_state.call_args.args[0], 'signed_in')
        signals.auth_success.emit.assert_called_once_with(result.credentials)
        signals.auth_failed.emit.assert_not_called()
        signals.sign_out.emit.assert_not_called()

    def test_failed_local_cache_removal_cannot_claim_still_signed_in(self):
        signals = SimpleNamespace(auth_success=Mock(), auth_failed=Mock(), sign_out=Mock())
        manager = SimpleNamespace(
            _closed=False, _pending_result=('sign_out', None, 'storage'),
            _thread=SimpleNamespace(is_alive=lambda: False), _poll=Mock(),
            _cancel_event=threading.Event(), _session=SimpleNamespace(credentials=None),
            _set_state=Mock(), creds=object(), identity=object(), message='storage failed')
        settle = load_function('maintained/core/auth_manager.py', 'AuthManager._settle',
                               {'signal_manager': signals, 'SAFE_MESSAGES': {'storage': 'Storage failed'}})
        with deny_external_effects():
            settle(manager)
        self.assertIsNone(manager.creds)
        self.assertIsNone(manager.identity)
        self.assertTrue(manager._clear_pending)
        self.assertEqual(manager._set_state.call_args.args[0], 'cleanup_required')
        signals.sign_out.emit.assert_called_once_with()

    def test_transfer_cancel_keeps_account_switch_blocked_until_executor_settles(self):
        signals = SimpleNamespace(sync_cancelled=Mock(), page_change=Mock())
        controller = SimpleNamespace(sync_executing=True, main_window=Mock(),
                                     auth_manager=Mock(), sync_analyzer=None)
        cancel = load_function('maintained/app/app_controller.py', 'AppController.on_back_button_clicked',
                              {'signal_manager': signals, 'logging': Mock(),
                               'Page': SimpleNamespace(PROJECT='project')})
        signout = load_function('maintained/app/app_controller.py', 'AppController.on_local_signout', {})
        with deny_external_effects():
            cancel(controller)
            signout(controller)
        signals.sync_cancelled.emit.assert_called_once_with()
        self.assertTrue(controller.sync_executing)
        controller.auth_manager.sign_out.assert_not_called()

    def test_stale_executor_completion_cannot_restart_scan_after_invalidation(self):
        controller = SimpleNamespace(sync_executing=True, _shutting_down=False,
                                     signed_in=False, _begin_sync_analysis=Mock())
        signals = SimpleNamespace(page_change=Mock())
        complete = load_function('maintained/app/app_controller.py', 'AppController.on_sync_execution_finished',
                                {'signal_manager': signals,
                                 'Page': SimpleNamespace(PROJECT='project')})
        with deny_external_effects():
            complete(controller)
        self.assertFalse(controller.sync_executing)
        controller._begin_sync_analysis.assert_not_called()
        signals.page_change.emit.assert_not_called()

    def test_unsettled_executor_blocks_signout_and_revoke_even_if_ui_flag_cleared(self):
        for method in ('on_local_signout', 'on_disconnect_clicked'):
            with self.subTest(method=method):
                controller = SimpleNamespace(sync_executing=False, _executor_unsettled=True,
                                             main_window=Mock(), auth_manager=Mock())
                action = load_function('maintained/app/app_controller.py', 'AppController.' + method, {})
                with deny_external_effects():
                    action(controller)
                controller.auth_manager.sign_out.assert_not_called()
                controller.auth_manager.disconnect.assert_not_called()

    def test_restart_requirement_prevents_bypassing_ui_with_direct_authorization(self):
        manager = SimpleNamespace(_closed=False, _auth_in_progress=False, _restart_required=True)
        start = load_function('maintained/core/auth_manager.py', 'AuthManager._start', {})
        with deny_external_effects():
            for operation in ('authorize', 'restore', 'disconnect', 'sign_out'):
                self.assertFalse(start(manager, operation))


if __name__ == '__main__':
    unittest.main()
