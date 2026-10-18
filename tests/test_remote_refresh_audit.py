"""Refresh transport/lifecycle regression evidence, with synthetic services only.

Baseline overlap demonstrates shared transport use, not a reported native crash.
No live account, authorization endpoint, browser, or real file transfer is used.
"""
from collections import deque
import copy
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import threading
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from recovered_harness import load_function, deny_external_effects
from test_recovered_scan import FakeDriveService, FakeEventLoop, FakeTimer, Signal

ROOT = Path(__file__).resolve().parents[1]
DRIVE = 'maintained/core/drive_client.py'


class ConcurrentService(FakeDriveService):
    def __init__(self, barrier):
        super().__init__({('root', None): {'files': []}})
        self.barrier = barrier
        self.active = 0
        self.overlapped = False
        self.lock = threading.Lock()
        self.close = Mock()

    def new_batch_http_request(self):
        batch = super().new_batch_http_request()
        execute = batch.execute

        def overlapping_execute():
            with self.lock:
                self.active += 1
                self.overlapped |= self.active > 1
            try:
                self.barrier.wait(timeout=3)
                execute()
            finally:
                with self.lock:
                    self.active -= 1
        batch.execute = overlapping_execute
        return batch


@unittest.skipUnless((ROOT / DRIVE).is_file() and (ROOT / 'recovered/core/drive_client.py').is_file(),
                     'Recovered/maintained fixtures absent from wheel-only validation')
