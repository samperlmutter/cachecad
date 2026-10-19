"""Extract and launch the actual .app twice, with disposable state and no network."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import platform
import posixpath
import re
import stat
import subprocess
import sys
import tempfile
import zipfile

from macos_support import (APP_NAME, EXECUTABLE, audit_native_files, bundle_source,
                           isolated_environment, sha256, snapshot,
                           validate_application_smoke, verify_bundle_info)
from build import check_signature


SANDBOX_PROFILE = '(version 1)(allow default)(deny network*)'
STARTUP_PHASES = frozenset(('profile_ready', 'before_qapplication', 'after_qapplication',
    'before_window_show', 'after_window_show', 'before_event_loop', 'finish_check',
    'after_event_loop', 'result_written'))


def read_profile_json(profile, filename, limit):
    """Read only a fixed, bounded, non-linked CI report after process exit."""
    profile = Path(profile)
    path = profile / filename
    if profile.is_symlink() or path.is_symlink():
        raise ValueError('Linked startup report rejected')
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('Invalid startup report size or type')
    return json.loads(path.read_text(encoding='utf-8'))


def read_startup_result(stdout, profile, qt_platform):
    """GUI stdout can disconnect; the fixed final report must meet the same gates."""
    payload = read_profile_json(profile, 'startup-check-result.json', 65536)
    if payload is None and (Path(profile) / 'startup-check-result.json').exists():
        raise ValueError('Final startup report is not an object')
    has_stdout = any(line.startswith('CACHECAD_APPLICATION_RESULT=') for line in stdout.splitlines())
    from_stdout = validate_application_smoke(stdout, qt_platform) if has_stdout else None
    from_file = (validate_application_smoke('CACHECAD_APPLICATION_RESULT=' + json.dumps(payload), qt_platform)
                 if payload is not None else None)
    if from_stdout is not None and from_file is not None and from_stdout != from_file:
        raise ValueError('Startup stdout and final profile report disagree')
    result = from_file if from_file is not None else from_stdout
    if result is None:
        raise ValueError('No complete startup result on stdout or in the final profile report')
    return result, ('stdout-and-profile' if from_file is not None and from_stdout is not None
                    else 'profile' if from_file is not None else 'stdout')


def preserve_startup_evidence(profile, report_dir, name):
    """Retain only allowlisted stages and existing redacted crash reports."""
    evidence = {'phases': [], 'redacted_crash_reports': [], 'rejected_reports': 0}
    try:
        phases = read_profile_json(profile, 'startup-check-phases.json', 4096)
        if phases is not None:
            if (not isinstance(phases, list) or len(phases) > 32
                    or any(type(phase) is not str or phase not in STARTUP_PHASES for phase in phases)):
                raise ValueError('Invalid startup phase labels')
            evidence['phases'] = phases
    except (OSError, ValueError, UnicodeError):
        evidence['rejected_reports'] += 1
    diagnostics = Path(profile) / 'diagnostics'
    if diagnostics.is_dir() and not diagnostics.is_symlink():
        fields = {'schema', 'application', 'version', 'build_sha256', 'platform',
                  'timestamp_utc', 'kind', 'frames', 'breadcrumbs', 'exception_type'}
        for path in sorted(diagnostics.glob('crash-*.json'))[:8]:
            try:
                if not re.fullmatch(r'crash-[0-9a-f]{32}\.json', path.name):
                    raise ValueError('Invalid crash record filename')
                record = read_profile_json(diagnostics, path.name, 32768)
                if (not isinstance(record, dict) or set(record) - fields
                        or record.get('schema') != 1 or record.get('application') != 'CacheCAD'):
                    raise ValueError('Not an application redacted crash record')
                evidence['redacted_crash_reports'].append(record)
            except (OSError, ValueError, UnicodeError):
                evidence['rejected_reports'] += 1
    (report_dir / f'{name}.evidence.json').write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return evidence


def diagnose_cocoa(sandbox, report_dir, *, env, cwd):
    """Failure-only source Qt probe; never substitutes for the packaged app test."""
    phases = ('before_import', 'after_import', 'after_application', 'after_show',
              'quit_requested', 'after_exec')
    script = (
        "print('CACHECAD_QT_PROBE=before_import', flush=True)\n"
        "from PySide6.QtWidgets import QApplication, QWidget\n"
        "from PySide6.QtCore import QTimer\n"
        "print('CACHECAD_QT_PROBE=after_import', flush=True)\n"
        "app = QApplication(['cachecad-cocoa-ci-probe', '-platform', 'cocoa'])\n"
        "print('CACHECAD_QT_PROBE=after_application', flush=True)\n"
        "window = QWidget(); window.resize(240, 160); window.show()\n"
        "print('CACHECAD_QT_PROBE=after_show', flush=True)\n"
        "def stop():\n"
        "    print('CACHECAD_QT_PROBE=quit_requested', flush=True)\n"
        "    app.quit()\n"
        "QTimer.singleShot(200, stop)\n"
        "code = app.exec()\n"
        "print('CACHECAD_QT_PROBE=after_exec', flush=True)\n"
        "raise SystemExit(code)\n"
    )
    report = {'diagnostic_only': True, 'counts_as_native_app_pass': False,
              'same_network_denial': True, 'phases': []}
    try:
        process = subprocess.run([str(sandbox), '-p', SANDBOX_PROFILE, sys.executable, '-I', '-c', script],
                                 cwd=cwd, env=env, capture_output=True, text=True,
                                 encoding='utf-8', errors='replace', timeout=20)
        report.update(exit_code=process.returncode, stderr_bytes=len(process.stderr.encode('utf-8')))
        report['phases'] = [line.partition('=')[2] for line in process.stdout.splitlines()
                            if line.startswith('CACHECAD_QT_PROBE=') and line.partition('=')[2] in phases]
    except subprocess.TimeoutExpired:
        report['timed_out'] = True
    except OSError:
        report['probe_unavailable'] = True
    (report_dir / 'cocoa-source-probe.json').write_text(
        json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return report


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
                command = [sandbox, '-p', SANDBOX_PROFILE,
                           executable, '--startup-check', '--platform', qt_platform,
                           '--profile-dir', profile, '--client-config', root / 'absent-client.json']
                try:
                    process = run_capture(command, report_dir, name, env=environment, cwd=root)
                    result, result_transport = read_startup_result(process.stdout, profile, qt_platform)
                except Exception:
                    if qt_platform == 'cocoa':
                        diagnose_cocoa(sandbox, report_dir, env=environment, cwd=root)
                    raise
                finally:
                    preserve_startup_evidence(profile, report_dir, name)
                if result.get('trash_backend') != 'send2trash.mac.modern':
                    raise RuntimeError('Frozen app did not load the modern macOS Trash backend')
                if snapshot(bundle) != initial:
                    raise RuntimeError('The extracted application modified its own bundle')
                for pattern in ('*.dpapi', '*token*.json', '*credentials*.json', '*.pickle', '*.pkl'):
                    if list(home.rglob(pattern)):
                        raise RuntimeError('Signed-out startup created credential material')
                record = {'platform': qt_platform, 'exit_code': process.returncode,
                          'result': result, 'result_transport': result_transport, 'disposable_home': True,
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
