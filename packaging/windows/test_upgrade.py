"""Native Windows N -> N+1 installer, repair, downgrade and data-safety proof.

Only disposable directories, synthetic records, and real CurrentUser DPAPI are
used. Unsigned CI installers are invoked explicitly as test inputs; this never
changes the application's fail-closed production update trust policy.
"""
import argparse
import ast
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from support import APP_NAME, EXE_NAME, HELPER_NAME, pe_file_version, sha256, verified_payload
from test_installer import installed_environment, run_capture, snapshot, uninstall_key_exists
from support import validate_application_smoke

MUTEX_PREFIX = r'Global\CacheCADReconstructed.D24539B1-3941-4273-9C44-A62A05A91623.'
IDENTITY_KEY = r'Software\CacheCADReconstructed\Installation'


def read_install_identity():
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, IDENTITY_KEY, 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        return {name: winreg.QueryValueEx(key, name)[0]
                for name in ('Version', 'WindowsVersion', 'ReleaseNumber', 'PayloadIdentity', 'InstallPath')}


def installer_run(installer, install_dir, group, reports, name, *, expect_failure=None):
    log = reports / (name + '.inno.log')
    command = [str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
               '/NOCLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS', '/SP-',
               '/DIR=' + str(install_dir), '/GROUP=' + group, '/LOG=' + str(log)]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=180)
    (reports / (name + '.stdout.log')).write_text(result.stdout, encoding='utf-8')
    (reports / (name + '.stderr.log')).write_text(result.stderr, encoding='utf-8')
    if expect_failure:
        if result.returncode == 0 or expect_failure not in log.read_text(encoding='utf-8-sig', errors='replace'):
            raise RuntimeError(name + ' did not refuse for the expected reason')
    elif result.returncode != 0:
        raise RuntimeError(name + ' failed; inspect its Inno log')
    return result.returncode


def installed_identity(install_dir):
    source = install_dir / '_internal/app-source'
    verified_payload(source)
    identity = json.loads((source / 'assembly-manifest.json').read_text(encoding='utf-8'))['release_identity']
    expected = tuple(map(int, identity['windows_version'].split('.')))
    if any(pe_file_version(install_dir / filename) != expected for filename in (EXE_NAME, HELPER_NAME)):
        raise RuntimeError('Installed application/helper PE identity does not match source identity')
    registry = read_install_identity()
    if (registry['Version'] != identity['version'] or registry['WindowsVersion'] != identity['windows_version']
            or registry['ReleaseNumber'] != identity['release_number']
            or Path(registry['InstallPath']).resolve() != install_dir.resolve()):
        raise RuntimeError('Installer registration and application identity disagree')
    return identity


CRASH_REPORT_LIMIT = 8
CRASH_BYTES_LIMIT = 32768
CRASH_KINDS = frozenset(('unhandled-main', 'unhandled-thread', 'qt-callback',
                        'startup-failure', 'shutdown-timeout', 'native-fatal'))
CRASH_EVENTS = frozenset(('bootstrap_verified', 'profile_ready', 'native_capture_ready',
    'application_configured', 'qt_started', 'window_shown', 'shutdown_started',
    'shutdown_finished', 'shutdown_timeout', 'diagnostic_test'))


