"""Install, launch the actual installed application, assert isolation, then uninstall.

Windows-only opt-in integration test. The source-only test suite imports no Qt.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from support import (APP_ID, APP_NAME, EXE_NAME, sha256, validate_application_smoke,
                     verified_payload)


def snapshot(root):
    return {path.relative_to(root).as_posix(): sha256(path)
            for path in root.rglob('*') if path.is_file()}


def run_capture(command, report_dir, name, *, env=None, cwd=None, timeout=120):
    try:
        process = subprocess.run([str(arg) for arg in command], cwd=cwd, env=env,
                                 capture_output=True, text=True, encoding='utf-8',
                                 errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired as error:
        for suffix, content in (('stdout', error.stdout), ('stderr', error.stderr)):
            raw = content if isinstance(content, bytes) else (content or '').encode('utf-8')
            (report_dir / f'{name}.{suffix}.log').write_bytes(raw)
        raise RuntimeError(f'{name} timed out after {timeout} seconds') from error
    (report_dir / f'{name}.stdout.log').write_text(process.stdout, encoding='utf-8')
    (report_dir / f'{name}.stderr.log').write_text(process.stderr, encoding='utf-8')
    if process.returncode:
        raise RuntimeError(f'{name} returned {process.returncode}; see {name} logs')
    return process


def uninstall_key_exists():
    import winreg
    key = r'Software\Microsoft\Windows\CurrentVersion\Uninstall' + '\\' + APP_ID + '_is1'
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | view):
                return True
        except FileNotFoundError:
            pass
    return False


def installed_environment(profiles):
    environment = {key.upper(): value for key, value in os.environ.items()}
    for key in list(environment):
        if (key.startswith(('PYTHON', 'QT_', 'QML', 'GOOGLE', 'GCLOUD', 'CLOUDSDK',
                            'OAUTHLIB', 'REQUESTS_', 'CURL_', 'SSL_CERT', 'CACHECAD_')) or
                key in ('VIRTUAL_ENV', 'CONDA_PREFIX', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')):
            environment.pop(key)
    windows = Path(environment['SYSTEMROOT'])
    environment.update(PATH=os.pathsep.join((str(windows / 'System32'), str(windows))),
                       TMP=str(profiles), TEMP=str(profiles), TMPDIR=str(profiles),
                       HOME=str(profiles), USERPROFILE=str(profiles),
                       APPDATA=str(profiles / 'Roaming'), LOCALAPPDATA=str(profiles / 'Local'))
    return environment


def verify_shortcut(path, executable, arguments, report_dir, name, environment, cwd):
    """Read the installed .lnk without launching it or changing its target."""
    powershell = Path(environment['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = (
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:CACHECAD_TEST_SHORTCUT); "
        "@{target=$s.TargetPath; arguments=$s.Arguments; working_directory=$s.WorkingDirectory} "
        "| ConvertTo-Json -Compress"
    )
    process = run_capture([powershell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
                          report_dir, name, env=dict(environment, CACHECAD_TEST_SHORTCUT=str(path)),
                          cwd=cwd, timeout=30)
    result = json.loads(process.stdout.lstrip('\ufeff'))
    if (Path(result['target']).resolve() != executable.resolve() or
            result['arguments'] != arguments or
            Path(result['working_directory']).resolve() != executable.parent.resolve()):
        raise RuntimeError('Installed shortcut has unexpected target, arguments or working directory')
    return result


def test_installation(installer, report_dir):
    if uninstall_key_exists():
        raise RuntimeError('Existing CacheCAD Reconstructed installation detected; refusing to replace it')
    summary = {'schema_version': 1, 'started_utc': datetime.now(timezone.utc).isoformat(),
               'installer_sha256': sha256(installer), 'status': 'running', 'scenarios': [],
               'installed_runtime_verified': False, 'signed_out_startup_verified': False,
               'live_authentication_verified': False, 'uninstall_verified': False}
    summary_path = report_dir / 'installed-runtime.json'
    def save():
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    save()
    # Use a unique Start Menu group; never touch an existing user's installed app.
    group_name = 'CacheCAD Application CI ' + uuid.uuid4().hex
    start_group = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs' / group_name
    error = None
    # On failure keep diagnostics in report_dir, not inside this ephemeral workspace.
    with tempfile.TemporaryDirectory(prefix='cachecad-installer-test-') as temporary:
        root = Path(temporary).resolve()
        install_dir = root / 'installed app'
        profiles = root / 'disposable profiles'
        profiles.mkdir()
        environment = installed_environment(profiles)
        try:
            run_capture([installer, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                         '/NOCLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS', '/SP-',
                         f'/DIR={install_dir}', f'/GROUP={group_name}',
                         f'/LOG={report_dir / "install.log"}'], report_dir, 'install', cwd=root)
            executable = install_dir / EXE_NAME
            if not executable.is_file() or not uninstall_key_exists():
                raise RuntimeError('Per-user install is missing its executable or uninstall registration')
            shortcut = start_group / (APP_NAME + '.lnk')
            if not shortcut.is_file():
                raise RuntimeError('Per-user Start Menu shortcut was not installed')
            summary['shortcuts'] = {
                'application': verify_shortcut(shortcut, executable, '', report_dir,
                                                'shortcut-application', environment, root),
            }
            source = install_dir / '_internal/app-source'
            verified_payload(source)
            initial = snapshot(install_dir)
            summary['installed_executable_sha256'] = sha256(executable)
            summary['source_manifest_sha256'] = sha256(source / 'assembly-manifest.json')
            summary['start_menu_shortcut_verified'] = True
            summary['development_payload_absent'] = not (source / 'development').exists()
            if not summary['development_payload_absent']:
                raise RuntimeError('Development fixtures were shipped in the real application')
            for qt_platform in ('offscreen', 'windows'):
                name = f'application-startup-{qt_platform}'
                profile = profiles / name
                # A guaranteed-absent explicit path also tests genuine missing setup
                # when an owner-configured installer is checked locally.
                missing_config = root / 'not-configured/google-desktop-client.json'
                process = run_capture([executable, '--startup-check', '--profile-dir', profile,
                                       '--client-config', missing_config,
                                       '--platform', qt_platform], report_dir, name,
                                      env=environment, cwd=root, timeout=90)
                result = validate_application_smoke(process.stdout, qt_platform)
                if snapshot(install_dir) != initial:
                    raise RuntimeError('Installed files changed during signed-out startup')
                for pattern in ('*.dpapi', '*token*.json', '*credentials*.json', '*.pickle', '*.pkl'):
                    if list(profiles.rglob(pattern)):
                        raise RuntimeError('Signed-out startup created a credential file')
                record = {'scenario': 'signed-out-setup-required', 'platform': qt_platform,
                          'exit_code': process.returncode, 'result': result,
                          'installed_files_unchanged': True, 'disposable_profile': True}
                (report_dir / f'{name}.json').write_text(json.dumps(record, indent=2, sort_keys=True) + '\n', encoding='utf-8')
                summary['scenarios'].append(record)
                save()
            summary['signed_out_startup_verified'] = True
            summary['installed_runtime_verified'] = True
        except Exception as failure:
            error = failure
            summary['error'] = str(failure)
        finally:
            # Attempt uninstall even if startup or one of the lifecycle checks failed.
            uninstallers = list(install_dir.glob('unins*.exe'))
            try:
                if len(uninstallers) != 1:
                    raise RuntimeError('Cannot find exactly one installed uninstaller')
                run_capture([uninstallers[0], '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                             f'/LOG={report_dir / "uninstall.log"}'], report_dir, 'uninstall', cwd=root)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and (install_dir.exists() or start_group.exists() or uninstall_key_exists()):
                    time.sleep(0.2)
                if install_dir.exists() or start_group.exists() or uninstall_key_exists():
                    raise RuntimeError('Uninstall left install files, Start Menu group or per-user registry entry')
                summary['uninstall_verified'] = True
            except Exception as failure:
                summary['uninstall_error'] = str(failure)
                if error is None:
                    error = failure
            summary['status'] = 'passed' if error is None else 'failed'
            summary['finished_utc'] = datetime.now(timezone.utc).isoformat()
            save()
    if error is not None:
        raise error
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer', required=True, type=Path)
    parser.add_argument('--report-dir', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('The installer integration test must run on Windows')
    installer = args.installer.resolve(strict=True)
    reports = args.report_dir.resolve()
    reports.mkdir(parents=True, exist_ok=False)
    test_installation(installer, reports)
    print(f'Installed real application, verified offscreen/native signed-out startup and uninstall; reports: {reports}')


if __name__ == '__main__':
    main()
