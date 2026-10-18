"""Private, separately frozen update helper. No command-line trust overrides.

The application stages this onefile executable outside the installation, verifies
its compiled identity and Authenticode publisher, then sends a bounded JSON ticket
over stdin. READY only means prepared. A distinct COMMIT is required before old
process exit can lead to installation. EOF or parent death before COMMIT aborts.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import sys
import uuid

from app.app_version import APP_BUILD_DATE, APP_PRODUCT_ID, APP_RELEASE_NUMBER, APP_VERSION
from core.application_updates import InstalledRelease, UpdateService, load_update_config
from core.windows_update_install import (
    HandoffLease, ParentProcess, SignerPolicy, UpdateInstallError, VerifiedInstaller,
    _require_non_elevated, _require_windows, _validated_path, launch_verified_installer,
)
from utils.profile_lock import ProfileLock


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateInstallError('handoff')
        result[key] = value
    return result


def _ticket(stream):
    raw = stream.readline(32770)
    if not raw.endswith(b'\n') or len(raw) > 32769:
        raise UpdateInstallError('handoff')
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    except (UnicodeError, ValueError, TypeError):
        raise UpdateInstallError('handoff') from None
    fields = {'schema', 'parent_pid', 'parent_creation_time', 'installer_path',
              'offer', 'cache_dir', 'profile'}
    if (not isinstance(result, dict) or set(result) != fields
            or type(result['schema']) is not int or result['schema'] != 1
            or type(result['parent_pid']) is not int or not 0 < result['parent_pid'] <= 0xFFFFFFFF
            or type(result['parent_creation_time']) is not int or result['parent_creation_time'] <= 0
            or not isinstance(result['offer'], dict)):
        raise UpdateInstallError('handoff')
    for field in ('installer_path', 'cache_dir', 'profile'):
        if not isinstance(result[field], str) or len(result[field]) > 4096:
            raise UpdateInstallError('handoff')
        result[field] = _validated_path(result[field])
    if (not result['installer_path'].is_relative_to(result['cache_dir'] / 'downloads')
            or result['installer_path'].suffix.lower() != '.exe'):
        raise UpdateInstallError('path')
    return result


def _write_result(profile, offer, status, exit_code=None):
    """Small local notice only; never log a ticket, account, path, or exception."""
    try:
        for parent in (profile, *profile.parents):
            info = parent.lstat()
            if not parent.is_dir() or getattr(info, 'st_file_attributes', 0) & 0x400:
                return
        with ProfileLock(profile):
            marker = profile / 'application-profile.json'
            if marker.is_symlink() or marker.stat().st_size > 1024:
                return
            if json.loads(marker.read_text(encoding='utf-8')) != {
                    'application': 'cachecad-reconstructed', 'schema': 1}:
                return
            data = profile / 'app_data'
            if (not data.is_dir() or data.is_symlink()
                    or getattr(data.lstat(), 'st_file_attributes', 0) & 0x400):
                return
            destination = data / 'update-result.json'
            if destination.is_symlink() or (destination.exists()
                    and getattr(destination.lstat(), 'st_file_attributes', 0) & 0x400):
                return
            value = {'schema': 1, 'status': status, 'version': offer.version,
                     'build': offer.build, 'installer_exit_code': exit_code,
                     'recorded_at': datetime.now(timezone.utc).isoformat()}
            temporary = data / ('update-result-' + uuid.uuid4().hex + '.tmp')
            try:
                with temporary.open('x', encoding='utf-8') as stream:
                    json.dump(value, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
    except Exception:
        # A notice failure is not permission to modify another profile or retry
        # installation, and must never conceal a successful install exit code.
        return


def _run(ticket, application_dir, input_stream, output_stream):
    _require_windows()
    _require_non_elevated()
    config = load_update_config(application_dir)
    if config is None:
        raise UpdateInstallError('policy')
    if platform.machine().lower() not in ('amd64', 'x86_64') or sys.maxsize <= 2**32:
        raise UpdateInstallError('platform')
    installed = InstalledRelease(APP_PRODUCT_ID, APP_VERSION, APP_RELEASE_NUMBER,
                                 windows_build=int(sys.getwindowsversion().build),
                                 build_date=APP_BUILD_DATE)
    policy = SignerPolicy(config.publisher_certificate_sha256)
    with ParentProcess(ticket['parent_pid'], ticket['parent_creation_time']) as parent:
        # The helper is not permitted to accept a local ticket as release trust.
        # Refresh from the compiled root and require the current authorized offer.
        offer = UpdateService(config, ticket['cache_dir'], installed).check()
        if offer is None or asdict(offer) != ticket['offer']:
            raise UpdateInstallError('integrity')
        with VerifiedInstaller.open(ticket['installer_path'], expected_sha256=offer.sha256,
                expected_length=offer.length, policy=policy) as installer:
            with HandoffLease() as handoff:
                output_stream.write(b'CACHECAD_UPDATE_READY\n')
                output_stream.flush()
                # Caller cancellation, abnormal parent exit, or closed stdin is
                # not COMMIT. No installer can start on those paths.
                if input_stream.readline(16) != b'COMMIT\n':
                    return 3
                try:
                    parent.wait_for_exit()
                except UpdateInstallError:
                    return 4  # Still running: do not write into its held profile.
                try:
                    exit_code = launch_verified_installer(installer, handoff)
                except UpdateInstallError:
                    _write_result(ticket['profile'], offer, 'installation_deferred')
                    return 5
                if exit_code == 0:
                    status = 'installation_finished'
                elif exit_code in (2, 5):
                    status = 'installation_cancelled'
                else:
                    status = 'installation_failed'
                _write_result(ticket['profile'], offer, status, exit_code)
                # No automatic relaunch or healthy-update assertion: those need
                # an authenticated payload identity and a first-launch protocol.
                return 0 if exit_code == 0 else 6


def main():
    try:
        _require_windows()
        if (not getattr(sys, 'frozen', False) or not hasattr(sys, '_MEIPASS')
                or len(sys.argv) != 1):
            raise UpdateInstallError('handoff')
        ticket = _ticket(sys.stdin.buffer)
        staged = _validated_path(sys.executable)
        if (staged.name != 'CacheCADUpdateHelper.exe'
                or not staged.parent.name.startswith('handoff-')
                or staged.parent.parent != ticket['cache_dir']):
            raise UpdateInstallError('path')
        return _run(ticket, Path(sys._MEIPASS) / 'application',
                    sys.stdin.buffer, sys.stdout.buffer)
    except Exception:
        # No raw errors, paths or signed metadata enter a pipe/log/UI.
        try:
            sys.stdout.buffer.write(b'CACHECAD_UPDATE_FAILED\n')
            sys.stdout.buffer.flush()
        except Exception:
            pass
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
