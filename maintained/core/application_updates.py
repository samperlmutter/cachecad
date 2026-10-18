"""Owner-rooted TUF discovery and bounded downloads; never executes an installer.

Call from a worker, not Qt's UI thread. The installed application policy and root
are the only configuration source; there are no vendor endpoints, environment
feed overrides, credentials, or TOFU. TUF metadata and a local release high-water
mark survive checks. Downloads reauthenticate the current offer, verify exact
TUF length/hashes, then atomically enter a private cache. ``VerifiedDownload``
means metadata/content verification only: Windows publisher trust, locked-file
reverification and user-approved handoff remain separate required gates.

Cancellation and deadlines are checked between network reads. Socket operations
have short timeouts; OS DNS resolution is not hard-real-time cancellable. We do
not claim protection from arbitrary code already executing as this Windows user.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit

try:
    from tuf.api import exceptions as _tuf_errors
    from tuf.ngclient import Updater, UpdaterConfig
    from tuf.ngclient.fetcher import FetcherInterface
except ImportError:
    Updater = UpdaterConfig = _tuf_errors = None
    FetcherInterface = object


_MESSAGES = {
    'not_configured': 'Application updates are not configured by the publisher.',
    'configuration': 'The installed update policy or root of trust is invalid.',
    'dependencies': 'The trusted update dependencies are unavailable.',
    'verification': 'This update could not be authenticated. No installer was accepted.',
    'incompatible': 'This update is incompatible with this application or Windows version.',
    'rollback': 'Older or conflicting update metadata was refused.',
    'changed': 'The offered update changed or was withdrawn. Check for updates again.',
    'network': 'The update service could not be reached securely. Try again later.',
    'timeout': 'The update operation timed out. Try again later.',
    'storage': 'The private update cache could not be accessed safely.',
    'busy': 'Another update operation is already running.',
    'clock': 'The system clock is incorrect or moved backwards. Correct it before updating.',
    'cancelled': 'The update operation was cancelled.',
}
_MAX_INSTALLER = 1024 * 1024 * 1024
_MAX_METADATA = 2 * 1024 * 1024
_MAX_POLICY = 16384
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_NAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}\Z')
_SEMVER = re.compile(
    r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    r'(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?'
    r'(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z')


class UpdateError(Exception):
    """Sanitized error boundary: do not display HTTP bodies, paths or raw errors."""

    def __init__(self, code='verification'):
        self.code = code if code in _MESSAGES else 'verification'
        super().__init__(_MESSAGES[self.code])


class UpdateNotConfigured(UpdateError):
    def __init__(self):
        super().__init__('not_configured')


class UpdateCancelled(UpdateError):
    def __init__(self):
        super().__init__('cancelled')


@dataclass(frozen=True)
class UpdateConfig:
    metadata_base_url: str
    target_base_url: str
    trusted_root: bytes
    publisher_certificate_sha256: tuple[str, ...]
    channel: str = 'stable'
    allowed_redirect_origins: tuple[str, ...] = ()
    max_installer_bytes: int = _MAX_INSTALLER


@dataclass(frozen=True)
class InstalledRelease:
    product_id: str
    version: str
    build: int
    architecture: str = 'x86_64'
    profile_schema: int = 1
    updater_version: int = 1
    windows_build: int = 0
    build_date: str | None = None


@dataclass(frozen=True)
class ReleaseOffer:
    product_id: str
    channel: str
    version: str
    build: int
    architecture: str
    minimum_windows_build: int
    maximum_windows_build: int
    minimum_updater_version: int
    profile_read_min: int
    profile_read_max: int
    profile_write_min: int
    profile_write_max: int
    installer_url: str
    release_notes_url: str
    length: int
    sha256: str
    target_path: str


@dataclass(frozen=True)
class VerifiedDownload:
    """TUF/hash verified content, NOT Authenticode or execution authorization."""

    offer: ReleaseOffer
    path: Path


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise UpdateCancelled()


def _integer(value, minimum=1, maximum=2**31 - 1):
    return type(value) is int and minimum <= value <= maximum


def _utc_now():
    return int(datetime.now(timezone.utc).timestamp())


def _version(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError('Invalid semantic version')
    match = _SEMVER.fullmatch(value)
    if not match:
        raise ValueError('Invalid semantic version')
    pre = match[4]
    parts = () if pre is None else tuple(pre.split('.'))
    if any(p.isdigit() and len(p) > 1 and p[0] == '0' for p in parts):
        raise ValueError('Invalid prerelease')
    # Numeric prerelease identifiers sort before non-numeric; a final release
    # sorts after every prerelease. Build metadata does not affect precedence.
    return (tuple(int(match[i]) for i in (1, 2, 3)), pre is None,
            tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts))


def _url(value, *, base=False):
    if (not isinstance(value, str) or len(value) > 2048 or not value.isascii()
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
            or '\\' in value or '%' in value):
        raise ValueError('Invalid URL')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment):
        raise ValueError('HTTPS URL required')
    host = parsed.hostname
    if (host != host.lower() or not re.fullmatch(r'[a-z0-9.-]+', host)
            or host.endswith('.') or host == 'localhost' or '.' not in host):
        raise ValueError('Invalid HTTPS host')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('Literal IP update origins are not allowed')
    origin = 'https://' + host
    if parsed.netloc not in (host, host + ':443'):
        raise ValueError('Noncanonical origin')
    if any(p in ('.', '..') for p in parsed.path.split('/')) or '//' in parsed.path:
        raise ValueError('Noncanonical path')
    if base and not parsed.path.endswith('/'):
        raise ValueError('Base URL must end in a slash')
    return origin


def _config_valid(config):
    try:
        if not isinstance(config, UpdateConfig):
            raise ValueError
        _url(config.metadata_base_url, base=True)
        _url(config.target_base_url, base=True)
        if (not _NAME.fullmatch(config.channel)
                or not isinstance(config.trusted_root, bytes)
                or not 1 <= len(config.trusted_root) <= 512000
                or not isinstance(config.publisher_certificate_sha256, tuple)
                or not 1 <= len(config.publisher_certificate_sha256) <= 8
                or any(not isinstance(p, str) or not _HEX.fullmatch(p)
                       for p in config.publisher_certificate_sha256)
                or not isinstance(config.allowed_redirect_origins, tuple)
                or len(config.allowed_redirect_origins) > 8
                or not _integer(config.max_installer_bytes, 1, _MAX_INSTALLER)):
            raise ValueError
        for origin in config.allowed_redirect_origins:
            if _url(origin) != origin:
                raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise UpdateError('configuration') from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON member')
        result[key] = value
    return result


def _check_path(path):
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise UpdateError('storage')


def _read_file(path, limit):
    _check_path(path)
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise UpdateError('storage')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise UpdateError('storage')
    return raw


def load_update_config(application_dir):
    """Read only installer-owned policy/root paths supplied by the application.

    Missing or explicitly disabled policy is unconfigured. A malformed enabled
    policy never falls back to vendor URLs or a root obtained over the network.
    """
    directory = Path(application_dir).absolute()
    policy_path = directory / 'update-policy.json'
    try:
        if not policy_path.exists():
            return None
        policy = json.loads(_read_file(policy_path, _MAX_POLICY), object_pairs_hook=_unique)
        if not isinstance(policy, dict) or type(policy.get('schema')) is not int:
            raise ValueError
        if policy.get('schema') != 1 or type(policy.get('configured')) is not bool:
            raise ValueError
        if policy['configured'] is False:
            if set(policy) != {'schema', 'configured'}:
                raise ValueError
            return None
        fields = {'schema', 'configured', 'metadata_base_url', 'target_base_url', 'channel',
                  'allowed_redirect_origins', 'publisher_certificate_sha256',
                  'max_installer_bytes'}
        if set(policy) != fields:
            raise ValueError
        if (not isinstance(policy['allowed_redirect_origins'], list)
                or not isinstance(policy['publisher_certificate_sha256'], list)):
            raise ValueError
        config = UpdateConfig(
            metadata_base_url=policy['metadata_base_url'],
            target_base_url=policy['target_base_url'],
            trusted_root=_read_file(directory / 'update-root.json', 512000),
            publisher_certificate_sha256=tuple(policy['publisher_certificate_sha256']),
            channel=policy['channel'],
            allowed_redirect_origins=tuple(policy['allowed_redirect_origins']),
            max_installer_bytes=policy['max_installer_bytes'])
        _config_valid(config)
        return config
    except (OSError, ValueError, TypeError, KeyError, UpdateError):
        raise UpdateError('configuration') from None


class _BoundedFetcher(FetcherInterface):
    """TUF transport with an explicit origin policy and no implicit retries."""

    def __init__(self, config, cancel, timeout):
        self.config = config
        self.cancel = cancel
        self.deadline = time.monotonic() + timeout
        self.failure_code = None
        self.allowed = {_url(config.metadata_base_url), _url(config.target_base_url),
                        *config.allowed_redirect_origins}

    def _checkpoint(self):
        _cancel(self.cancel)
        if time.monotonic() >= self.deadline:
            raise UpdateError('timeout')

    def _fetch(self, url):
        return self.stream(url, _MAX_METADATA)

    def stream(self, url, maximum):
        import requests
        self._checkpoint()
        try:
            with requests.Session() as session:
                session.trust_env = False
                session.headers.update({'Accept-Encoding': 'identity',
                                        'User-Agent': 'CacheCAD-Updater/1'})
                for redirect in range(4):
                    self._checkpoint()
                    try:
                        if _url(url) not in self.allowed:
                            raise ValueError
                    except ValueError:
                        raise UpdateError('verification') from None
                    remaining = max(0.1, self.deadline - time.monotonic())
                    with session.get(url, timeout=(min(3.0, remaining), min(2.0, remaining)),
                                     allow_redirects=False, stream=True, verify=True) as response:
                        self._checkpoint()
                        if response.status_code in (301, 302, 303, 307, 308):
                            if redirect == 3 or not response.headers.get('Location'):
                                raise UpdateError('verification')
                            # Validate before making ANY request to the redirected destination.
                            url = urljoin(url, response.headers['Location'])
                            continue
                        if response.status_code != 200:
                            raise _tuf_errors.DownloadHTTPError(
                                'Update HTTP request failed', response.status_code)
                        if response.headers.get('Content-Encoding', 'identity') != 'identity':
                            raise UpdateError('verification')
                        length = response.headers.get('Content-Length')
                        if length is not None and (not length.isdigit() or int(length) > maximum):
                            raise UpdateError('verification')
                        size = 0
                        while True:
                            self._checkpoint()
                            # read1 returns available bytes rather than waiting to fill a
                            # large chunk, allowing deadline checks on slow/dripping peers.
                            chunk = response.raw.read1(65536, decode_content=False)
                            self._checkpoint()
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > maximum:
                                raise UpdateError('verification')
                            yield chunk
                        if length is not None and size != int(length):
                            raise UpdateError('verification')
                        return
                raise UpdateError('verification')
        except UpdateError as error:
            self.failure_code = error.code
            raise
        except Exception:
            _cancel(self.cancel)
            # HTTP 403/404 must remain visible to TUF's root-rotation workflow.
            # Requests connection failures inherit OSError: normalize here so
            # the outer cache boundary cannot misreport them as disk failures.
            import sys
            if isinstance(sys.exception(), _tuf_errors.DownloadHTTPError):
                raise
            self.failure_code = 'network'
            raise UpdateError('network') from None


def _profile_lock(root):
    # Same file is assembled as core.application_updates and imported directly by
    # source tests as maintained.core.application_updates.
    try:
        from utils.profile_lock import ProfileLock
    except ModuleNotFoundError:
        from maintained.utils.profile_lock import ProfileLock
    return ProfileLock(root)


class UpdateService:
    def __init__(self, config, cache_dir, installed):
        self.config = config
        self.cache_dir = Path(cache_dir).absolute()
        self.installed = installed
        self._mutex = threading.Lock()
        try:
            if (not isinstance(installed, InstalledRelease)
                    or not _NAME.fullmatch(installed.product_id)
                    or installed.architecture not in ('x86_64', 'arm64')
                    or not _integer(installed.build)
                    or not _integer(installed.profile_schema)
                    or not _integer(installed.updater_version)
                    or not _integer(installed.windows_build, 0)):
                raise ValueError
            _version(installed.version)
            # The first maintained updater was built on this date. Later builds
            # supply their generated APP_BUILD_DATE, without weakening this floor.
            floor = date(2026, 10, 18)
            if installed.build_date is not None:
                if not isinstance(installed.build_date, str):
                    raise ValueError
                supplied = date.fromisoformat(installed.build_date)
                if supplied.isoformat() != installed.build_date:
                    raise ValueError
                floor = max(floor, supplied)
            self._clock_floor = int(datetime.combine(floor, datetime.min.time(),
                                                    tzinfo=timezone.utc).timestamp())
        except (ValueError, TypeError):
            raise UpdateError('configuration') from None
        if config is not None:
            _config_valid(config)

    @property
    def configured(self):
        return self.config is not None

    @contextmanager
    def _operation(self, cancel):
        _cancel(cancel)
        if self.config is None:
            raise UpdateNotConfigured()
        if Updater is None:
            raise UpdateError('dependencies')
        if not self._mutex.acquire(blocking=False):
            raise UpdateError('busy')
        process_lock = None
        try:
            _check_path(self.cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            process_lock = _profile_lock(self.cache_dir)
            try:
                process_lock.acquire()
            except Exception as error:
                raise UpdateError('busy' if getattr(error, 'code', None) == 'busy'
                                  else 'storage') from None
            for name in ('metadata', 'downloads'):
                path = self.cache_dir / name
                _check_path(path)
                path.mkdir(exist_ok=True, mode=0o700)
            # A crashed process may leave a partial download. The process lock
            # proves no supported downloader still owns these generated files.
            for path in (self.cache_dir / 'downloads').glob('download-*.part'):
                _check_path(path)
                if path.is_file():
                    path.unlink()
            yield
        except UpdateError:
            raise
        except OSError:
            raise UpdateError('storage') from None
        finally:
            if process_lock is not None:
                process_lock.close()
            self._mutex.release()

    def _metadata_safety(self):
        directory = self.cache_dir / 'metadata'
        count = total = 0
        for path in directory.rglob('*'):
            if path == directory / 'root.json':
                # TUF 7 owns this convenience symlink/hardlink; bootstrap always
                # comes from the bundled bytes and never this link.
                continue
            _check_path(path)
            if path.is_dir():
                continue
            info = path.stat()
            count += 1
            total += info.st_size
            if (not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_METADATA
                    or count > 512 or total > 32 * _MAX_METADATA):
                raise UpdateError('storage')

    def _refresh(self, cancel, timeout=45.0):
        self._metadata_safety()
        clock_started = self._check_clock()
        fetcher = _BoundedFetcher(self.config, cancel, timeout)
        try:
            updater = Updater(
                str(self.cache_dir / 'metadata'), self.config.metadata_base_url,
                fetcher=fetcher, bootstrap=self.config.trusted_root,
                config=UpdaterConfig(max_root_rotations=32, max_delegations=8,
                                     root_max_length=512000, timestamp_max_length=16384,
                                     snapshot_max_length=_MAX_METADATA,
                                     targets_max_length=_MAX_METADATA))
            updater.refresh()
            _cancel(cancel)
            target_path = (f'channels/{self.config.channel}/'
                           f'{self.installed.architecture}/installer.exe')
            target = updater.get_targetinfo(target_path)
            fetcher._checkpoint()
            if target is None:
                self._record_clock(clock_started)
                return None, None
            offer = self._offer(target)
            self._high_water(offer)
            installed_version = _version(self.installed.version)
            proposed_version = _version(offer.version)
            if offer.build < self.installed.build or proposed_version < installed_version:
                raise UpdateError('rollback')
            if offer.build == self.installed.build and proposed_version == installed_version:
                self._record_clock(clock_started)
                return None, None
            if offer.build <= self.installed.build or proposed_version <= installed_version:
                raise UpdateError('rollback')
            self._record_clock(clock_started)
            return offer, target
        except UpdateError:
            raise
        except OSError:
            raise UpdateError('storage') from None
        except Exception as error:
            _cancel(cancel)
            if fetcher.failure_code:
                raise UpdateError(fetcher.failure_code) from None
            if isinstance(error, _tuf_errors.DownloadError):
                raise UpdateError('network') from None
            raise UpdateError('verification') from None

    def _check_clock(self):
        now = _utc_now()
        if not _integer(now, self._clock_floor, 253402300799):
            raise UpdateError('clock')
        path = self.cache_dir / 'clock-state.json'
        if path.exists():
            try:
                record = json.loads(_read_file(path, _MAX_POLICY), object_pairs_hook=_unique)
                if (not isinstance(record, dict) or set(record) != {'schema', 'verified_utc'}
                        or type(record['schema']) is not int or record['schema'] != 1
                        or not _integer(record['verified_utc'], 1, 253402300799)):
                    raise ValueError
                if now + 300 < record['verified_utc']:
                    raise UpdateError('clock')
            except (ValueError, TypeError, KeyError):
                raise UpdateError('storage') from None
        return now

    def _record_clock(self, started):
        now = self._check_clock()
        if now + 300 < started:
            raise UpdateError('clock')
        path = self.cache_dir / 'clock-state.json'
        old = 0
        if path.exists():
            old = json.loads(_read_file(path, _MAX_POLICY))['verified_utc']
        _atomic_write(path, json.dumps({'schema': 1,
                                       'verified_utc': max(old, started, now)}).encode())

    def _offer(self, target):
        try:
            custom = target.custom
            required = {'schema', 'product_id', 'channel', 'version', 'build', 'architecture',
                        'minimum_windows_build', 'maximum_windows_build',
                        'minimum_updater_version', 'profile_read_min', 'profile_read_max',
                        'profile_write_min', 'profile_write_max', 'installer_url',
                        'release_notes_url'}
            if (not isinstance(custom, dict) or set(custom) != {'cachecad'}
                    or not isinstance(custom['cachecad'], dict)
                    or set(custom['cachecad']) != required):
                raise ValueError
            values = dict(custom['cachecad'])
            if type(values.pop('schema')) is not int or custom['cachecad']['schema'] != 1:
                raise ValueError
            version = _version(values['version'])
            for key in ('build', 'minimum_windows_build', 'maximum_windows_build',
                        'minimum_updater_version', 'profile_read_min', 'profile_read_max',
                        'profile_write_min', 'profile_write_max'):
                if not _integer(values[key]):
                    raise ValueError
            if (not _integer(target.length, 1, self.config.max_installer_bytes)
                    or not isinstance(target.hashes.get('sha256'), str)
                    or not _HEX.fullmatch(target.hashes['sha256'])):
                raise ValueError
            _url(values['installer_url'])
            _url(values['release_notes_url'])
            if (not values['installer_url'].startswith(self.config.target_base_url)
                    or not urlsplit(values['installer_url']).path.endswith('.exe')):
                raise ValueError
            if (values['product_id'] != self.installed.product_id
                    or values['channel'] != self.config.channel
                    or values['architecture'] != self.installed.architecture
                    or (self.config.channel == 'stable' and not version[1])
                    or values['minimum_updater_version'] > self.installed.updater_version
                    or not values['minimum_windows_build'] <= self.installed.windows_build
                    <= values['maximum_windows_build']
                    or not values['profile_read_min'] <= self.installed.profile_schema
                    <= values['profile_read_max']
                    or not values['profile_write_min'] <= self.installed.profile_schema
                    <= values['profile_write_max']):
                raise UpdateError('incompatible')
            return ReleaseOffer(**values, length=target.length, sha256=target.hashes['sha256'],
                                target_path=target.path)
        except UpdateError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            raise UpdateError('verification') from None

    def _high_water(self, offer):
        path = self.cache_dir / 'release-state.json'
        record = {'schema': 1, 'product_id': offer.product_id, 'channel': offer.channel,
                  'architecture': offer.architecture, 'build': offer.build,
                  'version': offer.version, 'sha256': offer.sha256}
        if path.exists():
            try:
                old = json.loads(_read_file(path, _MAX_POLICY), object_pairs_hook=_unique)
                if (not isinstance(old, dict) or set(old) != set(record)
                        or type(old['schema']) is not int or old['schema'] != 1
                        or any(old[k] != record[k]
                               for k in ('product_id', 'channel', 'architecture'))
                        or not _integer(old['build']) or not isinstance(old['sha256'], str)
                        or not _HEX.fullmatch(old['sha256'])):
                    raise ValueError
                old_version = _version(old['version'])
                current_version = _version(offer.version)
                if (offer.build < old['build'] or current_version < old_version
                        or (offer.build == old['build'] and record != old)
                        or (offer.build > old['build'] and current_version <= old_version)):
                    raise UpdateError('rollback')
                if record == old:
                    return
            except (ValueError, TypeError, KeyError):
                raise UpdateError('storage') from None
        _atomic_write(path, json.dumps(record, sort_keys=True).encode())

    def check(self, cancel=None):
        """Refresh signed metadata; return a newer compatible offer or None."""
        with self._operation(cancel):
            offer, _ = self._refresh(cancel)
            _cancel(cancel)
            return offer

    def download(self, offer, cancel=None, progress=None):
        """Reauthenticate the offer, download exact bytes, atomically cache them.

        Progress receives (bytes_received, authenticated_total). The returned
        file MUST still pass publisher verification and locked execution checks.
        """
        with self._operation(cancel):
            current, target = self._refresh(cancel)
            if current is None or current != offer:
                raise UpdateError('changed')
            directory = self.cache_dir / 'downloads'
            destination = directory / f'installer-{offer.sha256}.exe'
            _check_path(destination)
            if destination.exists():
                try:
                    with destination.open('rb') as source:
                        info = os.fstat(source.fileno())
                        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                or info.st_size != offer.length):
                            raise UpdateError('verification')
                        target.verify_length_and_hashes(source)
                except (UpdateError, _tuf_errors.RepositoryError):
                    destination.unlink()
                    raise UpdateError('verification') from None
                _cancel(cancel)
                if progress:
                    progress(offer.length, offer.length)
                return VerifiedDownload(offer, destination)
            fetcher = _BoundedFetcher(self.config, cancel, 600.0)
            temporary = None
            try:
                descriptor, name = tempfile.mkstemp(prefix='download-', suffix='.part',
                                                    dir=directory)
                temporary = Path(name)
                received = 0
                with os.fdopen(descriptor, 'w+b') as output:
                    if progress:
                        progress(0, offer.length)
                    for chunk in fetcher.stream(offer.installer_url, offer.length):
                        _cancel(cancel)
                        output.write(chunk)
                        received += len(chunk)
                        if progress:
                            progress(received, offer.length)
                    _cancel(cancel)
                    output.flush()
                    output.seek(0)
                    target.verify_length_and_hashes(output)
                    os.fsync(output.fileno())
                _cancel(cancel)
                self._check_clock()
                _check_path(destination)
                os.replace(temporary, destination)
                temporary = None
                # Retain the newest download and at most two earlier verified
                # payloads. Only generated content-addressed filenames qualify.
                older = [p for p in directory.glob('installer-*.exe')
                         if p != destination and re.fullmatch(
                             r'installer-[0-9a-f]{64}\.exe', p.name)]
                for path in older:
                    _check_path(path)
                for path in sorted(older, key=lambda p: p.stat().st_mtime, reverse=True)[2:]:
                    path.unlink()
                return VerifiedDownload(offer, destination)
            except UpdateError:
                raise
            except OSError:
                raise UpdateError('storage') from None
            except Exception as error:
                _cancel(cancel)
                if isinstance(error, _tuf_errors.RepositoryError):
                    raise UpdateError('verification') from None
                raise UpdateError(fetcher.failure_code or 'network') from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)


def _atomic_write(path, raw):
    temporary = None
    try:
        _check_path(path)
        descriptor, name = tempfile.mkstemp(prefix='state-', suffix='.part', dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        _check_path(path)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
