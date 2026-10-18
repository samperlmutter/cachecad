"""Offline sign-in security regressions. Synthetic identities, no browser/network.

The stdlib suite runs without the optional Google runtime. The pinned-runtime
suite additionally exercises actual OAuthlib PKCE/exchange and RSA ID verification
through an in-memory requests adapter, under socket/process tripwires.
"""

import base64
import copy
import hashlib
import io
import json
import logging
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

if not (Path(__file__).resolve().parents[1] / 'maintained/core/desktop_signin.py').is_file():
    raise unittest.SkipTest('Actual maintained application is absent from replacement wheel fixture')

from maintained.core import desktop_signin as auth
from tests.recovered_harness import deny_external_effects


CLIENT_ID = '12345-synthetic.apps.googleusercontent.com'
CONFIG = {'installed': {'client_id': CLIENT_ID, 'client_secret': 'SYNTHETIC-CLIENT',
                        'auth_uri': auth.AUTH_URI, 'token_uri': auth.TOKEN_URI,
                        'redirect_uris': ['http://localhost']}}


def claims(nonce, **updates):
    value = {'iss': 'https://accounts.google.com', 'aud': CLIENT_ID,
             'iat': int(time.time()) - 1, 'exp': int(time.time()) + 600,
             'nonce': nonce, 'email': 'synthetic@example.invalid',
             'email_verified': True, 'sub': 'synthetic-subject-123'}
    value.update(updates)
    return value


class FakeFlow:
    def __init__(self, config, **kwargs):
        self.client_config = config['installed']
        self.arguments = kwargs
        self.code_verifier = kwargs['code_verifier']
        self.oauth2session = SimpleNamespace(token={}, trust_env=True, close=lambda: None,
                                            request=lambda *args, **kwargs: None)
        self.fetches = []
        self.authorization = {}
        self.on_fetch = lambda: None

    def authorization_url(self, **kwargs):
        self.authorization = kwargs
        params = dict(kwargs, client_id=self.client_config['client_id'],
                      redirect_uri=self.arguments['redirect_uri'], response_type='code',
                      scope=' '.join(self.arguments['scopes']), code_challenge_method='S256',
                      code_challenge=base64.urlsafe_b64encode(hashlib.sha256(
                          self.code_verifier.encode()).digest()).decode().rstrip('='))
        return auth.AUTH_URI + '?' + urlencode(params), kwargs['state']

    def fetch_token(self, **kwargs):
        self.fetches.append(kwargs)
        self.on_fetch()
        self.oauth2session.token = {'id_token': 'SYNTHETIC-ID-TOKEN',
                                    'access_token': 'SYNTHETIC-ACCESS-TOKEN',
                                    'refresh_token': 'SYNTHETIC-REFRESH-TOKEN'}
        return self.oauth2session.token


