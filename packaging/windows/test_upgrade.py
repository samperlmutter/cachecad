"""Native Windows N -> N+1 installer, repair, downgrade and data-safety proof.

Only disposable directories, synthetic records, and real CurrentUser DPAPI are
used. Unsigned CI installers are invoked explicitly as test inputs; this never
changes the application's fail-closed production update trust policy.
"""
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
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


def native_startup(install_dir, root, environment, reports, name, expected, *, preserved_profile=None):
    profile = preserved_profile or root / 'startup-profiles' / name
    command = [install_dir / EXE_NAME, '--startup-check', '--platform', 'windows',
               '--profile-dir', profile, '--client-config', root / 'missing-client.json']
    if preserved_profile is not None:
        command.append('--preserved-profile-check')
        environment = dict(environment, CACHECAD_ENABLE_UPGRADE_TESTS='1')
    result = run_capture(command,
                         reports, name, env=environment, cwd=root, timeout=90)
    record = validate_application_smoke(result.stdout, 'windows')
    if preserved_profile is not None and record.get('preserved_profile_unchanged') is not True:
        raise RuntimeError('Preserved-profile startup did not confirm unchanged bytes')
    for field in ('version', 'release_number', 'windows_version', 'product_id'):
        if record.get(field) != expected[field]:
            raise RuntimeError('Installed startup identity differs: ' + field)
    return record


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