class RemoteRefreshAuditTests(unittest.TestCase):
    def setUp(self):
        self.namespace = dict(deque=deque, logging=Mock(spec=logging),
            traceback=SimpleNamespace(format_exc=lambda: 'synthetic traceback'),
            tic=lambda: None, toc=lambda *args: None, print=lambda *args: None,
            QEventLoop=FakeEventLoop, QTimer=FakeTimer,
            signal_manager=SimpleNamespace(drive_file_list_acquired=Signal(), error_occurred=Signal()))

    def bind(self, client, name, namespace=None):
        method = load_function(DRIVE, 'DriveClient.' + name, namespace or self.namespace)
        setattr(client, name, MethodType(method, client))
        return getattr(client, name)

    def run_overlap(self, path):
        barrier = threading.Barrier(2)
        shared = ConcurrentService(barrier)
        services = [ConcurrentService(barrier), ConcurrentService(barrier)]
        next_service = iter(services)
        client = SimpleNamespace(service=shared, _new_service=lambda: next(next_service),
            build_drive_files=lambda *args, **kwargs: [])
        self.bind(client, '_close_service')
        scan = load_function(path, 'DriveClient.get_drive_items_from_project_id', self.namespace)
        errors = []
        def run():
            try:
                scan(client, 'root')
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=run) for _ in range(2)]
        with deny_external_effects():
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        return shared, services

    def test_original_overlapping_scans_share_transport(self):
        shared, _ = self.run_overlap('recovered/core/drive_client.py')
        self.assertTrue(shared.overlapped)
        self.assertEqual(shared.batches_executed, 2)

    def test_original_project_selection_calls_refresh_with_missing_argument(self):
        refresh = load_function('recovered/core/auth_manager.py', 'AuthManager.refresh_token', {})
        manager = SimpleNamespace(check_creds=lambda: False)
        manager.refresh_token = MethodType(refresh, manager)
        selected = load_function('recovered/app/app_controller.py',
                                 'AppController.project_selected', {})
        with deny_external_effects(), self.assertRaises(TypeError):
            selected(SimpleNamespace(auth_manager=manager), object())

    def test_original_refresh_error_signs_out_without_transient_classification(self):
        class RefreshError(Exception):
            pass
        manager = SimpleNamespace(sign_out=Mock())
        creds = SimpleNamespace(refresh=Mock(side_effect=RefreshError('temporary provider failure')))
        signals = SimpleNamespace(token_refreshed=Signal())
        refresh = load_function('recovered/core/auth_manager.py', 'AuthManager.refresh_token', {
            'Request': object, 'RefreshError': RefreshError, 'logging': Mock(),
            'signal_manager': signals})
        with deny_external_effects():
            refresh(manager, creds)
        manager.sign_out.assert_called_once_with()
        self.assertEqual(signals.token_refreshed.calls, [(False, None)])

    def test_maintained_overlapping_scans_have_owned_transports(self):
        shared, services = self.run_overlap(DRIVE)
        self.assertEqual(shared.batches_executed, 0)
        self.assertTrue(all(service.batches_executed == 1 for service in services))
        self.assertTrue(all(not service.overlapped for service in services))
        for service in services:
            service.close.assert_called_once_with()

    def test_scan_transport_closed_on_error(self):
        service = FakeDriveService({('root', None): 'batch_error'})
        service.close = Mock()
        client = SimpleNamespace(_new_service=lambda: service,
                                 build_drive_files=Mock(side_effect=AssertionError('No partial plan')))
        self.bind(client, '_close_service')
        scan = self.bind(client, 'get_drive_items_from_project_id')
        with deny_external_effects():
            scan('root')
        service.close.assert_called_once_with()
        self.assertEqual(len(self.namespace['signal_manager'].error_occurred.calls), 1)

    def test_service_factory_bounds_transport_and_uses_static_discovery(self):
        http = SimpleNamespace(close=Mock())
        transport = Mock(return_value=http)
        authorized = Mock(return_value=SimpleNamespace(close=Mock()))
        build = Mock(return_value=object())
        client = SimpleNamespace(creds=object())
        factory = self.bind(client, '_new_service', {
            'httplib2': SimpleNamespace(Http=transport), 'AuthorizedHttp': authorized,
            'build': build, 'logging': Mock()})
        with deny_external_effects():
            self.assertIs(factory(), build.return_value)
        transport.assert_called_once_with(timeout=10)
        authorized.assert_called_once_with(client.creds, http=http, max_refresh_attempts=1)
        self.assertEqual(build.call_args.kwargs, {'http': authorized.return_value,
            'cache_discovery': False, 'static_discovery': True, 'num_retries': 0})

    def test_factory_failure_closes_transport_without_masking_error(self):
        http = SimpleNamespace(close=Mock(side_effect=OSError('cleanup')))
        client = SimpleNamespace(creds=object())
        factory = self.bind(client, '_new_service', {
            'httplib2': SimpleNamespace(Http=lambda **kwargs: http),
            'AuthorizedHttp': Mock(return_value=SimpleNamespace(close=http.close)),
            'build': Mock(side_effect=ValueError('discovery')), 'logging': Mock()})
        with deny_external_effects(), self.assertRaisesRegex(ValueError, 'discovery'):
            factory()
        http.close.assert_called_once_with()

    def test_close_failure_does_not_replace_successful_scan(self):
        service = FakeDriveService({('root', None): {'files': []}})
        service.close = Mock(side_effect=OSError('cleanup'))
        client = SimpleNamespace(_new_service=lambda: service, build_drive_files=lambda *args: [])
        self.bind(client, '_close_service')
        scan = self.bind(client, 'get_drive_items_from_project_id')
        with deny_external_effects():
            scan('root')
        self.assertEqual(self.namespace['signal_manager'].drive_file_list_acquired.calls, [([], )])
        self.assertEqual(self.namespace['signal_manager'].error_occurred.calls, [])

    def test_delete_readback_failure_does_not_repeat_mutation(self):
        write = Mock(return_value={'id': 'fixture'})
        read = Mock(side_effect=OSError('temporary readback failure'))
        files = SimpleNamespace(update=Mock(return_value=SimpleNamespace(execute=write)),
            get=Mock(return_value=SimpleNamespace(execute=read)))
        service = SimpleNamespace(files=lambda: files, close=Mock())
        client = SimpleNamespace(cancelled=False, _new_service=lambda: service)
        self.bind(client, '_close_service')
        delete = self.bind(client, 'delete_file', dict(self.namespace, HttpError=type('HttpError', (Exception,), {})))
        with deny_external_effects():
            self.assertFalse(delete('fixture'))
        write.assert_called_once_with()
        read.assert_called_once_with()
        service.close.assert_called_once_with()

    def test_cancelled_folder_creation_never_starts_mutation(self):
        client = SimpleNamespace(cancelled=True, _new_service=Mock())
        create = self.bind(client, 'create_folder')
        with deny_external_effects():
            self.assertIsNone(create('fixture', 'parent'))
        client._new_service.assert_not_called()

    def test_retired_client_rejects_new_service_and_closes_late_build(self):
        original, late = SimpleNamespace(close=Mock()), SimpleNamespace(close=Mock())
        client = SimpleNamespace(_closed=False, cancelled=False, service=original,
                                 loop=SimpleNamespace(quit=Mock()))
        self.bind(client, '_close_service')
        self.bind(client, 'close')
        self.bind(client, '_finish')
        self.bind(client, '_new_service')
        with deny_external_effects():
            client.close()
            client.close()
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                client._new_service()
            client._finish(late)
        self.assertIsNone(client.service)
        original.close.assert_called_once_with()
        late.close.assert_called_once_with()
        client.loop.quit.assert_called_once_with()

    def test_replaced_discovery_service_is_closed(self):
        original, replacement = SimpleNamespace(close=Mock()), SimpleNamespace(close=Mock())
        client = SimpleNamespace(_closed=False, service=original, loop=SimpleNamespace(quit=Mock()))
        self.bind(client, '_close_service')
        finish = self.bind(client, '_finish')
        with deny_external_effects():
            finish(replacement)
        self.assertIs(client.service, replacement)
        original.close.assert_called_once_with()
        replacement.close.assert_not_called()