class FakeServer:
    redirect_uri = 'http://127.0.0.1:39465/'

    def __init__(self, *, expected_state):
        self.state = expected_state
        self.closed = False

    def wait_for_code(self, **kwargs):
        return 'SYNTHETIC-AUTHORIZATION-CODE'

    def close(self):
        self.closed = True


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.guard = deny_external_effects()
        self.guard.__enter__()
        self.addCleanup(self.guard.__exit__, None, None, None)
        self.flow = None
        self.server = None
        self.urls = []
        self.verified = []
        self.event = threading.Event()

    def signer(self, **overrides):
        def factory(config, **kwargs):
            self.flow = FakeFlow(config, **kwargs)
            return self.flow

        def server_factory(**kwargs):
            self.server = FakeServer(**kwargs)
            return self.server

        def verifier(token, **kwargs):
            self.verified.append((token, kwargs))
            return claims(self.flow.authorization['nonce'])

        def browser(url):
            self.assertIsNotNone(self.server)
            self.assertFalse(self.server.closed)
            self.urls.append(url)
            return True

        arguments = dict(browser_open=browser, flow_factory=factory,
                         callbackserver_factory=server_factory, verifier=verifier)
        arguments.update(overrides)
        return auth.DesktopSignIn(copy.deepcopy(CONFIG), **arguments)

    def test_identity_only_online_pkce_and_token_cleanup(self):
        identity = self.signer().run()
        self.assertEqual(identity, auth.SignInIdentity('synthetic@example.invalid',
                                                      'synthetic-subject-123'))
        self.assertEqual(set(vars(identity)), {'email', 'sub'})
        params = parse_qs(urlsplit(self.urls[0]).query)
        self.assertEqual(params['access_type'], ['online'])
        self.assertEqual(set(params['scope'][0].split()), set(auth.SCOPES))
        self.assertEqual(params['code_challenge_method'], ['S256'])
        self.assertNotEqual(params['nonce'], params['state'])
        self.assertGreaterEqual(len(params['nonce'][0]), 43)
        self.assertEqual(self.verified[0][1]['audience'], CLIENT_ID)
        self.assertLessEqual(self.verified[0][1]['timeout'], 5)
        self.assertNotIn('allow_redirects', self.flow.fetches[0])
        self.assertLessEqual(self.flow.fetches[0]['timeout'], 5)
        self.assertEqual(self.flow.oauth2session.token, {})
        self.assertIsNone(self.flow.code_verifier)
        self.assertEqual(self.flow.client_config, {})
        self.assertFalse(self.flow.oauth2session.trust_env)
        self.assertTrue(self.server.closed)

    def test_pre_cancel_has_no_effects(self):
        self.event.set()
        with self.assertRaises(auth.SignInCancelled):
            self.signer().run(self.event)
        self.assertIsNone(self.server)
        self.assertFalse(self.urls)

    def test_browser_failure_closes_listener_without_exchange(self):
        with self.assertRaises(auth.DesktopSignInError) as raised:
            self.signer(browser_open=lambda url: False).run()
        self.assertEqual(raised.exception.code, 'browser')
        self.assertTrue(self.server.closed)
        self.assertFalse(self.flow.fetches)

    def test_cancellation_after_exchange_prevents_verification(self):
        signer = self.signer()
        old_factory = signer._flow_factory

        def factory(config, **kwargs):
            flow = old_factory(config, **kwargs)
            flow.on_fetch = self.event.set
            return flow

        signer._flow_factory = factory
        with self.assertRaises(auth.SignInCancelled):
            signer.run(self.event)
        self.assertFalse(self.verified)
        self.assertEqual(self.flow.oauth2session.token, {})

    def test_cancellation_after_verification_cannot_return_identity(self):
        def verifier(token, **kwargs):
            self.event.set()
            return claims(self.flow.authorization['nonce'])

        with self.assertRaises(auth.SignInCancelled):
            self.signer(verifier=verifier).run(self.event)

    def test_timeout_after_browser_prevents_token_exchange(self):
        def browser(url):
            time.sleep(0.02)
            return True

        with self.assertRaises(auth.SignInTimeout):
            self.signer(browser_open=browser).run(timeout=0.001)
        self.assertFalse(self.flow.fetches)
        self.assertTrue(self.server.closed)

    def test_errors_have_no_provider_content_cause_or_context(self):
        def verifier(*args, **kwargs):
            raise ValueError('SYNTHETIC-ACCESS-TOKEN auth_code=SECRET')

        with self.assertRaises(auth.DesktopSignInError) as raised:
            self.signer(verifier=verifier).run()
        error = raised.exception
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn('SECRET', str(error))
        self.assertNotIn('TOKEN', repr(error))
        self.assertEqual(self.flow.oauth2session.token, {})

    def test_reentrant_attempt_refused_and_next_run_allowed(self):
        signer = self.signer()
        original = signer._browser_open

        def browser(url):
            with self.assertRaises(auth.DesktopSignInError) as raised:
                signer.run()
            self.assertEqual(raised.exception.code, 'busy')
            return original(url)

        signer._browser_open = browser
        signer.run()
        first = parse_qs(urlsplit(self.urls[0]).query)
        signer.run()
        second = parse_qs(urlsplit(self.urls[1]).query)
        self.assertNotEqual(first['state'], second['state'])
        self.assertNotEqual(first['nonce'], second['nonce'])
        self.assertNotEqual(first['code_challenge'], second['code_challenge'])

    def test_bad_timeouts_rejected_before_effects(self):
        for timeout in (0, -1, 601, float('nan'), float('inf'), True, '120'):
            with self.subTest(timeout=timeout), self.assertRaises(auth.DesktopSignInError):
                self.signer().run(timeout=timeout)
        self.assertIsNone(self.server)

    def test_forged_authorization_url_never_opens_browser(self):
        signer = self.signer()
        old_factory = signer._flow_factory

        def factory(config, **kwargs):
            flow = old_factory(config, **kwargs)
            flow.authorization_url = lambda **kw: ('https://evil.invalid/?token=secret', kw['state'])
            return flow

        signer._flow_factory = factory
        with self.assertRaises(auth.DesktopSignInError):
            signer.run()
        self.assertFalse(self.urls)

    def test_bad_claims_rejected(self):
        for update in ({'iss': 'https://evil.invalid'}, {'aud': 'wrong'}, {'azp': 'wrong'},
                       {'exp': 1}, {'iat': int(time.time()) + 600}, {'exp': True},
                       {'nonce': 'wrong'}, {'nonce': None}, {'email_verified': False},
                       {'email_verified': 1}, {'email': '<script>@example.invalid'},
                       {'email': 'a\n@example.invalid'}, {'sub': ''}, {'sub': 'a\nb'}):
            with self.subTest(update=update):
                def verifier(token, **kwargs):
                    return claims(self.flow.authorization['nonce'], **update)

                with self.assertRaises(auth.DesktopSignInError):
                    self.signer(verifier=verifier).run()

    def test_missing_id_token_rejected(self):
        signer = self.signer()
        old_factory = signer._flow_factory

        def factory(config, **kwargs):
            flow = old_factory(config, **kwargs)
            flow.fetch_token = lambda **kw: {'access_token': 'SYNTHETIC'}
            return flow

        signer._flow_factory = factory
        with self.assertRaises(auth.DesktopSignInError) as raised:
            signer.run()
        self.assertEqual(raised.exception.code, 'identity')
        self.assertFalse(self.verified)


