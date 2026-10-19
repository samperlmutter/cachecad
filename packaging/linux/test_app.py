"""Extract and test the actual Linux application using disposable signed-out state."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time

from linux_support import (EXECUTABLE, audit_native_files, bundle_source, extract_archive,
                           isolated_environment, minimum_glibc, sha256, snapshot,
                           validate_application_smoke)

INTERACTION_CHECKS = ['settings_repeated_clicks_and_back', 'missing_configuration_rejected',
                      'directory_modal_escape_and_navigation', 'ordinary_close_and_reopen',
                      'explicit_exit_repeated']


def run_capture(command, report_dir, name, *, env, cwd, expected=0, timeout=90):
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
    if expected is not None and process.returncode != expected:
        print(process.stdout[-12000:] + process.stderr[-12000:], file=sys.stderr)
        raise RuntimeError(f'{name} returned {process.returncode}, expected {expected}; see test logs')
    return process


def read_startup_result(stdout, profile, qt_platform):
    result = validate_application_smoke(stdout, qt_platform)
    path = Path(profile) / 'startup-check-result.json'
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ValueError('Invalid final startup report')
        saved = validate_application_smoke('CACHECAD_APPLICATION_RESULT=' + path.read_text(), qt_platform)
        if saved != result:
            raise ValueError('Final startup report disagrees with stdout')
    # This result records helpers activated before the GUI starts, not processes
    # remaining after exit. The caller verifies the real /proc process state.
    diagnostics = result.get('diagnostics')
    if (not isinstance(diagnostics, dict) or diagnostics.get('enabled') is not True
            or diagnostics.get('local_only') is not True or diagnostics.get('native_capture') is not True
            or type(diagnostics.get('helper_processes')) is not int
            or diagnostics.get('helper_processes') != 1):
        raise ValueError('Application native crash diagnostics were not activated')
    return result


def preserve_interactions(result, profile, report_dir):
    interaction = result.get('interaction_check')
    if (not isinstance(interaction, dict) or interaction.get('completed') is not True
            or interaction.get('checks') != INTERACTION_CHECKS
            or interaction.get('shutdown_signal_received') is not True):
        raise ValueError('Packaged application interactions did not complete')
    images = interaction.get('screenshots')
    expected = {'interaction-settings.png', 'interaction-home.png'}
    if not isinstance(images, list) or len(images) != 2 or {item.get('file') for item in images} != expected:
        raise ValueError('Missing actual-window interaction screenshots')
    for item in images:
        path = Path(profile) / item['file']
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 10 * 1024**2:
            raise ValueError('Invalid interaction screenshot')
        image_bytes = path.read_bytes()
        if (image_bytes[:8] != b'\x89PNG\r\n\x1a\n' or image_bytes[12:16] != b'IHDR'
                or sha256(path) != item.get('sha256')):
            raise ValueError('Interaction screenshot hash or format differs')
        size = item.get('size')
        if not isinstance(size, list) or len(size) != 2 or any(type(n) is not int or n <= 100 for n in size):
            raise ValueError('Interaction screenshot has an invalid size')
        if len(image_bytes) < 24 or list(struct.unpack('>II', image_bytes[16:24])) != size:
            raise ValueError('Interaction screenshot dimensions disagree with the actual PNG')
        shutil.copyfile(path, report_dir / item['file'])
    return interaction


def matching_processes(executable):
    """Inspect this unique installation path; never kill or inspect account data."""
    result = []
    expected = str(Path(executable).resolve(strict=True))
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            if os.readlink(process / 'exe') == expected:
                result.append(int(process.name))
        except (OSError, PermissionError):
            continue
    return result


def test_crash_diagnostics(executable, home, environment, root, report_dir, identity):
    """These four deliberate faults prove redacted local logs, not ordinary crashes."""
    records = []
    expected = {'main': 'startup-failure', 'thread': 'unhandled-thread',
                'qt': 'qt-callback', 'native': 'native-fatal'}
    build_hash = sha256(bundle_source(executable.parent) / 'assembly-manifest.json')
    fields = {'schema', 'application', 'version', 'build_sha256', 'platform',
              'timestamp_utc', 'kind', 'frames', 'breadcrumbs', 'exception_type'}
    for mode, kind in expected.items():
        profile = home / ('diagnostic-' + mode)
        name = 'synthetic-diagnostic-' + mode
        command = [executable, '--startup-check', '--platform', 'offscreen',
                   '--profile-dir', profile, '--client-config', root / 'absent-client.json',
                   '--diagnostic-test', mode]
        process = run_capture(command, report_dir, name, cwd=root,
                              env=dict(environment, CACHECAD_ENABLE_DIAGNOSTIC_TESTS='1'),
                              expected=None)
        expected_exit = -signal.SIGABRT if mode == 'native' else 1
        if process.returncode != expected_exit:
            raise RuntimeError(f'Synthetic {mode} fault returned {process.returncode}, expected {expected_exit}')
        deadline = time.monotonic() + 15
        while matching_processes(executable) and time.monotonic() < deadline:
            time.sleep(0.1)
        if matching_processes(executable):
            raise RuntimeError('A synthetic diagnostic helper did not exit')
        reports = sorted((profile / 'diagnostics').glob('crash-*.json'))
        if not 1 <= len(reports) <= 8:
            raise RuntimeError(f'Synthetic {mode} fault did not produce bounded durable reports')
        parsed = []
        for path in reports:
            if (path.is_symlink() or not re.fullmatch(r'crash-[0-9a-f]{32}\.json', path.name)
                    or path.stat().st_size > 32768):
                raise RuntimeError('Invalid synthetic crash report')
            raw = path.read_text(encoding='utf-8')
            report = json.loads(raw)
            if (str(root) in raw or str(profile) in raw or 'Synthetic diagnostic failure' in raw
                    or not isinstance(report, dict) or set(report) - fields
                    or report.get('schema') != 1 or report.get('application') != 'CacheCAD'
                    or report.get('version') != identity['version']
                    or report.get('build_sha256') != build_hash
                    or not isinstance(report.get('frames'), list)):
                raise RuntimeError('Synthetic crash report failed identity or redaction checks')
            parsed.append(report)
        if kind not in [item.get('kind') for item in parsed]:
            raise RuntimeError(f'Synthetic {mode} fault omitted its expected report kind')
        record = {'scenario': mode, 'synthetic_fault': True, 'exit_code': process.returncode,
                  'durable_report_verified': True, 'local_only': True,
                  'all_installed_processes_exited': True, 'reports': parsed}
        (report_dir / f'{name}.json').write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
        records.append(record)
    return records


def test_rejections(executable, home, environment, root, report_dir):
    records = []
    cases = [('foreign-platform', ['--platform', 'windows']), ('nonempty-profile', [])]
    for name, extra in cases:
        profile = home / ('rejection-' + name)
        if name == 'nonempty-profile':
            profile.mkdir()
            (profile / 'unrelated-user-file.txt').write_text('preserve this fixture')
        command = [executable, '--startup-check', '--profile-dir', profile,
                   '--client-config', root / 'absent-client.json', *extra]
        process = run_capture(command, report_dir, name, env=environment, cwd=root, expected=None)
        if process.returncode == 0 or 'CACHECAD_APPLICATION_RESULT=' in process.stdout:
            raise RuntimeError(f'Invalid startup {name} was accepted')
        if name == 'nonempty-profile' and (profile / 'unrelated-user-file.txt').read_text() != 'preserve this fixture':
            raise RuntimeError('Rejected profile contents were changed')
        records.append({'scenario': name, 'rejected': True, 'exit_code': process.returncode})
    source = bundle_source(executable.parent) / 'src/gui/main_window.py'
    raw = source.read_bytes()
    try:
        source.write_bytes(raw + b'\n# synthetic integrity failure\n')
        process = run_capture([executable, '--startup-check', '--platform', 'offscreen',
                               '--profile-dir', home / 'rejection-tampered-source',
                               '--client-config', root / 'absent-client.json'],
                              report_dir, 'tampered-source', env=environment, cwd=root, expected=None)
        if process.returncode == 0 or 'CACHECAD_APPLICATION_RESULT=' in process.stdout:
            raise RuntimeError('A tampered installed application source was accepted')
    finally:
        source.write_bytes(raw)
    records.append({'scenario': 'tampered-source', 'rejected': True, 'exit_code': process.returncode})
    return records


def test_application(archive, report_dir, *, install_dir=None, offscreen_only=False, display=None, xauthority=None):
    if not offscreen_only and display is None:
        raise ValueError('A disposable local X display is required for the native application gate')
    summary = {'schema_version': 1, 'started_utc': datetime.now(timezone.utc).isoformat(),
               'archive_sha256': sha256(archive), 'status': 'running', 'scenarios': [],
               'native_runtime_verified': False, 'live_authentication_verified': False,
               'real_cad_operations_verified': False, 'os_network_sandbox': False,
               'network_controls': 'Application Python network/browser/process tripwires; no live credentials or client configuration.'}
    summary_path = report_dir / 'linux-runtime.json'
    def save():
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    save()
    try:
        temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix='cachecad-linux-test-', dir=temporary_root) as temporary:
            root = Path(temporary).resolve(strict=True)
            home = root / 'disposable home'
            home.mkdir(mode=0o700)
            environment = isolated_environment(home, display=display, xauthority=xauthority)
            # The launcher checks tempfile.gettempdir() in the child. Its TMPDIR
            # is isolated too, so interaction/fault profiles belong under it.
            profiles = Path(environment['TMPDIR'])
            bundle = extract_archive(archive, install_dir if install_dir is not None else root / 'extracted application')
            summary['installation'] = {'path': str(bundle), 'retained': install_dir is not None,
                                       'method': 'user-owned extracted application directory', 'system_changes': False}
            identity = json.loads((bundle_source(bundle) / 'assembly-manifest.json').read_text())['release_identity']
            summary['release_identity'] = identity
            native = audit_native_files(bundle)
            summary['native_binaries'] = native
            summary['minimum_glibc_required'] = minimum_glibc(native)
            executable = bundle / EXECUTABLE
            initial = snapshot(bundle)
            scenarios = [('offscreen', False)] if offscreen_only else [('offscreen', False), ('xcb', False), ('xcb', True)]
            for qt_platform, interaction in scenarios:
                name = 'application-' + ('interactions' if interaction else 'startup') + '-' + qt_platform
                profile = profiles / name
                command = [executable, '--startup-check', '--platform', qt_platform,
                           '--profile-dir', profile, '--client-config', root / 'absent-client.json']
                env = dict(environment)
                if interaction:
                    command.append('--interaction-check')
                    env['CACHECAD_ENABLE_INTERACTION_TESTS'] = '1'
                process = run_capture(command, report_dir, name, env=env, cwd=root)
                result = read_startup_result(process.stdout, profile, qt_platform)
                if matching_processes(executable):
                    raise RuntimeError('An application or diagnostic helper remained after normal exit')
                if interaction:
                    preserve_interactions(result, profile, report_dir)
                if snapshot(bundle) != initial:
                    raise RuntimeError('The extracted application modified its own bundle')
                for pattern in ('*.dpapi', '*token*.json', '*credentials*.json', '*.pickle', '*.pkl'):
                    if list(home.rglob(pattern)):
                        raise RuntimeError('Signed-out startup created credential material')
                record = {'platform': qt_platform, 'interaction_check': interaction,
                          'exit_code': process.returncode, 'result': result, 'disposable_home': True,
                          'clean_exit': True, 'bundle_unchanged': True,
                          'all_installed_processes_exited': True}
                summary['scenarios'].append(record)
                (report_dir / f'{name}.json').write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
                save()
            summary['rejections'] = test_rejections(executable, profiles, environment, root, report_dir)
            summary['synthetic_crash_diagnostics'] = test_crash_diagnostics(executable, profiles, environment, root, report_dir, identity)
            if snapshot(bundle) != initial:
                raise RuntimeError('Installation changed during disposable tests')
            if matching_processes(executable):
                raise RuntimeError('An installed test application remains running')
            summary.update(status='passed', native_runtime_verified=not offscreen_only,
                           interactions_verified=not offscreen_only, signed_out_startup_verified=True,
                           clean_exit_verified=True, extracted_archive_verified=True,
                           synthetic_crash_diagnostics_verified=True, profile_cleanup_verified=False)
        summary['profile_cleanup_verified'] = not root.exists()
        if not summary['profile_cleanup_verified']:
            raise RuntimeError('Disposable profile was not removed')
    except Exception as error:
        summary.update(status='failed', error=str(error))
        raise
    finally:
        summary['finished_utc'] = datetime.now(timezone.utc).isoformat()
        save()
    print('Extracted Linux application checks passed' + (' (offscreen only; native GUI not verified)' if offscreen_only else ' including native xcb interactions'))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--report-dir', required=True, type=Path)
    parser.add_argument('--install-dir', type=Path, help='New user-owned directory to retain the tested installation')
    parser.add_argument('--offscreen-only', action='store_true', help='Diagnostic gate only; never marks native GUI verified')
    parser.add_argument('--display', default=os.environ.get('DISPLAY'), help='Explicit local disposable X display (defaults to DISPLAY)')
    parser.add_argument('--xauthority', type=Path, default=os.environ.get('XAUTHORITY'))
    args = parser.parse_args()
    if sys.platform != 'linux' or platform.machine().lower() not in ('x86_64', 'amd64'):
        parser.error('Native x86_64 Linux is required')
    report_dir = args.report_dir.absolute()
    report_dir.mkdir(parents=True, exist_ok=False)
    test_application(args.archive.resolve(strict=True), report_dir, install_dir=args.install_dir,
                     offscreen_only=args.offscreen_only, display=args.display, xauthority=args.xauthority)


if __name__ == '__main__':
    main()
