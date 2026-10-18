"""Selected real transfer methods with retained fake streams and no services.

MediaFileUpload.stream() was verified against bundled static code objects and
https://googleapis.github.io/google-api-python-client/docs/epy/googleapiclient.http.MediaIoBaseUpload-class.html
The fake service deliberately retains media; cleanup cannot depend on finalizers.
Windows sharing semantics and the real Qt event loop are separate validation.
"""
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import Mock

from recovered_harness import ROOT, deny_external_effects, load_function
from test_recovered_scan import Signal

BASELINE = 'recovered/core/drive_client.py'
MAINTAINED = 'maintained/core/drive_client.py'


class FakeHttpError(Exception):
    def __init__(self):
        super().__init__('Fixture HTTP failure')
        self.resp = SimpleNamespace(status=503)
        self.content = b'{"error":{"message":"Fixture HTTP failure"}}'


class FakeStream:
    def __init__(self, close_error=False):
        self.closed = False
        self.close_calls = 0
        self.close_error = close_error

    def close(self):
        self.close_calls += 1
        if self.close_error:
            raise OSError('Fixture stream close failure')
        self.closed = True


class TransferFixture:
    def __init__(self, source=MAINTAINED, close_error=False):
        self.stream = FakeStream(close_error)
        self.media = SimpleNamespace(stream=Mock(return_value=self.stream))
        self.client = SimpleNamespace(cancelled=False, creds=object(), keep_setting=True)
        self.signals = SimpleNamespace(file_progress=Signal(), error_occurred=Signal())
        self.request = SimpleNamespace(next_chunk=Mock(return_value=(None, {'id': 'created', 'md5Checksum': 'fixture-md5'})))
        self.files = SimpleNamespace(create=Mock(return_value=self.request),
                                     update=Mock(return_value=self.request),
                                     get_media=Mock(return_value=object()))
        self.service = SimpleNamespace(files=lambda: self.files, close=Mock())
        self.media_factory = Mock(return_value=self.media)
        self.file_factory = Mock(return_value=self.stream)
        self.downloader = SimpleNamespace(next_chunk=Mock(return_value=(None, True)))
        self.download_factory = Mock(return_value=self.downloader)
        self.build = Mock(return_value=self.service)
        self.client._new_service = self.build
        self.client._close_service = lambda service: service.close() if service is not None else None
        self.logger = Mock()
        namespace = dict(Path=Path, logging=self.logger, json=json,
                         traceback=SimpleNamespace(format_exc=lambda: 'fixture traceback'),
                         HttpError=FakeHttpError, signal_manager=self.signals,
                         build=self.build, MediaFileUpload=self.media_factory,
                         MediaIoBaseDownload=self.download_factory,
                         io=SimpleNamespace(FileIO=self.file_factory))
        self.upload = load_function(source, 'DriveClient.upload_file', namespace)
        self.download = load_function(source, 'DriveClient.download_file', namespace)


@unittest.skipUnless(all((ROOT / p).is_file() for p in (BASELINE, MAINTAINED)),
                     'Recovered/maintained source fixtures absent from wheel-only validation')