class ConfigTests(unittest.TestCase):
    def test_config_copy_optional_secret_and_old_official_auth_uri(self):
        source = copy.deepcopy(CONFIG)
        del source['installed']['client_secret']
        source['installed']['auth_uri'] = 'https://accounts.google.com/o/oauth2/auth'
        result = auth.validate_client_config(source)
        self.assertEqual(result['installed']['auth_uri'], auth.AUTH_URI)
        self.assertEqual(result['installed']['client_secret'], '')
        result['installed']['redirect_uris'].append('http://localhost:123/')
        self.assertEqual(source['installed']['redirect_uris'], ['http://localhost'])

    def test_reject_untrusted_endpoints_web_clients_and_malformed_config(self):
        bad = [None, [], {}, {'web': CONFIG['installed']},
               dict(CONFIG, web=CONFIG['installed'])]
        for key, value in (
                ('auth_uri', 'https://accounts.google.com.evil.invalid/o/oauth2/v2/auth'),
                ('auth_uri', auth.AUTH_URI + '?scope=drive'),
                ('token_uri', 'http://oauth2.googleapis.com/token'),
                ('token_uri', auth.TOKEN_URI + '/'),
                ('token_uri', 'https://evil.invalid/token'),
                ('auth_provider_x509_cert_url', 'https://evil.invalid/certs'),
                ('client_id', 'secret\n.apps.googleusercontent.com'),
                ('client_secret', 'line\nbreak'), ('redirect_uris', []),
                ('redirect_uris', ['http://evil.invalid/']),
                ('redirect_uris', ['http://localhost@evil.invalid/']),
                ('redirect_uris', ['http://localhost:invalid/']),
                ('redirect_uris', ['http://localhost/#fragment']),
                ('redirect_uris', ['urn:ietf:wg:oauth:2.0:oob']),
                ('token', 'SYNTHETIC')):
            config = copy.deepcopy(CONFIG)
            config['installed'][key] = value
            bad.append(config)
        for config in bad:
            with self.subTest(config=config), self.assertRaises(auth.DesktopSignInError) as error:
                auth.validate_client_config(config)
            self.assertEqual(error.exception.code, 'configuration')

    def test_file_bounded_duplicate_keys_rejected_and_no_copy_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'selected.json'
            path.write_text(json.dumps(CONFIG))
            self.assertEqual(auth.load_client_config(path), CONFIG)
            self.assertEqual([item.name for item in Path(directory).iterdir()], ['selected.json'])
            for content in ('{"installed":{},"installed":{}}', '{', 'x' * 65537):
                path.write_text(content)
                with self.assertRaises(auth.DesktopSignInError):
                    auth.load_client_config(path)
            with self.assertRaises(auth.DesktopSignInError):
                auth.load_client_config(Path(directory))