def reviewed_crash_labels(install_dir):
    """Parse verified source for safe labels; never execute installed modules."""
    labels = {'<external>': {'<redacted>'}}
    for name, path in verified_payload(install_dir / '_internal/app-source').items():
        if not name.endswith('.py') or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}\.py', path.name):
            continue
        functions = labels.setdefault(path.name, {'<module>', '<redacted>'})
        for node in ast.walk(ast.parse(path.read_bytes())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.add(node.name)
            elif isinstance(node, ast.Lambda):
                functions.add('<lambda>')
    return labels


def validated_crash_record(record, expected, build_hash, labels):
    """Accept only the existing privacy-safe crash schema and reviewed labels."""
    required = {'schema', 'application', 'version', 'build_sha256', 'platform',
                'timestamp_utc', 'kind', 'frames', 'breadcrumbs'}
    if (type(record) is not dict or not required <= record.keys()
            or record.keys() - required - {'exception_type'}
            or type(record['schema']) is not int or record['schema'] != 1
            or record['application'] != 'CacheCAD' or record['version'] != expected['version']
            or record['build_sha256'] != build_hash or record['kind'] not in CRASH_KINDS):
        raise ValueError('Crash metadata is outside the approved schema')
    platform = record['platform']
    if (type(platform) is not dict or set(platform) != {'os', 'python', 'bits'}
            or platform['os'] != 'windows' or type(platform['bits']) is not int
            or platform['bits'] != 64 or type(platform['python']) is not str
            or not re.fullmatch(r'[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,3}', platform['python'])):
        raise ValueError('Crash platform is invalid')
    timestamp = record['timestamp_utc']
    if (type(timestamp) is not str or not re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\+00:00', timestamp)):
        raise ValueError('Crash timestamp is invalid')
    datetime.fromisoformat(timestamp)
    errors = {name for name, value in vars(__import__('builtins')).items()
              if isinstance(value, type) and issubclass(value, BaseException)} | {'ApplicationError'}
    if 'exception_type' in record and record['exception_type'] not in errors:
        raise ValueError('Crash exception type is not allowlisted')
    frames, crumbs = record['frames'], record['breadcrumbs']
    if type(frames) is not list or len(frames) > 64 or type(crumbs) is not list or len(crumbs) > 16:
        raise ValueError('Crash arrays exceed their bounds')
    for frame in frames:
        if (type(frame) is not dict or set(frame) != {'source', 'function', 'line'}
                or type(frame['source']) is not str or frame['source'] not in labels
                or frame['function'] not in labels[frame['source']]
                or type(frame['line']) is not int or not 0 <= frame['line'] <= 999999999):
            raise ValueError('Crash frame is not a reviewed source label')
    if any(type(crumb) is not str or crumb not in CRASH_EVENTS for crumb in crumbs):
        raise ValueError('Crash breadcrumb is not allowlisted')
    return record


def collect_startup_crashes(profile, expected, build_hash, labels):
    """Export only bounded validated JSON, never profile/settings/session files."""
    result = {'status': 'absent', 'reports': [], 'rejected_count': 0, 'scan_limited': False}
    directory = profile / 'diagnostics'
    try:
        for path in (directory, *directory.parents):
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                result['status'] = 'linked-directory-refused'
                return result
        if not directory.exists():
            return result
        result['status'] = 'collected'
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= 64:
                    result['scan_limited'] = True
                    break
                if not re.fullmatch(r'crash-[0-9a-f]{32}\.json', entry.name):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    path = Path(entry.path)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())
                            or info.st_size > CRASH_BYTES_LIMIT or len(result['reports']) >= CRASH_REPORT_LIMIT):
                        raise ValueError('Unsafe or oversized crash report')
                    with path.open('rb') as stream:
                        raw = stream.read(CRASH_BYTES_LIMIT + 1)
                    if len(raw) > CRASH_BYTES_LIMIT:
                        raise ValueError('Crash report grew past its bound')
                    record = validated_crash_record(json.loads(raw), expected, build_hash, labels)
                    result['reports'].append(record)
                except (OSError, ValueError, TypeError, KeyError):
                    result['rejected_count'] += 1
    except OSError:
        result['status'] = 'unreadable'
    return result


