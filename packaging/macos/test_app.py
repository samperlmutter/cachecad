"""Extract and launch the actual .app twice, with disposable state and no network."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import platform
import posixpath
import stat
import subprocess
import sys
import tempfile
import zipfile

from macos_support import (APP_NAME, EXECUTABLE, audit_native_files, bundle_source,
                           isolated_environment, sha256, snapshot,
                           validate_application_smoke, verify_bundle_info)
from build import check_signature


def validate_archive(path):
    """Check member paths and symlink destinations before invoking ditto."""
    seen = set()
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = info.filename
            relative = PurePosixPath(name)
            if (not relative.parts or relative.is_absolute() or '..' in relative.parts
                    or '\\' in name or ':' in name or name in seen
                    or relative.parts[0] not in (APP_NAME, '__MACOSX')):
                raise ValueError('Unsafe or duplicate application ZIP member')
            seen.add(name)
            if stat.S_ISLNK(info.external_attr >> 16):
                target = archive.read(info).decode('utf-8')
                resolved = posixpath.normpath(posixpath.join(str(relative.parent), target))
                if (not target or target.startswith('/') or '\\' in target
                        or PurePosixPath(resolved).parts[0] != relative.parts[0]):
                    raise ValueError('Application ZIP symlink escapes its bundle')
    if f'{APP_NAME}/{EXECUTABLE}' not in seen:
        raise ValueError('ZIP is missing the actual application executable')


def run_capture(command, report_dir, name, *, env, cwd, timeout=90):
    try:
        process = subprocess.run([str(item) for item in command], cwd=cwd, env=env,
                                 capture_output=True, text=True, encoding='utf-8',
                                 errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired as error:
        for suffix, value in (('stdout', error.stdout), ('stderr', error.stderr)):
            raw = value if isinstance(value, bytes) else (value or '').encode('utf-8')
            (report_dir / f'{name}.{suffix}.log').write_bytes(raw)
        raise RuntimeError(f'{name} did not exit within {timeout} seconds') from error
    for suffix, value in (('stdout', process.stdout), ('stderr', process.stderr)):
        (report_dir / f'{name}.{suffix}.log').write_text(value, encoding='utf-8')
    if process.returncode:
        print(process.stdout[-12000:] + process.stderr[-12000:], file=sys.stderr)
        raise RuntimeError(f'{name} returned {process.returncode}; see test logs')
    return process


def test_application(archive, report_dir):
    summary = {'schema_version': 1, 'started_utc': datetime.now(timezone.utc).isoformat(),
               'archive_sha256': sha256(archive), 'status': 'running', 'scenarios': [],
               'native_runtime_verified': False, 'live_authentication_verified': False,
               'gatekeeper_download_acceptance_verified': False}
    summary_path = report_dir / 'macos-runtime.json'
    def save():
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    save()
    try:
        validate_archive(archive)
        # /var and /tmp are aliases on macOS. The app intentionally refuses links.
        temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix='cachecad-macos-test-', dir=temporary_root) as temporary:
            root = Path(temporary).resolve(strict=True)
            home = root / 'disposable home'
            home.mkdir(mode=0o700)
            environment = isolated_environment(home)
            extracted = root / 'extracted application'
            run_capture(['/usr/bin/ditto', '-x', '-k', archive, extracted], report_dir,
                        'extract', env=environment, cwd=root)
            bundle = extracted / APP_NAME
            identity = json.loads((bundle_source(bundle) / 'assembly-manifest.json').read_text())['release_identity']
            summary['bundle_info'] = verify_bundle_info(bundle, identity)
            summary['native_binaries'] = audit_native_files(bundle)
            summary['signature'] = check_signature(bundle, report_dir / 'codesign-verify.log')
            executable = bundle / EXECUTABLE
            initial = snapshot(bundle)
            sandbox = Path('/usr/bin/sandbox-exec')
            if not sandbox.is_file():
                raise RuntimeError('The macOS network-denial sandbox is unavailable; do not run an unguarded smoke test')
            for qt_platform in ('offscreen', 'cocoa'):
                name = 'application-startup-' + qt_platform
                profile = home / name
                # deny network* applies at the OS boundary as well as the app's
                # Python socket/browser/process tripwires. No signing/trust edit.
                command = [sandbox, '-p', '(version 1)(allow default)(deny network*)',
                           executable, '--startup-check', '--platform', qt_platform,
                           '--profile-dir', profile, '--client-config', root / 'absent-client.json']
                process = run_capture(command, report_dir, name, env=environment, cwd=root)
                result = validate_application_smoke(process.stdout, qt_platform)
                if result.get('trash_backend') != 'send2trash.mac.modern':
                    raise RuntimeError('Frozen app did not load the modern macOS Trash backend')
                if snapshot(bundle) != initial:
                    raise RuntimeError('The extracted application modified its own bundle')
                for pattern in ('*.dpapi', '*token*.json', '*credentials*.json', '*.pickle', '*.pkl'):
                    if list(home.rglob(pattern)):
                        raise RuntimeError('Signed-out startup created credential material')
                record = {'platform': qt_platform, 'exit_code': process.returncode,
                          'result': result, 'disposable_home': True,
                          'os_network_denied': True, 'clean_exit': True,
                          'bundle_unchanged': True}
                summary['scenarios'].append(record)
                (report_dir / f'{name}.json').write_text(json.dumps(record, indent=2, sort_keys=True) + '\n', encoding='utf-8')
                save()
            summary.update(status='passed', native_runtime_verified=True,
                           signed_out_startup_verified=True, clean_exit_verified=True,
                           extracted_archive_verified=True, profile_cleanup_verified=False)
        summary['profile_cleanup_verified'] = not root.exists()
        if not summary['profile_cleanup_verified']:
            raise RuntimeError('Disposable profile was not removed')
    except Exception as error:
        summary.update(status='failed', error=str(error))
        raise
    finally:
        summary['finished_utc'] = datetime.now(timezone.utc).isoformat()
        save()
    print('Native arm64 .app passed offscreen and Cocoa startup, clean exit, and ZIP extraction checks')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--report-dir', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'darwin' or platform.machine().lower() != 'arm64':
        parser.error('Native Apple Silicon macOS is required; this is not a cross-platform execution claim')
    report_dir = args.report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=False)
    test_application(args.archive.resolve(strict=True), report_dir)


if __name__ == '__main__':
    main()