class TransferCleanupTests(unittest.TestCase):
    def setUp(self):
        effects = deny_external_effects()
        effects.__enter__()
        self.addCleanup(effects.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory(prefix='cachecad-transfer-cleanup-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'staged.step'
        self.path.write_bytes(b'disposable fixture')

    def assert_closed_once(self, fixture):
        self.assertTrue(fixture.stream.closed)
        self.assertEqual(fixture.stream.close_calls, 1)

    def test_original_upload_leaves_retained_stream_open_after_success(self):
        fixture = TransferFixture(BASELINE)
        self.assertEqual(fixture.upload(fixture.client, str(self.path), 'parent'),
                         ('created', 'fixture-md5'))
        self.assertFalse(fixture.stream.closed)
        self.assertEqual(fixture.stream.close_calls, 0)

    def test_upload_create_and_revision_close_before_return_preserving_requests(self):
        for existing in (None, 'existing-file'):
            with self.subTest(existing=existing):
                fixture = TransferFixture()
                result = fixture.upload(fixture.client, str(self.path), 'parent', 3, existing)
                self.assertEqual(result, ('created', 'fixture-md5'))
                self.assert_closed_once(fixture)
                fixture.media.stream.assert_called_once_with()
                self.assertEqual(fixture.signals.file_progress.calls, [(3, 100.0)])
                if existing:
                    fixture.files.update.assert_called_once_with(
                        fileId=existing, media_body=fixture.media,
                        fields='id, md5Checksum', supportsAllDrives=True)
                    fixture.files.create.assert_not_called()
                else:
                    fixture.files.create.assert_called_once_with(
                        body={'name': 'staged.step', 'parents': ['parent'], 'keepRevisionForever': True},
                        media_body=fixture.media, fields='id, md5Checksum', supportsAllDrives=True)
                    fixture.files.update.assert_not_called()

    def test_upload_http_and_unexpected_errors_close_and_preserve_none_return(self):
        for error in (FakeHttpError(), RuntimeError('Fixture chunk failure')):
            with self.subTest(error=type(error).__name__):
                fixture = TransferFixture()
                fixture.request.next_chunk.side_effect = error
                self.assertIsNone(fixture.upload(fixture.client, str(self.path), 'parent'))
                self.assert_closed_once(fixture)
                expected = [] if isinstance(error, FakeHttpError) else [('',)]
                self.assertEqual(fixture.signals.error_occurred.calls, expected)

    def test_upload_mid_transfer_cancel_closes_and_preserves_tuple(self):
        fixture = TransferFixture()
        def cancel():
            fixture.client.cancelled = True
            return None, None
        fixture.request.next_chunk.side_effect = cancel
        self.assertEqual(fixture.upload(fixture.client, str(self.path), 'parent'), (None, None))
        self.assert_closed_once(fixture)

    def test_upload_cancel_before_start_or_missing_path_never_acquires_stream(self):
        for cancelled in (True, False):
            with self.subTest(cancelled=cancelled):
                fixture = TransferFixture()
                fixture.client.cancelled = cancelled
                path = self.path if cancelled else self.root / 'missing.step'
                self.assertEqual(fixture.upload(fixture.client, str(path), 'parent'), (None, None))
                fixture.media_factory.assert_not_called()
                self.assertEqual(fixture.stream.close_calls, 0)

    def test_upload_constructor_failure_does_not_mask_original_error(self):
        fixture = TransferFixture()
        fixture.media_factory.side_effect = OSError('Fixture constructor failure')
        self.assertIsNone(fixture.upload(fixture.client, str(self.path), 'parent'))
        fixture.build.assert_not_called()
        self.assertEqual(fixture.stream.close_calls, 0)
        self.assertEqual(fixture.signals.error_occurred.calls, [('',)])
        fixture.logger.exception.assert_not_called()

    def test_upload_service_constructor_failure_closes_acquired_stream(self):
        fixture = TransferFixture()
        fixture.build.side_effect = RuntimeError('Fixture service failure')
        self.assertIsNone(fixture.upload(fixture.client, str(self.path), 'parent'))
        self.assert_closed_once(fixture)

    def test_upload_close_failure_never_changes_remote_outcome_or_emits_transfer_error(self):
        for outcome in ('success', 'error', 'cancel'):
            with self.subTest(outcome=outcome):
                fixture = TransferFixture(close_error=True)
                expected = ('created', 'fixture-md5')
                if outcome == 'error':
                    fixture.request.next_chunk.side_effect = FakeHttpError()
                    expected = None
                elif outcome == 'cancel':
                    def cancel():
                        fixture.client.cancelled = True
                        return None, None
                    fixture.request.next_chunk.side_effect = cancel
                    expected = (None, None)
                self.assertEqual(fixture.upload(fixture.client, str(self.path), 'parent'), expected)
                self.assertEqual(fixture.stream.close_calls, 1)
                self.assertEqual(fixture.signals.error_occurred.calls, [])
                fixture.logger.exception.assert_called_once()

    def test_original_download_leaves_retained_stream_open_after_success(self):
        fixture = TransferFixture(BASELINE)
        self.assertEqual(fixture.download(fixture.client, 'remote-file', str(self.path), 5),
                         self.root / '~$downloading5.temp')
        self.assertFalse(fixture.stream.closed)
        self.assertEqual(fixture.stream.close_calls, 0)

    def test_download_success_closes_before_return(self):
        fixture = TransferFixture()
        result = fixture.download(fixture.client, 'remote-file', str(self.path), 5)
        self.assertEqual(result, self.root / '~$downloading5.temp')
        self.assert_closed_once(fixture)
        fixture.file_factory.assert_called_once_with(result, 'wb')
        self.assertEqual(fixture.signals.file_progress.calls, [(5, 100.0)])

    def test_download_http_and_unexpected_errors_close_and_preserve_none_return(self):
        for error in (FakeHttpError(), RuntimeError('Fixture download failure')):
            with self.subTest(error=type(error).__name__):
                fixture = TransferFixture()
                fixture.downloader.next_chunk.side_effect = error
                self.assertIsNone(fixture.download(fixture.client, 'remote-file', str(self.path), 5))
                self.assert_closed_once(fixture)

    def test_download_mid_transfer_cancel_closes_and_preserves_temp_path(self):
        fixture = TransferFixture()
        def cancel():
            fixture.client.cancelled = True
            return None, False
        fixture.downloader.next_chunk.side_effect = cancel
        self.assertEqual(fixture.download(fixture.client, 'remote-file', str(self.path), 5),
                         self.root / '~$downloading5.temp')
        self.assert_closed_once(fixture)

    def test_download_cancel_before_start_does_not_create_stream(self):
        fixture = TransferFixture()
        fixture.client.cancelled = True
        self.assertIsNone(fixture.download(fixture.client, 'remote-file', str(self.path), 5))
        fixture.file_factory.assert_not_called()
        self.assertEqual(fixture.stream.close_calls, 0)

    def test_download_constructor_failures_close_only_acquired_stream(self):
        for stage in ('service', 'file', 'downloader'):
            with self.subTest(stage=stage):
                fixture = TransferFixture()
                factory = {'service': fixture.build, 'file': fixture.file_factory,
                           'downloader': fixture.download_factory}[stage]
                factory.side_effect = OSError('Fixture constructor failure')
                self.assertIsNone(fixture.download(fixture.client, 'remote-file', str(self.path), 5))
                self.assertEqual(fixture.stream.close_calls, int(stage == 'downloader'))
                self.assertEqual(fixture.signals.error_occurred.calls, [('',)])
                fixture.logger.exception.assert_not_called()

    def test_download_close_failure_preserves_return_and_is_logged(self):
        for outcome in ('success', 'error', 'cancel'):
            with self.subTest(outcome=outcome):
                fixture = TransferFixture(close_error=True)
                expected = self.root / '~$downloading5.temp'
                if outcome == 'error':
                    fixture.downloader.next_chunk.side_effect = FakeHttpError()
                    expected = None
                elif outcome == 'cancel':
                    def cancel():
                        fixture.client.cancelled = True
                        return None, False
                    fixture.downloader.next_chunk.side_effect = cancel
                self.assertEqual(fixture.download(fixture.client, 'remote-file', str(self.path), 5), expected)
                self.assertEqual(fixture.stream.close_calls, 1)
                self.assertEqual(fixture.signals.error_occurred.calls, [])
                fixture.logger.exception.assert_called_once()


if __name__ == '__main__':
    unittest.main()