class CallbackTests(unittest.TestCase):
    def test_state_is_required_before_provider_error_or_code(self):
        for target in ('/?code=abc', '/?state=other&code=abc', '/?error=access_denied',
                       '/?state=other&error=access_denied', '/?state=%FF&code=abc',
                       '/?state=state&code=%ZZ'):
            with self.subTest(target=target), self.assertRaises(auth.DesktopSignInError):
                auth._parse_callback(target, 'state')

    def test_duplicate_ambiguous_or_malformed_callback_rejected(self):
        for suffix in ('state=state&state=state&code=abc', 'state=state&code=a&code=b',
                       'state=state&error=a&error=b', 'state=state&code=a&error=b',
                       'state=state', 'state=state&code=', 'state=state&code=%0A',
                       'state=state&code=a#fragment', 'state=state&code=a&bad'):
            with self.subTest(suffix=suffix), self.assertRaises(auth.DesktopSignInError):
                auth._parse_callback('/?' + suffix, 'state')
        for path in ('/favicon.ico?', '//evil.invalid/?', 'https://evil.invalid/?', '/other?'):
            with self.assertRaises(auth.DesktopSignInError):
                auth._parse_callback(path + 'state=state&code=abc', 'state')

    def test_code_success_and_safe_provider_errors(self):
        self.assertEqual(auth._parse_callback('/?state=state&code=4%2Fabc&new=value', 'state'),
                         ('4/abc', None))
        self.assertEqual(auth._parse_callback('/?state=state&error=access_denied', 'state'),
                         (None, 'denied'))
        self.assertEqual(auth._parse_callback('/?state=state&error=TOKEN-SECRET', 'state'),
                         (None, 'provider'))

    def make_server(self, connections=()):
        listener = SimpleNamespace(bind=lambda address: self.assertEqual(address, ('127.0.0.1', 0)),
                                   listen=lambda count: None, settimeout=lambda value: None,
                                   getsockname=lambda: ('127.0.0.1', 34567),
                                   close=lambda: None, setsockopt=lambda *args: None)
        queue = list(connections)

        def accept():
            if queue:
                return queue.pop(0), ('127.0.0.1', 12345)
            raise socket.timeout

        listener.accept = accept
        with patch.object(auth.socket, 'socket', return_value=listener):
            return auth.LoopbackCallbackServer(expected_state='state')

    def connection(self, raw):
        class Connection:
            data = raw
            output = b''

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def settimeout(self, timeout):
                pass

            def recv(self, length):
                value, self.data = self.data[:length], self.data[length:]
                return value

            def sendall(self, value):
                self.output += value

        return Connection()

    def test_listener_ignores_bad_requests_and_consumes_one_success(self):
        bad = self.connection(b'GET /?state=wrong&code=DO-NOT-LEAK HTTP/1.1\r\n'
                              b'Host: 127.0.0.1:34567\r\n\r\n')
        good = self.connection(b'GET /?state=state&code=GOOD-CODE HTTP/1.1\r\n'
                               b'Host: 127.0.0.1:34567\r\n\r\n')
        server = self.make_server([bad, good])
        self.assertEqual(server.redirect_uri, 'http://127.0.0.1:34567/')
        result = server.wait_for_code(cancel_event=threading.Event(), deadline=time.monotonic() + 5)
        self.assertEqual(result, 'GOOD-CODE')
        self.assertIn(b'400', bad.output)
        self.assertIn(b'200', good.output)
        self.assertIn(b'Cache-Control: no-store', good.output)
        self.assertNotIn(b'GOOD-CODE', good.output)
        self.assertNotIn(b'DO-NOT-LEAK', bad.output)
        with self.assertRaises(auth.DesktopSignInError):
            server.wait_for_code(cancel_event=threading.Event(), deadline=time.monotonic() + 5)
        server.close()

    def test_http_host_method_body_and_limits(self):
        server = self.make_server()
        for raw in (
                b'POST /?state=state&code=a HTTP/1.1\r\nHost: 127.0.0.1:34567\r\n\r\n',
                b'GET /?state=state&code=a HTTP/1.1\r\nHost: evil.invalid\r\n\r\n',
                b'GET /?state=state&code=a HTTP/1.1\r\nHost: 127.0.0.1:34567\r\nHost: evil\r\n\r\n',
                b'GET /?state=state&code=a HTTP/1.1\r\nHost: 127.0.0.1:34567\r\nContent-Length: 9\r\n\r\n',
                b'GET /' + b'x' * 8192):
            connection = self.connection(raw)
            with self.subTest(raw=raw[:80]), self.assertRaises(auth.DesktopSignInError):
                server._read_target(connection, threading.Event(), time.monotonic() + 5)
        server.close()

    def test_listener_timeout_cancel_and_request_deadline(self):
        server = self.make_server()
        with self.assertRaises(auth.SignInTimeout):
            server.wait_for_code(cancel_event=threading.Event(), deadline=time.monotonic() - 1)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(auth.SignInCancelled):
            server.wait_for_code(cancel_event=cancelled, deadline=time.monotonic() + 5)
        connection = self.connection(b'x' * 100)
        with patch.object(auth.time, 'monotonic', side_effect=[0, 0.5, 1.1]):
            with self.assertRaises(auth.DesktopSignInError):
                server._read_target(connection, threading.Event(), 5)
        server.close()


