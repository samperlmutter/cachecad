"""Real TUF verification with ephemeral test-only keys and mocked HTTPS transport.

No production key material, live feed, publisher signature, or installer execution
is supplied by these tests. They exercise the actual TUF client and cache.
"""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

if not (Path(__file__).resolve().parents[1] / 'maintained/core/application_updates.py').is_file():
    raise unittest.SkipTest('Maintained application updater absent from wheel fixture')

from maintained.core import application_updates as updates

try:
    import requests
    from securesystemslib.signer import CryptoSigner
    from tuf.api.metadata import Metadata, MetaFile, Root, Snapshot, TargetFile, Targets, Timestamp
except ImportError:
    HAS_TUF = False
else:
    HAS_TUF = True

METADATA_URL = 'https://updates.example.invalid/metadata/'
TARGET_URL = 'https://updates.example.invalid/releases/'
TARGET_PATH = 'channels/stable/x86_64/installer.exe'
INSTALLER = b'MZ SYNTHETIC TEST DATA ONLY - NOT AN EXECUTABLE\n' * 3000


class Response:
    def __init__(self, content=b'', status=200, headers=None, on_read=None):
        self.status_code = status
        self.headers = {} if headers is None else headers
        self._stream = io.BytesIO(content)
        self.raw = self
        self.on_read = on_read
        self.closed = False

    def read1(self, amount, decode_content=False):
        chunk = self._stream.read(amount)
        if self.on_read:
            self.on_read(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.closed = True


class TestRepository:
    """Fresh Ed25519 keys in memory only, with all four actual TUF roles."""

    def __init__(self):
        self.signers = {name: CryptoSigner.generate_ed25519()
                        for name in ('root', 'timestamp', 'snapshot', 'targets')}
        self.expires = datetime.now(timezone.utc) + timedelta(days=1)
        root = Metadata(Root(expires=self.expires, consistent_snapshot=False))
        for role, signer in self.signers.items():
            root.signed.add_key(signer.public_key, role)
        root.sign(self.signers['root'])
        self.root = root
        self.root_bytes = root.to_bytes()
        self.routes = {}
        self.release = {
            'schema': 1, 'product_id': 'cachecad-reconstructed', 'channel': 'stable',
            'version': '0.1.1', 'build': 2, 'architecture': 'x86_64',
            'minimum_windows_build': 19041, 'maximum_windows_build': 99999,
            'minimum_updater_version': 1, 'profile_read_min': 1, 'profile_read_max': 1,
            'profile_write_min': 1, 'profile_write_max': 1,
            'installer_url': TARGET_URL + '0.1.1/cachecad-installer.exe',
            'release_notes_url': 'https://updates.example.invalid/notes/0.1.1',
        }
        self.publish()

    def publish(self, metadata_version=1, expires=None, payload=INSTALLER,
                target=True, sign=True, target_length=None, sha256=None):
        expires = self.expires if expires is None else expires
        entry = TargetFile.from_data(TARGET_PATH, payload, ['sha256'])
        entry.unrecognized_fields['custom'] = {'cachecad': dict(self.release)}
        if target_length is not None:
            entry.length = target_length
        if sha256 is not None:
            entry.hashes['sha256'] = sha256
        self.targets = Metadata(Targets(version=metadata_version, expires=expires,
                                        targets={TARGET_PATH: entry} if target else {}))
        if sign:
            self.targets.sign(self.signers['targets'])
        raw_targets = self.targets.to_bytes()
        self.snapshot = Metadata(Snapshot(version=metadata_version, expires=expires, meta={
            'targets.json': MetaFile.from_data(metadata_version, raw_targets, ['sha256'])}))
        self.snapshot.sign(self.signers['snapshot'])
        raw_snapshot = self.snapshot.to_bytes()
        self.timestamp = Metadata(Timestamp(version=metadata_version, expires=expires,
                                            snapshot_meta=MetaFile.from_data(
                                                metadata_version, raw_snapshot, ['sha256'])))
        self.timestamp.sign(self.signers['timestamp'])
        self.routes.update({METADATA_URL + 'targets.json': raw_targets,
                            METADATA_URL + 'snapshot.json': raw_snapshot,
                            METADATA_URL + 'timestamp.json': self.timestamp.to_bytes(),
                            self.release['installer_url']: payload})

    def config(self, **changes):
        return replace(updates.UpdateConfig(METADATA_URL, TARGET_URL, self.root_bytes,
                                            ('a' * 64,)), **changes)


@unittest.skipUnless(HAS_TUF, 'Requires the pinned TUF/cryptography runtime')
class TrustedUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / 'cache'
        self.repo = TestRepository()
        self.installed = updates.InstalledRelease('cachecad-reconstructed', '0.1.0', 1,
                                                  windows_build=22631)
        self.service = updates.UpdateService(self.repo.config(), self.cache, self.installed)
        self.calls = []
        self.responses = []
        outer = self

        def get(session, url, **kwargs):
            outer.assertFalse(session.trust_env)
            outer.assertTrue(kwargs['verify'])
            outer.assertFalse(kwargs['allow_redirects'])
            outer.assertTrue(kwargs['stream'])
            outer.assertLessEqual(max(kwargs['timeout']), 3)
            outer.calls.append(url)
            route = outer.repo.routes.get(url)
            if isinstance(route, Exception):
                raise route
            if callable(route):
                result = route()
            elif isinstance(route, bytes):
                result = Response(route, headers={'Content-Length': str(len(route))})
            else:
                result = Response(status=404)
            outer.responses.append(result)
            return result

        self.patch = patch.object(requests.Session, 'get', get)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def assertError(self, code, fn, *args, **kwargs):
        with self.assertRaises(updates.UpdateError) as error:
            fn(*args, **kwargs)
        self.assertEqual(error.exception.code, code)
        return error.exception

    def test_verified_offer_is_immutable_and_identity_bound(self):
        offer = self.service.check()
        self.assertEqual(offer.version, '0.1.1')
        self.assertEqual(offer.build, 2)
        self.assertEqual(offer.sha256, hashlib.sha256(INSTALLER).hexdigest())
        self.assertEqual(offer.length, len(INSTALLER))
        self.assertEqual(offer.target_path, TARGET_PATH)
        with self.assertRaises(FrozenInstanceError):
            offer.build = 99
        self.assertNotIn(offer.installer_url, self.calls)

    def test_download_rechecks_then_atomically_caches_exact_target(self):
        offer = self.service.check()
        before = self.calls.count(METADATA_URL + 'timestamp.json')
        progress = []
        result = self.service.download(offer, progress=lambda a, b: progress.append((a, b)))
        self.assertEqual(self.calls.count(METADATA_URL + 'timestamp.json'), before + 1)
        self.assertEqual(result.path.read_bytes(), INSTALLER)
        self.assertEqual(result.offer, offer)
        self.assertEqual(progress[0], (0, len(INSTALLER)))
        self.assertEqual(progress[-1], (len(INSTALLER), len(INSTALLER)))
        self.assertFalse(list(self.cache.rglob('*.part')))
        self.assertTrue(all(response.closed for response in self.responses))
        self.assertFalse(hasattr(result, 'publisher_verified'))

    def test_cached_download_is_rehashed_without_fetching_installer(self):
        offer = self.service.check()
        first = self.service.download(offer)
        count = self.calls.count(offer.installer_url)
        self.assertEqual(self.service.download(offer), first)
        self.assertEqual(self.calls.count(offer.installer_url), count)

    def test_corrupt_cached_installer_is_removed_and_never_returned(self):
        offer = self.service.check()
        result = self.service.download(offer)
        result.path.write_bytes(b'X' * offer.length)
        self.assertError('verification', self.service.download, offer)
        self.assertFalse(result.path.exists())
        # A later explicit retry can safely fetch the authenticated bytes again.
        self.assertEqual(self.service.download(offer).path.read_bytes(), INSTALLER)

    def test_symlink_cached_installer_is_not_followed(self):
        offer = self.service.check()
        result = self.service.download(offer)
        outside = Path(self.temp.name) / 'outside-installer'
        outside.write_bytes(INSTALLER)
        result.path.unlink()
        result.path.symlink_to(outside)
        self.assertError('storage', self.service.download, offer)
        self.assertEqual(outside.read_bytes(), INSTALLER)

    def test_disk_full_during_download_preserves_installed_app_and_removes_partial(self):
        offer = self.service.check()
        with patch.object(updates.os, 'fsync', side_effect=OSError(errno.ENOSPC, 'private path')):
            error = self.assertError('storage', self.service.download, offer)
        self.assertNotIn('private path', str(error))
        self.assertFalse(list(self.cache.rglob('*.part')))
        self.assertFalse(list((self.cache / 'downloads').glob('*.exe')))

    def test_cancel_from_progress_before_promotion_removes_partial(self):
        offer = self.service.check()
        cancel = threading.Event()
        def progress(received, total):
            if received == total:
                cancel.set()
        self.assertError('cancelled', self.service.download, offer, cancel, progress)
        self.assertFalse(list(self.cache.rglob('*.part')))
        self.assertFalse(list((self.cache / 'downloads').glob('*.exe')))

    def test_stale_partial_download_is_removed_under_cache_lock(self):
        self.service.check()
        partial = self.cache / 'downloads/download-crashed.part'
        partial.write_bytes(b'partial')
        self.service.check()
        self.assertFalse(partial.exists())

    def test_disabled_never_contacts_network_or_creates_cache(self):
        service = updates.UpdateService(None, self.cache, self.installed)
        self.assertFalse(service.configured)
        self.assertError('not_configured', service.check)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.cache.exists())

    def test_equal_installed_returns_none_only_after_authentication(self):
        service = updates.UpdateService(self.repo.config(), self.cache,
                                         replace(self.installed, version='0.1.1', build=2))
        self.assertIsNone(service.check())
        self.assertIn(METADATA_URL + 'timestamp.json', self.calls)

    def test_authenticated_withdrawal_returns_none(self):
        self.service.check()
        self.repo.publish(metadata_version=2, target=False)
        self.assertIsNone(self.service.check())

    def test_withdrawal_prevents_download_of_retained_offer(self):
        offer = self.service.check()
        self.repo.publish(metadata_version=2, target=False)
        self.assertError('changed', self.service.download, offer)
        self.assertNotIn(offer.installer_url, self.calls)

    def test_forged_offer_cannot_supply_new_hash_or_url(self):
        offer = self.service.check()
        for altered in (replace(offer, sha256='b' * 64),
                        replace(offer, installer_url=TARGET_URL + 'malware.exe')):
            self.assertError('changed', self.service.download, altered)
        self.assertNotIn(offer.installer_url, self.calls)

    def test_unsigned_targets_rejected(self):
        self.repo.publish(sign=False)
        self.assertError('verification', self.service.check)

    def test_unknown_signing_key_rejected(self):
        self.repo.signers['targets'] = CryptoSigner.generate_ed25519()
        self.repo.publish()
        self.assertError('verification', self.service.check)

    def test_modified_timestamp_signature_rejected(self):
        raw = json.loads(self.repo.routes[METADATA_URL + 'timestamp.json'])
        raw['signed']['version'] = 77
        self.repo.routes[METADATA_URL + 'timestamp.json'] = json.dumps(raw).encode()
        self.assertError('verification', self.service.check)

    def test_expired_metadata_rejected(self):
        self.repo.publish(expires=datetime.now(timezone.utc) - timedelta(days=1))
        self.assertError('verification', self.service.check)

    def test_cached_metadata_expires_on_later_check(self):
        self.service.check()
        future = datetime.now(timezone.utc) + timedelta(days=2)
        with patch('tuf.ngclient._internal.trusted_metadata_set.datetime') as clock:
            clock.datetime.now.return_value = future
            clock.timezone.utc = timezone.utc
            self.assertError('verification', self.service.check)

    def test_clock_before_build_date_refused_without_network(self):
        service = updates.UpdateService(self.repo.config(), self.cache,
                                         replace(self.installed, build_date='2026-10-18'))
        before_build = int(datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp())
        with patch.object(updates, '_utc_now', return_value=before_build):
            self.assertError('clock', service.check)
        self.assertEqual(self.calls, [])

    def test_clock_rollback_remembered_across_service_restart(self):
        now = updates._utc_now()
        with patch.object(updates, '_utc_now', return_value=now):
            self.service.check()
        self.calls.clear()
        restarted = updates.UpdateService(self.repo.config(), self.cache, self.installed)
        with patch.object(updates, '_utc_now', return_value=now - 301):
            self.assertError('clock', restarted.check)
        self.assertEqual(self.calls, [])

    def test_small_clock_adjustment_does_not_lower_saved_clock(self):
        now = updates._utc_now()
        with patch.object(updates, '_utc_now', return_value=now):
            self.service.check()
        with patch.object(updates, '_utc_now', return_value=now - 299):
            self.service.check()
        self.assertEqual(json.loads((self.cache / 'clock-state.json').read_text())[
            'verified_utc'], now)
        with patch.object(updates, '_utc_now', return_value=now - 598):
            self.assertError('clock', self.service.check)

    def test_clock_regression_during_check_refused(self):
        now = updates._utc_now()
        with patch.object(updates, '_utc_now', side_effect=(now, now - 301)):
            self.assertError('clock', self.service.check)

    def test_malformed_clock_state_is_not_silently_reset(self):
        self.service.check()
        (self.cache / 'clock-state.json').write_text('{"schema":1,"verified_utc":false}')
        self.assertError('storage', self.service.check)

    def test_newer_build_date_does_not_invalidate_an_older_valid_clock_record(self):
        now = updates._utc_now()
        self.service.check()
        (self.cache / 'clock-state.json').write_text(json.dumps({
            'schema': 1, 'verified_utc': int(datetime(2026, 10, 17,
                                                   tzinfo=timezone.utc).timestamp())}))
        with patch.object(updates, '_utc_now', return_value=now):
            self.assertEqual(self.service.check().build, 2)

    def test_metadata_rollback_after_restart_rejected(self):
        old = dict(self.repo.routes)
        self.repo.publish(metadata_version=2)
        self.service.check()
        self.repo.routes = old
        restarted = updates.UpdateService(self.repo.config(), self.cache, self.installed)
        self.assertError('verification', restarted.check)

    def test_release_downgrade_in_newly_signed_metadata_rejected(self):
        self.service.check()
        self.repo.release.update(version='0.1.0', build=1)
        self.repo.publish(metadata_version=2)
        self.assertError('rollback', self.service.check)

    def test_same_version_build_cannot_change_hash(self):
        self.service.check()
        self.repo.publish(metadata_version=2, payload=b'different installer')
        self.assertError('rollback', self.service.check)

    def test_installed_downgrade_rejected(self):
        service = updates.UpdateService(self.repo.config(), self.cache,
                                         replace(self.installed, version='0.2.0', build=3))
        self.assertError('rollback', service.check)

    def test_semantic_version_comparison_not_lexicographic(self):
        self.repo.release.update(version='0.10.0', build=10)
        self.repo.publish()
        service = updates.UpdateService(self.repo.config(), self.cache,
                                         replace(self.installed, version='0.9.0', build=9))
        self.assertEqual(service.check().version, '0.10.0')

    def test_wrong_product_channel_architecture_and_schema_rejected(self):
        changes = {'product_id': 'another-product', 'channel': 'beta', 'architecture': 'arm64',
                   'profile_read_min': 2, 'profile_write_max': 0,
                   'minimum_windows_build': 99999, 'maximum_windows_build': 10000,
                   'minimum_updater_version': 2, 'version': '0.1.1-beta.1'}
        for key, value in changes.items():
            with self.subTest(key=key):
                old = self.repo.release[key]
                self.repo.release[key] = value
                self.repo.publish()
                cache = Path(self.temp.name) / key
                service = updates.UpdateService(self.repo.config(), cache, self.installed)
                with self.assertRaises(updates.UpdateError):
                    service.check()
                self.repo.release[key] = old

    def test_unknown_release_schema_rejected(self):
        self.repo.release['schema'] = 2
        self.repo.publish()
        self.assertError('verification', self.service.check)

    def test_signed_commands_are_not_accepted(self):
        self.repo.release['arguments'] = '/SILENT'
        self.repo.publish()
        self.assertError('verification', self.service.check)

    def test_signed_installer_url_cannot_escape_owner_base(self):
        for url in ('http://updates.example.invalid/release.exe',
                    'https://other.example.invalid/release.exe',
                    TARGET_URL + '../release.exe', TARGET_URL + '%2e%2e/release.exe',
                    TARGET_URL + 'release.exe?token=secret', TARGET_URL + 'release.cmd'):
            with self.subTest(url=url):
                self.repo.release['installer_url'] = url
                self.repo.publish()
                self.assertError('verification', self.service.check)
                self.assertNotIn(url, self.calls)

    def test_truncated_and_oversized_and_tampered_bytes_rejected(self):
        offer = self.service.check()
        for payload in (INSTALLER[:-1], INSTALLER + b'extra', b'X' * len(INSTALLER)):
            with self.subTest(length=len(payload)):
                self.repo.routes[offer.installer_url] = payload
                self.assertError('verification', self.service.download, offer)
                self.assertFalse(list((self.cache / 'downloads').glob('*.exe')))
                self.assertFalse(list(self.cache.rglob('*.part')))

    def test_maximum_installer_size_enforced_before_download(self):
        self.repo.publish(target_length=1024**3 + 1)
        self.assertError('verification', self.service.check)
        self.assertNotIn(self.repo.release['installer_url'], self.calls)

    def test_redirect_to_unapproved_origin_never_requested(self):
        offer = self.service.check()
        bad = 'https://attacker.example.invalid/installer.exe'
        self.repo.routes[offer.installer_url] = lambda: Response(status=302,
                                                               headers={'Location': bad})
        self.assertError('verification', self.service.download, offer)
        self.assertNotIn(bad, self.calls)

    def test_redirect_to_approved_origin_verified_against_tuf(self):
        approved = 'https://cdn.example.invalid'
        service = updates.UpdateService(self.repo.config(allowed_redirect_origins=(approved,)),
                                         self.cache, self.installed)
        offer = service.check()
        self.repo.routes[offer.installer_url] = lambda: Response(
            status=302, headers={'Location': approved + '/file.exe'})
        self.repo.routes[approved + '/file.exe'] = INSTALLER
        self.assertEqual(service.download(offer).path.read_bytes(), INSTALLER)

    def test_metadata_redirect_does_not_expand_origin_permissions(self):
        bad = 'https://attacker.example.invalid/timestamp.json'
        self.repo.routes[METADATA_URL + 'timestamp.json'] = lambda: Response(
            status=302, headers={'Location': bad})
        self.assertError('verification', self.service.check)
        self.assertNotIn(bad, self.calls)

    def test_redirect_loop_bounded(self):
        self.repo.routes[METADATA_URL + 'timestamp.json'] = lambda: Response(
            status=302, headers={'Location': METADATA_URL + 'timestamp.json'})
        self.assertError('verification', self.service.check)
        self.assertEqual(self.calls.count(METADATA_URL + 'timestamp.json'), 4)

    def test_cancel_before_operation_makes_no_network_request(self):
        cancel = threading.Event()
        cancel.set()
        self.assertError('cancelled', self.service.check, cancel)
        self.assertEqual(self.calls, [])

    def test_cancel_mid_download_removes_partial_bytes(self):
        offer = self.service.check()
        cancel = threading.Event()
        self.repo.routes[offer.installer_url] = lambda: Response(
            INSTALLER, on_read=lambda chunk: cancel.set())
        self.assertError('cancelled', self.service.download, offer, cancel)
        self.assertFalse(list((self.cache / 'downloads').glob('*.exe')))
        self.assertFalse(list(self.cache.rglob('*.part')))

    def test_cancel_during_metadata_is_not_reported_as_network_failure(self):
        cancel = threading.Event()
        timestamp = self.repo.routes[METADATA_URL + 'timestamp.json']
        self.repo.routes[METADATA_URL + 'timestamp.json'] = lambda: Response(
            timestamp, on_read=lambda chunk: cancel.set())
        self.assertError('cancelled', self.service.check, cancel)

    def test_offline_is_safe_error(self):
        self.repo.routes[METADATA_URL + 'timestamp.json'] = requests.ConnectionError('secret')
        error = self.assertError('network', self.service.check)
        self.assertNotIn('secret', str(error))

    def test_socket_timeout_is_safe_and_not_retried(self):
        self.repo.routes[METADATA_URL + 'timestamp.json'] = requests.ReadTimeout('private URL')
        self.assertError('network', self.service.check)
        self.assertEqual(self.calls.count(METADATA_URL + 'timestamp.json'), 1)

    def test_cached_metadata_is_not_used_when_offline(self):
        self.service.check()
        self.repo.routes[METADATA_URL + 'timestamp.json'] = requests.ConnectionError()
        self.assertError('network', self.service.check)

    def test_response_encoding_and_size_abuse_rejected(self):
        for headers in ({'Content-Encoding': 'gzip'}, {'Content-Length': str(3 * 1024**2)}):
            self.repo.routes[METADATA_URL + 'timestamp.json'] = lambda: Response(
                b'x', headers=headers)
            self.assertError('verification', self.service.check)

    def test_deadline_stops_before_network(self):
        with patch.object(updates.time, 'monotonic', side_effect=(0.0, 2.0)):
            fetcher = updates._BoundedFetcher(self.repo.config(), None, 1)
            self.assertError('timeout', lambda: list(fetcher.stream(TARGET_URL + 'x.exe', 100)))
        self.assertEqual(self.calls, [])

    def test_repeat_click_and_competing_cache_lock_refused(self):
        with self.service._mutex:
            self.assertError('busy', self.service.check)
        self.cache.mkdir()
        with updates._profile_lock(self.cache):
            self.assertError('busy', self.service.check)

    def test_symlink_cache_refused(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        self.cache.symlink_to(outside, target_is_directory=True)
        self.assertError('storage', self.service.check)
        self.assertEqual(self.calls, [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_corrupt_high_water_state_fails_closed(self):
        self.service.check()
        (self.cache / 'release-state.json').write_text('{corrupt')
        self.assertError('storage', self.service.check)

    def test_root_rotation_requires_old_and_new_root_signatures(self):
        old_root_signer = self.repo.signers['root']
        new_root_signer = CryptoSigner.generate_ed25519()
        root = Metadata.from_bytes(self.repo.root_bytes)
        root.signed.version = 2
        root.signed.revoke_key(old_root_signer.public_key.keyid, 'root')
        root.signed.add_key(new_root_signer.public_key, 'root')
        root.sign(old_root_signer)
        root.sign(new_root_signer, append=True)
        self.repo.routes[METADATA_URL + '2.root.json'] = root.to_bytes()
        self.assertEqual(self.service.check().build, 2)
        self.assertTrue((self.cache / 'metadata/root_history/2.root.json').exists())
        # Restart from bundled v1 still verifies the cached v1->v2 chain.
        self.assertEqual(updates.UpdateService(self.repo.config(), self.cache,
                                               self.installed).check().build, 2)

    def test_untrusted_root_rotation_refused(self):
        root = Metadata.from_bytes(self.repo.root_bytes)
        root.signed.version = 2
        root.sign(CryptoSigner.generate_ed25519())
        self.repo.routes[METADATA_URL + '2.root.json'] = root.to_bytes()
        self.assertError('verification', self.service.check)

    def test_rotation_missing_new_root_signature_refused(self):
        root = Metadata.from_bytes(self.repo.root_bytes)
        root.signed.version = 2
        old = self.repo.signers['root']
        root.signed.revoke_key(old.public_key.keyid, 'root')
        root.signed.add_key(CryptoSigner.generate_ed25519().public_key, 'root')
        root.sign(old)
        self.repo.routes[METADATA_URL + '2.root.json'] = root.to_bytes()
        self.assertError('verification', self.service.check)


class UpdatePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_missing_and_disabled_policy_require_no_dependencies_or_root(self):
        self.assertIsNone(updates.load_update_config(self.directory))
        (self.directory / 'update-policy.json').write_text('{"schema":1,"configured":false}')
        self.assertIsNone(updates.load_update_config(self.directory))

    def test_no_policy_environment_override(self):
        with patch.dict('os.environ', {'CACHECAD_UPDATE_URL': 'https://attacker.example.invalid/'}):
            self.assertIsNone(updates.load_update_config(self.directory))

    def test_duplicate_fields_unknown_keys_and_incomplete_enabled_policy_rejected(self):
        for value in ('{"schema":1,"configured":true}',
                      '{"schema":1,"configured":false,"url":"https://attacker.invalid/"}',
                      '{"schema":1,"schema":1,"configured":false}',
                      '{"schema":true,"configured":false}'):
            (self.directory / 'update-policy.json').write_text(value)
            with self.assertRaises(updates.UpdateError) as caught:
                updates.load_update_config(self.directory)
            self.assertEqual(caught.exception.code, 'configuration')

    @unittest.skipUnless(HAS_TUF, 'Requires pinned TUF runtime')
    def test_enabled_policy_loads_only_bundled_public_root_and_certificate_pins(self):
        repo = TestRepository()
        policy = {'schema': 1, 'configured': True, 'metadata_base_url': METADATA_URL,
                  'target_base_url': TARGET_URL, 'channel': 'stable',
                  'allowed_redirect_origins': [], 'publisher_certificate_sha256': ['a' * 64],
                  'max_installer_bytes': 1024**3}
        (self.directory / 'update-policy.json').write_text(json.dumps(policy))
        with self.assertRaises(updates.UpdateError):
            updates.load_update_config(self.directory)
        (self.directory / 'update-root.json').write_bytes(repo.root_bytes)
        self.assertEqual(updates.load_update_config(self.directory), repo.config())

    def test_unsafe_owner_urls_and_empty_pins_refused(self):
        base = updates.UpdateConfig(METADATA_URL, TARGET_URL, b'{}', ('a' * 64,))
        configs = [replace(base, publisher_certificate_sha256=()),
                   replace(base, metadata_base_url='http://example.invalid/'),
                   replace(base, metadata_base_url='https://127.0.0.1/'),
                   replace(base, metadata_base_url='https://user:password@example.invalid/'),
                   replace(base, target_base_url='https://example.invalid/releases'),
                   replace(base, allowed_redirect_origins=('https://example.invalid/path',))]
        installed = updates.InstalledRelease('cachecad-reconstructed', '0.1.0', 1)
        for config in configs:
            with self.subTest(config=config), self.assertRaises(updates.UpdateError):
                updates.UpdateService(config, self.directory, installed)

    def test_semver_numeric_prerelease_and_release_ordering(self):
        versions = ['1.0.0-alpha', '1.0.0-alpha.1', '1.0.0-alpha.beta', '1.0.0-beta',
                    '1.0.0-beta.2', '1.0.0-beta.11', '1.0.0-rc.1', '1.0.0']
        self.assertEqual(sorted(versions, key=updates._version), versions)
        self.assertEqual(updates._version('1.0.0+one'), updates._version('1.0.0+two'))
        for invalid in ('1.2', '01.2.3', '1.2.3-01', ' 1.2.3', 'v1.2.3'):
            with self.assertRaises(ValueError):
                updates._version(invalid)

    def test_build_date_must_be_canonical_iso_date(self):
        for value in ('20261018', '2026-99-99', True, 12345):
            installed = updates.InstalledRelease('cachecad-reconstructed', '0.1.0', 1,
                                                  build_date=value)
            with self.assertRaises(updates.UpdateError):
                updates.UpdateService(None, self.directory, installed)


if __name__ == '__main__':
    unittest.main()