def startup_gate_snapshot():
    """Observe object presence only; never create, acquire or release a gate."""
    if sys.platform != 'win32':
        return {'status': 'not-windows'}
    kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True, winmode=0x00000800)
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    result = {}
    for suffix in ('Install', 'Running', 'Handoff'):
        ctypes.set_last_error(0)
        handle = kernel.OpenMutexW(0x00100000, False, MUTEX_PREFIX + suffix)
        error = ctypes.get_last_error()
        if handle:
            kernel.CloseHandle(handle)
            result[suffix] = {'presence': 'present'}
        else:
            result[suffix] = {'presence': 'absent' if error == 2 else 'unknown', 'win32_error': error}
    return result


def native_startup(install_dir, root, environment, reports, name, expected, *, preserved_profile=None):
    profile = preserved_profile or root / 'startup-profiles' / name
    command = [install_dir / EXE_NAME, '--startup-check', '--platform', 'windows',
               '--profile-dir', profile, '--client-config', root / 'missing-client.json']
    if preserved_profile is not None:
        command.append('--preserved-profile-check')
        environment = dict(environment, CACHECAD_ENABLE_UPGRADE_TESTS='1')
    evidence = {'schema_version': 1, 'scenario': name, 'status': 'failed', 'stage': 'source-verification',
                'started_utc': datetime.now(timezone.utc).isoformat(), 'launch_attempts': 0,
                'expected_identity': expected, 'preserved_profile': preserved_profile is not None}
    started = time.monotonic()
    labels, build_hash = {'<external>': {'<redacted>'}}, ''
    try:
        labels = reviewed_crash_labels(install_dir)
        build_hash = sha256(install_dir / '_internal/app-source/assembly-manifest.json')
        evidence['gates_before'] = startup_gate_snapshot()
        evidence['stage'] = 'process'
        evidence['launch_attempts'] = 1
        try:
            process = subprocess.run([str(arg) for arg in command], env=environment, cwd=root,
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
        except subprocess.TimeoutExpired as failure:
            evidence['timed_out'] = True
            for suffix, content in (('stdout', failure.stdout), ('stderr', failure.stderr)):
                raw = content if isinstance(content, bytes) else (content or '').encode('utf-8')
                (reports / f'{name}.{suffix}.log').write_bytes(raw)
            raise RuntimeError(name + ' timed out after 90 seconds') from failure
        evidence['exit_code'] = process.returncode
        for suffix, content in (('stdout', process.stdout), ('stderr', process.stderr)):
            (reports / f'{name}.{suffix}.log').write_text(content, encoding='utf-8')
        if process.returncode:
            raise RuntimeError(f'{name} returned {process.returncode}; see {name} logs')
        evidence['stage'] = 'smoke-validation'
        record = validate_application_smoke(process.stdout, 'windows')
        if preserved_profile is not None and record.get('preserved_profile_unchanged') is not True:
            raise RuntimeError('Preserved-profile startup did not confirm unchanged bytes')
        for field in ('version', 'release_number', 'windows_version', 'product_id'):
            if record.get(field) != expected[field]:
                raise RuntimeError('Installed startup identity differs: ' + field)
        evidence.update(status='passed', stage='complete', result=record)
        return record
    finally:
        evidence['gates_after'] = startup_gate_snapshot()
        evidence['crash_diagnostics'] = collect_startup_crashes(profile, expected, build_hash, labels)
        evidence['profile_state'] = {'directory_exists': profile.is_dir(),
            'marker_exists': (profile / 'application-profile.json').is_file(),
            'lock_exists': (profile / '.cachecad-profile.lock').is_file()}
        evidence['duration_seconds'] = round(time.monotonic() - started, 3)
        evidence['finished_utc'] = datetime.now(timezone.utc).isoformat()
        (reports / (name + '.json')).write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def token_store_module(install_dir, suffix):
    path = install_dir / '_internal/app-source/src/utils/windows_token_store.py'
    spec = importlib.util.spec_from_file_location('_installed_token_store_' + suffix, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_preserved_data(root, environment, install_dir):
    default = Path(environment['LOCALAPPDATA']) / 'CacheCADReconstructed/v1'
    custom = Path(environment['TEMP']) / 'custom profile'
    project = root / 'user CAD project'
    project.mkdir()
    (project / 'assembly.step').write_bytes(b'ISO-10303-21;\nSYNTHETIC CAD FIXTURE ONLY\nEND-ISO-10303-21;\n')
    recovery = project / '.cachecad/recovery/synthetic-operation'
    recovery.mkdir(parents=True)
    (recovery / 'original.step').write_bytes(b'SYNTHETIC LOCAL RECOVERY BYTES\x00\xff')
    (recovery / 'journal.json').write_text('{"schema":1,"synthetic":true,"state":"preserved"}\n', encoding='utf-8')
    token_record = {'schema': 1, 'client_id': 'synthetic-ci.apps.googleusercontent.com',
                    'refresh_token': 'SYNTHETIC-NONFUNCTIONAL-REFRESH-TOKEN',
                    'scopes': ['https://www.googleapis.com/auth/drive.file']}
    store = token_store_module(install_dir, 'old')
    for profile in (default, custom):
        data = profile / 'app_data'
        data.mkdir(parents=True)
        (profile / 'application-profile.json').write_text(
            '{"application":"cachecad-reconstructed","schema":1}\n', encoding='utf-8')
        (profile / '.cachecad-upgrade-fixture.json').write_text(
            '{"fixture":"cachecad-upgrade-ci","schema":1}\n', encoding='utf-8')
        (profile / '.cachecad-profile.lock').write_bytes(b'\x00')
        settings = {'working_directory': str(project.parent), 'use_tray_icon': False,
                    'keep_revision_forever': False, 'default_drive_folder': '',
                    'last_opened_project': None, 'usage': False}
        (data / 'settings.json').write_text(json.dumps(settings, indent=2) + '\n', encoding='utf-8')
        (data / 'projects.csv').write_text('user CAD project,synthetic-remote-id,False\n', encoding='utf-8')
        protected = store.WindowsTokenStore(data / 'google-session.dpapi')
        protected.save(token_record)
        if protected.load() != token_record:
            raise RuntimeError('Synthetic CurrentUser DPAPI round-trip failed')
    return (default, custom, project), token_record


def assert_preserved(roots, expected, install_dir, token_record, name):
    for root, initial in zip(roots, expected):
        if snapshot(root) != initial:
            raise RuntimeError(name + ' changed preserved profile, recovery or CAD bytes')
    module = token_store_module(install_dir, name)
    for profile in roots[:2]:
        if module.WindowsTokenStore(profile / 'app_data/google-session.dpapi').load() != token_record:
            raise RuntimeError(name + ' changed same-user DPAPI decryptability')


def held_mutex(suffix, owned=False):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, owned, MUTEX_PREFIX + suffix)
    if not handle or ctypes.get_last_error() == 183:
        if handle:
            kernel.CloseHandle(handle)
        raise RuntimeError('Cannot safely create unique CI exclusion mutex')
    return kernel, handle


def test_upgrade(older, newer, reports):
    if uninstall_key_exists():
        raise RuntimeError('Existing installation detected; refusing to run upgrade fixtures')
    summary = {'schema_version': 1, 'status': 'running', 'code_signed': False,
               'production_updater_trust_bypassed': False,
               'baseline_is_synthetic_prior_version_from_current_source': True,
               'upgrade_destination_is_exact_deliverable': True,
               'started_utc': datetime.now(timezone.utc).isoformat(),
               'older_installer_sha256': sha256(older), 'newer_installer_sha256': sha256(newer),
               'scenarios': []}
    def save():
        (reports / 'upgrade-runtime.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    save()
    group = 'CacheCAD Upgrade CI ' + uuid.uuid4().hex
    start_group = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs' / group
    error = None
    with tempfile.TemporaryDirectory(prefix='cachecad-upgrade-test-') as temporary:
        root = Path(temporary).resolve()
        install_dir = root / 'installed app'
        profiles = root / 'disposable data'
        profiles.mkdir()
        environment = installed_environment(profiles)
        try:
            installer_run(older, install_dir, group, reports, 'install-N')
            old = installed_identity(install_dir)
            old_start = native_startup(install_dir, root, environment, reports, 'startup-N', old)
            roots, token_record = seed_preserved_data(root, environment, install_dir)
            preserved = [snapshot(path) for path in roots]
            summary['older_identity'] = old
            summary['scenarios'].append({'name': 'install-N', 'startup': old_start})
            old_files = snapshot(install_dir)
            for suffix, owned in (('Running', False), ('Install', True)):
                kernel, handle = held_mutex(suffix, owned)
                try:
                    exit_code = installer_run(newer, install_dir, group, reports,
                        'blocked-' + suffix.lower(), expect_failure='CACHECAD_GATE_REFUSED')
                    if snapshot(install_dir) != old_files:
                        raise RuntimeError('Blocked installer changed application bytes')
                    summary['scenarios'].append({'name': 'blocked-' + suffix.lower(), 'exit_code': exit_code,
                                                 'installed_bytes_unchanged': True})
                finally:
                    if owned:
                        kernel.ReleaseMutex(handle)
                    kernel.CloseHandle(handle)
            nested_profile_marker = install_dir / '_internal/app-source/application-profile.json'
            nested_profile_marker.write_text('{"application":"cachecad-reconstructed","schema":1}', encoding='utf-8')
            before_refusal = snapshot(install_dir)
            try:
                marker_exit = installer_run(newer, install_dir, group, reports, 'blocked-nested-profile',
                                            expect_failure='CACHECAD_SOURCE_REFUSED')
                if snapshot(install_dir) != before_refusal:
                    raise RuntimeError('Refused nested-profile cleanup modified installed bytes')
                summary['scenarios'].append({'name': 'blocked-nested-profile', 'exit_code': marker_exit,
                                             'installed_bytes_unchanged': True})
            finally:
                nested_profile_marker.unlink()
            obsolete = install_dir / '_internal/app-source/src/core/obsolete_upgrade_fixture.py'
            obsolete.write_text('# Synthetic old module that is absent from the new payload.\n', encoding='utf-8')
            installer_run(newer, install_dir, group, reports, 'upgrade-N-to-N-plus-1')
            if obsolete.exists():
                raise RuntimeError('Upgrade left an obsolete source module behind')
            summary['obsolete_source_removed'] = True
            new = installed_identity(install_dir)
            if (new['release_number'] <= old['release_number'] or
                    tuple(map(int, new['version'].split('.'))) <= tuple(map(int, old['version'].split('.')))):
                raise RuntimeError('Upgrade test inputs are not increasing releases')
            summary['newer_identity'] = new
            save()
            assert_preserved(roots, preserved, install_dir, token_record, 'upgrade')
            new_start = native_startup(install_dir, root, environment, reports, 'startup-N-plus-1', new)
            assert_preserved(roots, preserved, install_dir, token_record, 'startup')
            summary['preserved_profile_startups'] = [
                native_startup(install_dir, root, environment, reports, 'preserved-' + label, new,
                               preserved_profile=profile)
                for label, profile in zip(('default', 'custom'), roots[:2])]
            assert_preserved(roots, preserved, install_dir, token_record, 'preserved-profile-startup')
            summary['newer_identity'] = new
            summary['scenarios'].append({'name': 'upgrade-N-to-N-plus-1', 'startup': new_start,
                                        'preserved_bytes': True, 'dpapi_decrypts': True})
            installer_run(newer, install_dir, group, reports, 'same-version-repair')
            if installed_identity(install_dir) != new:
                raise RuntimeError('Repair changed installed release identity')
            assert_preserved(roots, preserved, install_dir, token_record, 'repair')
            repaired_files, repaired_registration = snapshot(install_dir), read_install_identity()
            # Simulate an installed release with conflicting approved payload identity.
            # This touches only the disposable installation created above.
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, IDENTITY_KEY, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
                winreg.SetValueEx(key, 'PayloadIdentity', 0, winreg.REG_SZ, '0' * 64)
            try:
                repair_exit = installer_run(newer, install_dir, group, reports, 'blocked-conflicting-repair',
                                           expect_failure='CACHECAD_REPAIR_REFUSED')
                if snapshot(install_dir) != repaired_files:
                    raise RuntimeError('Rejected conflicting repair modified installed bytes')
                summary['scenarios'].append({'name': 'blocked-conflicting-repair',
                                             'exit_code': repair_exit, 'installed_bytes_unchanged': True})
            finally:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, IDENTITY_KEY, 0,
                                    winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
                    winreg.SetValueEx(key, 'PayloadIdentity', 0, winreg.REG_SZ,
                                      repaired_registration['PayloadIdentity'])
            exit_code = installer_run(older, install_dir, group, reports, 'blocked-downgrade',
                                      expect_failure='CACHECAD_DOWNGRADE_REFUSED')
            if snapshot(install_dir) != repaired_files or read_install_identity() != repaired_registration:
                raise RuntimeError('Rejected downgrade modified installed files or identity')
            assert_preserved(roots, preserved, install_dir, token_record, 'downgrade')
            final_start = native_startup(install_dir, root, environment, reports, 'startup-after-repair-and-refusal', new)
            summary['scenarios'] += [{'name': 'same-version-repair', 'preserved_bytes': True},
                                     {'name': 'blocked-downgrade', 'exit_code': exit_code,
                                      'installed_bytes_unchanged': True, 'startup': final_start}]
            summary['preservation'] = {'settings': True, 'project_index': True,
                'profile_marker': True, 'encrypted_session_bytes': True, 'same_user_dpapi_decrypts': True,
                'project_local_recovery': True, 'user_cad_bytes': True,
                'default_profile_path': True, 'explicit_custom_profile': True,
                'fixture_file_count': sum(len(item) for item in preserved)}
            save()
        except Exception as failure:
            error = failure
            summary['error'] = str(failure)
        finally:
            try:
                uninstallers = list(install_dir.glob('unins*.exe'))
                if len(uninstallers) != 1:
                    raise RuntimeError('Cannot locate exactly one test uninstaller')
                run_capture([uninstallers[0], '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                             '/LOG=' + str(reports / 'uninstall.inno.log')], reports, 'uninstall', cwd=root)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and (install_dir.exists() or start_group.exists() or uninstall_key_exists()):
                    time.sleep(0.2)
                if install_dir.exists() or start_group.exists() or uninstall_key_exists():
                    raise RuntimeError('Upgrade-test uninstall did not finish cleanly')
                if 'preserved' in locals():
                    for path, initial in zip(roots, preserved):
                        if snapshot(path) != initial:
                            raise RuntimeError('Uninstall changed profile or project bytes')
                summary['uninstall_verified'] = True
            except Exception as failure:
                summary['uninstall_error'] = str(failure)
                if error is None:
                    error = failure
            summary['status'] = 'passed' if error is None else 'failed'
            summary['finished_utc'] = datetime.now(timezone.utc).isoformat()
            save()
    if error:
        raise error
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--older-installer', type=Path, required=True)
    parser.add_argument('--newer-installer', type=Path, required=True)
    parser.add_argument('--report-dir', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Upgrade integration requires native Windows')
    reports = args.report_dir.resolve()
    reports.mkdir(parents=True, exist_ok=False)
    test_upgrade(args.older_installer.resolve(strict=True), args.newer_installer.resolve(strict=True), reports)
    print('Native unsigned N -> N+1 upgrade, exact repair, downgrade refusal and data preservation passed.')


if __name__ == '__main__':
    main()