try:
    import google.auth.crypt
    import google.auth.jwt
    import google_auth_oauthlib.flow
    import requests
    import rsa
except ImportError:
    HAS_RUNTIME = False
else:
    HAS_RUNTIME = True


@unittest.skipUnless(HAS_RUNTIME, 'Optional pinned Google runtime is not installed')
class OfficialRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.public_key, cls.private_key = rsa.newkeys(1024)

    def setUp(self):
        self.guard = deny_external_effects()
        self.guard.__enter__()
        self.addCleanup(self.guard.__exit__, None, None, None)
        self.requests = []
        self.flow = None
        self.claim_updates = {}
        self.bad_signature = False
        self.certificate_payload = None
        self.cert_status = 200
        self.token_status = 200

    def make_signer(self):
        def factory(config, **kwargs):
            self.flow = google_auth_oauthlib.flow.InstalledAppFlow.from_client_config(config, **kwargs)
            return self.flow

        def browser(url):
            self.params = parse_qs(urlsplit(url).query)
            return True

        return auth.DesktopSignIn(copy.deepcopy(CONFIG), browser_open=browser,
                                  callbackserver_factory=FakeServer, flow_factory=factory)

    def send(self, session, request, **kwargs):
        self.requests.append((request, kwargs, session.trust_env))
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.headers['Content-Type'] = 'application/json'
        if request.url == auth.TOKEN_URI:
            self.assertEqual(request.method, 'POST')
            body = parse_qs(request.body)
            self.assertEqual(body['code'], ['SYNTHETIC-AUTHORIZATION-CODE'])
            self.assertEqual(body['code_verifier'], [self.flow.code_verifier])
            self.assertEqual(body['redirect_uri'], [FakeServer.redirect_uri])
            self.assertEqual(body['grant_type'], ['authorization_code'])
            self.assertNotIn('allow_redirects', body)
            signer = google.auth.crypt.RSASigner.from_string(
                self.private_key.save_pkcs1(), key_id='synthetic-key')
            token = google.auth.jwt.encode(signer, claims(self.params['nonce'][0],
                                                         **self.claim_updates)).decode()
            if self.bad_signature:
                pieces = token.split('.')
                pieces[-1] = ('A' if pieces[-1][0] != 'A' else 'B') + pieces[-1][1:]
                token = '.'.join(pieces)
            response.status_code = self.token_status
            response.headers['Location'] = 'https://evil.invalid/token'
            response._content = json.dumps({'access_token': 'SYNTHETIC-ACCESS-TOKEN',
                                             'refresh_token': 'SYNTHETIC-REFRESH-TOKEN',
                                             'id_token': token, 'token_type': 'Bearer',
                                             'scope': ' '.join(auth.SCOPES),
                                             'expires_in': 600}).encode()
        elif request.url == auth.CERTS_URI:
            self.assertEqual(request.method, 'GET')
            self.assertIsNone(request.body)
            self.assertNotIn('Authorization', request.headers)
            response.status_code = self.cert_status
            response.headers['Location'] = 'https://evil.invalid/certs'
            payload = self.certificate_payload
            if payload is None:
                payload = {'synthetic-key': self.public_key.save_pkcs1().decode()}
            response._content = json.dumps(payload).encode()
        else:
            self.fail('Unexpected endpoint attempted')
        return response

    def run_signer(self):
        owner = self

        def send(session, request, **kwargs):
            return owner.send(session, request, **kwargs)

        with patch.object(requests.Session, 'send', send):
            return self.make_signer().run()

    def test_real_library_pkce_exchange_and_signed_id_token(self):
        identity = self.run_signer()
        self.assertEqual(identity.email, 'synthetic@example.invalid')
        self.assertEqual([request.url for request, _, _ in self.requests],
                         [auth.TOKEN_URI, auth.CERTS_URI])
        for request, kwargs, trust_env in self.requests:
            self.assertFalse(kwargs['allow_redirects'])
            self.assertLessEqual(kwargs['timeout'], 5)
            self.assertFalse(trust_env)
        self.assertEqual(self.flow.oauth2session.token, {})
        self.assertIsNone(self.flow.oauth2session._client.access_token)
        self.assertIsNone(self.flow.oauth2session._client.refresh_token)

    def test_real_signature_audience_expiry_issuer_nonce_rejection(self):
        for updates, bad_signature in (({}, True), ({'aud': 'wrong'}, False),
                                       ({'exp': 1}, False), ({'iss': 'evil'}, False),
                                       ({'nonce': 'wrong'}, False)):
            with self.subTest(updates=updates, bad_signature=bad_signature):
                self.claim_updates = updates
                self.bad_signature = bad_signature
                with self.assertRaises(auth.DesktopSignInError):
                    self.run_signer()
                self.assertEqual(self.flow.oauth2session.token, {})

    def test_cert_redirect_or_jwk_response_rejected_without_followup(self):
        for status, payload in ((302, None), (200, {'keys': []}), (200, [])):
            self.cert_status = status
            self.certificate_payload = payload
            self.requests.clear()
            with self.subTest(status=status, payload=payload), self.assertRaises(auth.DesktopSignInError):
                self.run_signer()
            self.assertEqual(len(self.requests), 2)

    def test_sdk_debug_logs_never_expose_tokens_or_codes(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger('requests_oauthlib.oauth2_session')
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            self.run_signer()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
