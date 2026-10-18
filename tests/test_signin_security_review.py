"""Independent identity boundary regressions; synthetic values, no live network.

The transport tests use the real pinned OAuth SDK and requests redirect logic,
but replace HTTPAdapter.send before it can contact any host. They intentionally
do not replace InstalledAppFlow.fetch_token with a permissive test double.
"""
import importlib.util
import io
import logging
from pathlib import Path
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'maintained/core/desktop_signin.py'


def load_core():
    name = '_cachecad_signin_independent_review'
    spec = importlib.util.spec_from_file_location(name, SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(SOURCE.is_file(), 'Maintained application absent in wheel fixture')
class SignInIndependentBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.core = load_core()

    def test_configuration_endpoint_variants_are_rejected(self):
        core = self.core
        config = {'installed': {
            'client_id': 'synthetic-review.apps.googleusercontent.com',
            'auth_uri': core.AUTH_URI, 'token_uri': core.TOKEN_URI,
            'redirect_uris': ['http://localhost']}}
        for field, value in (
                ('auth_uri', core.AUTH_URI + '#fragment'),
                ('auth_uri', 'https://accounts.google.com.example.invalid/o/oauth2/v2/auth'),
                ('token_uri', core.TOKEN_URI + '?destination=untrusted'),
                ('token_uri', 'https://oauth2.googleapis.com@example.invalid/token'),
                ('auth_provider_x509_cert_url', 'https://example.invalid/certs')):
            with self.subTest(field=field, value=value):
                candidate = {'installed': dict(config['installed'], **{field: value})}
                with self.assertRaises(core.DesktopSignInError) as caught:
                    core.validate_client_config(candidate)
                self.assertEqual(caught.exception.code, 'configuration')

    def test_wrong_state_cannot_convert_provider_error_into_valid_denial(self):
        for query in ('state=wrong&error=access_denied',
                      'error=access_denied',
                      'state=expected&state=expected&error=access_denied',
                      'state=expected&code=fixture&error=access_denied'):
            with self.subTest(query=query), self.assertRaises(self.core.DesktopSignInError) as caught:
                self.core._parse_callback('/?' + query, 'expected')
            self.assertEqual(caught.exception.code, 'callback')

    def test_callback_does_not_accept_host_or_path_confusion(self):
        for target in ('//example.invalid/?state=expected&code=fixture',
                       'https://127.0.0.1/?state=expected&code=fixture',
                       '/callback?state=expected&code=fixture',
                       '/?state=expected&code=fixture#fragment',
                       '/?state=expected&code=%00fixture'):
            with self.subTest(target=target), self.assertRaises(self.core.DesktopSignInError):
                self.core._parse_callback(target, 'expected')

    def test_already_cancelled_attempt_has_no_configuration_or_external_effects(self):
        core = self.core
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(core, 'load_client_config', side_effect=AssertionError('No file read')), \
                patch.object(socket.socket, 'bind', side_effect=AssertionError('No bind')), \
                patch.object(socket.socket, 'connect', side_effect=AssertionError('No network')), \
                patch.object(core.webbrowser, 'open', side_effect=AssertionError('No browser')):
            with self.assertRaises(core.SignInCancelled):
                core.DesktopSignIn('unread-synthetic-config.json').run(cancel_event=cancelled)


@unittest.skipUnless(SOURCE.is_file(), 'Maintained application absent in wheel fixture')
class SignInActualSdkBoundaryTests(unittest.TestCase):
    def setUp(self):
        try:
            import requests
            import google_auth_oauthlib.flow
        except ImportError:
            self.skipTest('Requires reviewed runtime with pinned Google OAuth SDK')
        self.requests = requests
        self.core = load_core()
        self.config = {'installed': {
            'client_id': 'synthetic-review.apps.googleusercontent.com',
            'client_secret': 'synthetic-review-secret',
            'auth_uri': self.core.AUTH_URI, 'token_uri': self.core.TOKEN_URI,
            'redirect_uris': ['http://localhost']}}
        self.enterContext(patch.object(socket.socket, 'connect',
                                       side_effect=AssertionError('Live network forbidden')))
        self.enterContext(patch.object(socket.socket, 'connect_ex',
                                       side_effect=AssertionError('Live network forbidden')))
        self.enterContext(patch.object(socket.socket, 'bind',
                                       side_effect=AssertionError('No listener needed')))

    def response(self, request, status, *, location=None, content=b'{"error":"invalid_grant"}'):
        response = self.requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = status
        response._content = content
        response._content_consumed = True
        if location is not None:
            response.headers['Location'] = location
        return response

    def attempt(self):
        class SyntheticCallback:
            redirect_uri = 'http://127.0.0.1:49111/'

            def __init__(self, **kwargs):
                pass

            def wait_for_code(self, **kwargs):
                return 'synthetic-review-code'

            def close(self):
                pass

        return self.core.DesktopSignIn(
            self.config, browser_open=lambda url: True,
            callbackserver_factory=SyntheticCallback)

    def test_real_oauth_sdk_never_follows_token_endpoint_redirect(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                requests_seen = []

                def send(adapter, request, **kwargs):
                    requests_seen.append(request)
                    if len(requests_seen) == 1:
                        return self.response(request, status, location='https://example.invalid/capture')
                    return self.response(request, 400)

                with patch('requests.adapters.HTTPAdapter.send', send):
                    with self.assertRaises(self.core.DesktopSignInError):
                        self.attempt().run(timeout=2)
                self.assertEqual([request.url for request in requests_seen],
                                 [self.core.TOKEN_URI],
                                 'Token request followed an untrusted redirect')
                body = parse_qs(requests_seen[0].body)
                self.assertEqual(body['code'], ['synthetic-review-code'])
                self.assertIn('code_verifier', body)
                self.assertNotIn('allow_redirects', body)

    def test_sdk_error_body_cannot_escape_through_logging_or_exception_context(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        previous_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, previous_level)
        synthetic_body = b'{"error":"invalid_grant","error_description":"synthetic-review-secret"}'

        def send(adapter, request, **kwargs):
            return self.response(request, 400, content=synthetic_body)

        with patch('requests.adapters.HTTPAdapter.send', send):
            with self.assertRaises(self.core.DesktopSignInError) as caught:
                self.attempt().run(timeout=2)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        for secret in ('synthetic-review-secret', 'synthetic-review-code'):
            self.assertNotIn(secret, str(caught.exception))
            self.assertNotIn(secret, stream.getvalue())

    def test_first_sdk_import_and_successful_exchange_are_quiet_at_debug(self):
        # A fresh interpreter is essential: preimporting the SDK masks incomplete
        # protection of loggers created lazily after the filter is installed.
        program = r'''
import importlib.util, io, json, logging, socket, sys, time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
spec = importlib.util.spec_from_file_location('signin_logging_review', sys.argv[1])
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)
assert not any(name.startswith(('oauthlib', 'requests_oauthlib', 'google_auth_oauthlib'))
               for name in sys.modules)
stream = io.StringIO()
root = logging.getLogger()
root.setLevel(logging.DEBUG)
root.addHandler(logging.StreamHandler(stream))
config = {'installed': {
    'client_id': 'synthetic-review.apps.googleusercontent.com',
    'client_secret': 'synthetic-review-secret', 'auth_uri': core.AUTH_URI,
    'token_uri': core.TOKEN_URI, 'redirect_uris': ['http://localhost']}}
state = {}
class Callback:
    redirect_uri = 'http://127.0.0.1:49111/'
    def __init__(self, **kwargs): pass
    def wait_for_code(self, **kwargs): return 'synthetic-review-code'
    def close(self): pass
def flow_factory(config, **kwargs):
    # All OAuth SDK modules are first imported within the real quiet context.
    flow = core._default_flow_factory(config, **kwargs)
    import requests
    def send(request, **kwargs):
        assert request.url == core.TOKEN_URI
        response = requests.Response()
        response.request, response.url, response.status_code = request, request.url, 200
        response._content_consumed = True
        response._content = json.dumps({
            'access_token': 'synthetic-review-access',
            'refresh_token': 'synthetic-review-refresh',
            'id_token': 'synthetic-review-id-token',
            'token_type': 'Bearer', 'expires_in': 3600,
            'scope': ' '.join(core.SCOPES)}).encode()
        return response
    flow.oauth2session.send = send
    return flow
def browser(url):
    state.update(parse_qs(urlsplit(url).query))
    return True
def verifier(token, **kwargs):
    return {'iss': 'https://accounts.google.com',
            'aud': config['installed']['client_id'], 'nonce': state['nonce'][0],
            'iat': int(time.time()) - 1, 'exp': int(time.time()) + 300,
            'email': 'synthetic-review@example.invalid', 'email_verified': True,
            'sub': 'synthetic-review-sub'}
with patch.object(socket.socket, 'connect', side_effect=AssertionError('No network')), \
     patch.object(socket.socket, 'bind', side_effect=AssertionError('No listener')):
    identity = core.DesktopSignIn(config, flow_factory=flow_factory,
        verifier=verifier, browser_open=browser, callbackserver_factory=Callback).run(timeout=2)
assert identity.email == 'synthetic-review@example.invalid'
logged = stream.getvalue()
assert not any(value in logged for value in (
    'synthetic-review-secret', 'synthetic-review-code', 'synthetic-review-access',
    'synthetic-review-refresh', 'synthetic-review-id-token',
    'synthetic-review@example.invalid')), 'Synthetic sensitive values escaped SDK log suppression'
print('Fresh SDK import and successful token parsing produced no sensitive logs.')
'''
        completed = subprocess.run([sys.executable, '-B', '-c', program, str(SOURCE)],
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn('no sensitive logs', completed.stdout)

    def test_google_certificate_request_cannot_follow_redirect(self):
        requests_seen = []

        def send(adapter, request, **kwargs):
            requests_seen.append(request)
            return self.response(request, 307, location='https://example.invalid/certs')

        with patch('requests.adapters.HTTPAdapter.send', send):
            with self.assertRaises(Exception):
                self.core._verify_google_id_token('synthetic-unverified-token',
                    audience=self.config['installed']['client_id'], timeout=1)
        self.assertEqual([request.url for request in requests_seen], [self.core.CERTS_URI])


if __name__ == '__main__':
    unittest.main()