try:
    from google.oauth2.credentials import Credentials
    from maintained.core import google_session as session
    from maintained.core.desktop_signin import SignInIdentity
    from test_desktop_signin import CONFIG
    from test_google_session import MemoryStore
except ImportError:
    HAS_GOOGLE = False
else:
    HAS_GOOGLE = True


@unittest.skipUnless(HAS_GOOGLE and (ROOT / DRIVE).is_file(),
                     'Requires pinned Google SDK runtime and maintained source')
class RefreshStatusAuditTests(unittest.TestCase):
    def test_actual_sdk_factory_is_offline_isolated_and_timeout_bounded(self):
        from googleapiclient.discovery import build
        from google_auth_httplib2 import AuthorizedHttp
        import httplib2
        client = SimpleNamespace(creds=Credentials(token='SYNTHETIC-ACCESS'))
        factory = load_function(DRIVE, 'DriveClient._new_service', {
            'build': build, 'AuthorizedHttp': AuthorizedHttp,
            'httplib2': httplib2, 'logging': logging})
        with deny_external_effects():
            first, second = factory(client), factory(client)
            try:
                self.assertIsNot(first._http.http, second._http.http)
                self.assertEqual(first._http.http.timeout, 10)
                self.assertIs(first._http.credentials, client.creds)
                self.assertIs(second._http.credentials, client.creds)
                self.assertTrue(callable(first.files().list))
            finally:
                first.close()
                second.close()

    def test_transient_status_never_discards_login_even_with_invalid_grant_body(self):
        for status in (429, 500, 502, 503):
            with self.subTest(status=status), deny_external_effects():
                store = MemoryStore()
                notices = []
                owner = session.GoogleSession(CONFIG, store=store, on_invalidation=notices.append)
                identity = SignInIdentity('synthetic@example.invalid', 'synthetic-subject')
                expiry = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
                store.save(owner._record(identity, 'SYNTHETIC-ACCESS', 'SYNTHETIC-REFRESH', expiry))
                creds = owner.restore().credentials
                saved = copy.deepcopy(store.record)
                with patch.object(session, '_post_form', return_value=(status, {'error': 'invalid_grant'})):
                    with self.assertRaises(session.SessionError) as raised:
                        creds.refresh(None)
                self.assertEqual(raised.exception.code, 'network')
                self.assertEqual(store.record, saved)
                self.assertIs(owner.credentials, creds)
                self.assertEqual(creds.refresh_token, 'SYNTHETIC-REFRESH')
                self.assertEqual(notices, [])


if __name__ == '__main__':
    unittest.main()
