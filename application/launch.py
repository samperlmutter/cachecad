"""Start the real CacheCAD application using reviewed, hash-verified source.

Normal launch uses persistent isolated state and actual Google/Drive services.
--startup-check only renders the real signed-out window, with network and account
restoration disabled. It never fabricates a user, credential or successful scan.
"""
import argparse
from contextlib import ExitStack, contextmanager, redirect_stdout, redirect_stderr
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import logging
import os
from pathlib import Path, PurePosixPath
import socket
import sys
import tempfile
import webbrowser


BLOCKED_MODULES = frozenset((
    'core.bug_report', 'core.service_credential_manager', 'core.user_validation',
    'core.github_updater', 'core.oauth_server', 'utils.oauth_credential_import', 'utils.cloud_logging',
    'utils.app_logger',
))


def verify_source(root):
    """Verify reviewed bytes before importing first-party code or reading sessions."""
    root = root.resolve(strict=True)
    manifest = json.loads((root / 'assembly-manifest.json').read_text(encoding='utf-8'))
    seen, sources, listed = set(), {}, set()
    for record in manifest['files']:
        name = record['path']
        relative = PurePosixPath(name)
        if (not relative.parts or relative.is_absolute() or '..' in relative.parts
                or '\\' in name or ':' in name):
            raise ValueError('Unsafe application manifest path')
        path = root / relative
        if path in seen:
            raise ValueError('Duplicate application manifest path')
        seen.add(path)
        for part in (path, *path.parents):
            if part == root:
                break
            if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
                raise ValueError('Linked application source is not accepted')
        if not path.resolve().is_relative_to(root):
            raise ValueError('Application source escapes its reviewed directory')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record['sha256']:
            raise ValueError('Application source differs from its reviewed assembly')
        if relative.parts[0] == 'src' and path.suffix == '.py':
            sources[relative.as_posix()] = raw
            listed.add(path.resolve())
        elif relative.as_posix() in ('application/resources_rc.py', 'application/launch.py',
                                       'application/crash_diagnostics.py', 'application/build_version.py'):
            sources[relative.as_posix()] = raw
    if listed != {path.resolve() for path in (root / 'src').rglob('*.py')}:
        raise ValueError('Unmanifested application module')
    required = ('src/core/auth_manager.py', 'src/core/google_session.py',
                'src/utils/application_mode.py', 'src/gui/application_account.py',
                'application/resources_rc.py', 'application/crash_diagnostics.py')
    if any(name not in sources for name in required):
        raise ValueError('The real application source or resources are incomplete')
    return sources


class VerifiedSourceLoader(importlib.abc.Loader):
    def __init__(self, path, raw):
        self.path, self.raw = path, raw

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(compile(self.raw, str(self.path), 'exec'), module.__dict__)


class VerifiedSourceFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root, sources):
        self.modules, self.packages = {}, {}
        for relative, raw in sources.items():
            path = PurePosixPath(relative)
            parts = list(path.with_suffix('').parts[1:])
            is_package = parts[-1] == '__init__'
            if is_package:
                parts.pop()
            if not parts or not all(part.isidentifier() for part in parts):
                raise ValueError('Unsafe application module name')
            name = '.'.join(parts)
            if name in self.modules:
                raise ValueError('Duplicate application module')
            self.modules[name] = (root / relative, raw, is_package)
            for index in range(1, len(parts)):
                self.packages['.'.join(parts[:index])] = root / path.parts[0] / Path(*parts[:index])
        self.roots = {name.split('.')[0] for name in self.modules} | {'application', 'development'}

    def find_spec(self, fullname, path=None, target=None):
        if fullname in BLOCKED_MODULES or fullname.split('.')[0] == 'development':
            raise ImportError('This application does not use vendor services or development backends')
        if fullname in self.modules:
            location, raw, is_package = self.modules[fullname]
            return importlib.util.spec_from_file_location(fullname, location,
                loader=VerifiedSourceLoader(location, raw),
                submodule_search_locations=[str(location.parent)] if is_package else None)
        if fullname in self.packages:
            spec = importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [str(self.packages[fullname])]
            return spec
        if fullname.split('.')[0] in self.roots:
            raise ModuleNotFoundError('Unmanifested application module')
        return None


def install_source_importer(root, sources):
    finder = VerifiedSourceFinder(root, sources)
    if any(name.split('.')[0] in finder.roots for name in sys.modules):
        raise RuntimeError('Application modules were loaded before source verification')
    sys.meta_path.insert(0, finder)
    return finder


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-source', type=Path)
    parser.add_argument('--client-config', type=Path,
        help='Distributor-owned Desktop client JSON; end users do not create a client')
    parser.add_argument('--profile-dir', type=Path,
        help='Isolated persistent application profile (must not contain legacy credentials)')
    parser.add_argument('--startup-check', action='store_true',
        help='Render the actual signed-out UI with no network, then exit')
    parser.add_argument('--platform', choices=('offscreen', 'windows'))
    parser.add_argument('--preserved-profile-check', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--diagnostic-test', choices=('main', 'thread', 'qt', 'native'),
        help=argparse.SUPPRESS)
    parser.add_argument('--diagnostic-native-helper', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.diagnostic_test and (not args.startup_check or args.profile_dir is None
            or os.environ.get('CACHECAD_ENABLE_DIAGNOSTIC_TESTS') != '1'
            or args.diagnostic_native_helper):
        parser.error('Diagnostic fault tests require explicit test mode and a new disposable profile')
    if args.diagnostic_native_helper and (args.profile_dir is None or args.startup_check):
        parser.error('The private diagnostics helper requires an application profile')
    if args.app_source is None:
        if not getattr(sys, 'frozen', False) or not hasattr(sys, '_MEIPASS'):
            parser.error('--app-source is required for a source launch')
        args.app_source = Path(sys._MEIPASS) / 'app-source'
    if args.platform == 'windows' and sys.platform != 'win32':
        parser.error('--platform windows requires Windows')
    if args.client_config is None:
        supplied = os.environ.get('CACHECAD_GOOGLE_CLIENT_CONFIG')
        args.client_config = Path(supplied) if supplied else args.app_source / 'config' / 'google-desktop-client.json'
    args.client_config = args.client_config.absolute()
    if args.profile_dir is not None:
        args.profile_dir = args.profile_dir.absolute()
    if args.preserved_profile_check:
        if (not args.startup_check or args.profile_dir is None or args.diagnostic_test
                or args.diagnostic_native_helper or os.environ.get('CACHECAD_ENABLE_UPGRADE_TESTS') != '1'
                or args.client_config.exists() or args.client_config.is_symlink()):
            parser.error('Preserved-profile checks require an explicit disposable upgrade fixture and missing client configuration')
        temporary = Path(tempfile.gettempdir()).resolve()
        profile = args.profile_dir.resolve()
        if profile == temporary or not profile.is_relative_to(temporary):
            parser.error('Preserved-profile checks require a profile inside the disposable temporary directory')
        marker = profile / '.cachecad-upgrade-fixture.json'
        try:
            if marker.is_symlink() or marker.stat().st_size > 1024 or json.loads(marker.read_text()) != {
                    'fixture': 'cachecad-upgrade-ci', 'schema': 1}:
                raise ValueError
        except (OSError, ValueError):
            parser.error('The disposable upgrade fixture marker is missing or invalid')
    return args


def prepare_profile(profile, *, startup_check=False, preserved_profile_check=False):
    """Never import the legacy plaintext token cache, settings or project list."""
    profile = Path(profile).absolute()
    for part in (profile, *profile.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('Linked application profile is not accepted')
    if profile.exists() and not profile.is_dir():
        raise ValueError('The application profile is not a directory')
    contents = [path for path in profile.iterdir() if path.name != '.cachecad-profile.lock'] if profile.exists() else []
    if startup_check and contents and not preserved_profile_check:
        raise ValueError('Startup checks require a new empty disposable profile')
    marker = profile / 'application-profile.json'
    if contents:
        if not marker.is_file() or json.loads(marker.read_text()) != {'application': 'cachecad-reconstructed', 'schema': 1}:
            raise ValueError('Choose a new profile; legacy data is never imported automatically')
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = profile / 'app_data'
    data.mkdir(mode=0o700, exist_ok=True)
    if any((data / name).exists() for name in ('token.json', 'creds.json', 'temp.json')):
        raise ValueError('Legacy credential files are not accepted in this profile')
    if not marker.exists():
        marker.write_text(json.dumps({'application': 'cachecad-reconstructed', 'schema': 1}), encoding='utf-8')
    settings = data / 'settings.json'
    if not settings.exists():
        settings.write_text(json.dumps({
            'working_directory': str(profile / 'projects'),
            'use_tray_icon': False, 'keep_revision_forever': False,
            'default_drive_folder': '', 'last_opened_project': None,
        }), encoding='utf-8')
    return profile.resolve()


def install_startup_check_guards():
    """Deny effects during a CI render. This is not a production fake service."""
    attempts = []
    def deny(*args, **kwargs):
        attempts.append('external-operation')
        raise RuntimeError('External operations are forbidden during the startup check')
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.socket.bind = deny
    socket.create_connection = deny
    socket.getaddrinfo = deny
    socket.gethostbyname = deny
    socket.socket.sendto = deny
    webbrowser.open = deny
    webbrowser.open_new = deny
    webbrowser.open_new_tab = deny
    def audit(event, args):
        if event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo', 'socket.gethostbyname',
                     'socket.sendto', 'subprocess.Popen', 'os.system', 'os.posix_spawn',
                     'os.exec', 'os.fork', 'os.spawn', 'os.startfile', 'os.startfile/2'):
            deny()
    sys.addaudithook(audit)
    # urllib3 probes IPv6 with a loopback bind at import. Skip only that capability
    # probe in this network-denied check; every bind/connection tripwire remains.
    ipv6 = socket.has_ipv6
    try:
        socket.has_ipv6 = False
        import requests
    finally:
        socket.has_ipv6 = ipv6
    return attempts


@contextmanager
def application_profile(args, default):
    from utils.profile_lock import ProfileLock
    previous = Path.cwd()
    @contextmanager
    def locked_profile(path):
        path = Path(path).absolute()
        for part in (path, *path.parents):
            if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
                raise ValueError('Linked application profile is not accepted')
        with ProfileLock(path):
            profile = prepare_profile(path, startup_check=args.startup_check,
                                      preserved_profile_check=getattr(args, 'preserved_profile_check', False))
            try:
                os.chdir(profile)
                yield profile
            finally:
                os.chdir(previous)
    if args.startup_check and args.profile_dir is None:
        with tempfile.TemporaryDirectory(prefix='cachecad-startup-') as directory:
            with locked_profile(directory) as profile:
                yield profile
    else:
        with locked_profile(args.profile_dir or default) as profile:
            yield profile


def native_helper_command(args, root, profile):
    command = [sys.executable]
    if not getattr(sys, 'frozen', False):
        command += ['-I', '-S', '-B', str(root / 'application' / 'launch.py')]
    return command + ['--app-source', str(root), '--profile-dir', str(profile),
                      '--diagnostic-native-helper']


def run_native_helper(args, diagnostics_module, catalog, build):
    # Parent owns the application profile lock. Only a marked, currently locked
    # profile is eligible; no arbitrary output path is accepted by this mode.
    from utils.profile_lock import ProfileLock, ProfileLockError
    profile = args.profile_dir
    for part in (profile, *profile.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            return 1
    marker = profile / 'application-profile.json'
    lock_path = profile / '.cachecad-profile.lock'
    if (not lock_path.is_file() or marker.is_symlink() or not marker.is_file()
            or marker.stat().st_size > 1024 or json.loads(marker.read_text()) !=
            {'application': 'cachecad-reconstructed', 'schema': 1}):
        return 1
    lock = ProfileLock(profile)
    try:
        lock.acquire()
    except ProfileLockError as error:
        if error.code != 'busy':
            return 1
    else:
        lock.close()
        return 1
    return diagnostics_module.capture_native(profile / 'diagnostics', catalog, build, announce=True)


def diagnostic_fault(mode):
    # This function is reachable only after explicit CLI/environment gating and
    # prepare_profile's new-empty-profile check. Never use a real user profile.
    if mode == 'main':
        raise RuntimeError('Synthetic diagnostic failure')
    if mode == 'thread':
        import threading
        def fail_thread():
            raise RuntimeError('Synthetic diagnostic failure')
        worker = threading.Thread(target=fail_thread)
        worker.start()
        worker.join(5)
    elif mode == 'native':
        if sys.platform == 'win32':
            import ctypes
            # Suppress Windows Error Reporting dialogs only in this disposable
            # fault-test process. Normal app system settings are untouched.
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
            try:
                ctypes.CDLL('ucrtbase')._set_abort_behavior(0, 3)
            except (AttributeError, OSError):
                pass
        else:
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.abort()


def snapshot_preserved_profile(profile):
    """Hash fixture data while retaining the active profile-exclusion lock.

    Windows byte-range locks deny reads through another handle, including from
    this process. The runtime lock is not user data: never reopen it here. The
    external installer test still checks its bytes after this process exits.
    """
    preserved = {}
    for path in profile.rglob('*'):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Linked upgrade fixture content is not accepted')
        relative = path.relative_to(profile).as_posix()
        if relative != '.cachecad-profile.lock' and path.is_file():
            preserved[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return preserved


def run_application(args, root, diagnostics, lifetime=None, pending_updates=None):
    from utils.application_mode import configure_application, default_profile_root
    if args.platform or args.startup_check:
        os.environ['QT_QPA_PLATFORM'] = args.platform or 'offscreen'
    result = None
    if sys.platform == 'win32' and lifetime is not None:
        from core.windows_update_install import ApplicationRunLease
        lifetime.enter_context(ApplicationRunLease())
    with application_profile(args, default_profile_root()) as profile:
        preserved = (snapshot_preserved_profile(profile)
                     if getattr(args, 'preserved_profile_check', False) else {})
        diagnostics.activate(profile / 'diagnostics')
        diagnostics.mark('profile_ready')
        native_capture = diagnostics.enable_native(native_helper_command(args, root, profile))
        if args.diagnostic_test:
            diagnostics.mark('diagnostic_test')
            if args.diagnostic_test == 'native' and not native_capture:
                return 1
            if args.diagnostic_test != 'qt':
                diagnostic_fault(args.diagnostic_test)
                return 1
        configure_application(profile, args.client_config, startup_check=args.startup_check)
        diagnostics.mark('application_configured')
        attempts = install_startup_check_guards() if args.startup_check else []
        # Recovered modules print file paths and provider details. The real entrypoint
        # deliberately enables no file/cloud logger and suppresses those raw streams.
        logging.disable(logging.CRITICAL)
        with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
            from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMessageBox
            from PySide6.QtCore import QTimer, qInstallMessageHandler
            import resources_rc
            from app.app_controller import AppController
            from gui.main_window import MainWindow
            from core.thread_manager import thread_pool, network_pool
            slot_errors = []
            diagnostics.set_qt_callback(lambda: slot_errors.append('Unhandled application callback'))
            qInstallMessageHandler(lambda *args: None)
            app = QApplication(['cachecad'])
            diagnostics.mark('qt_started')
            controller = AppController()
            window = MainWindow(controller)
            controller.set_main_window(window)
            if not QSystemTrayIcon.isSystemTrayAvailable():
                window.tray_icon.hide()
            window.show()
            diagnostics.mark('window_shown')
            if diagnostics.previous_reports and not args.startup_check:
                notice = QMessageBox(window)
                notice.setWindowTitle('Local crash diagnostics')
                notice.setText('A previous CacheCAD error report is saved in the diagnostics folder '
                               'inside your application profile. Reports stay on this computer; '
                               'nothing is uploaded automatically.')
                notice.setInformativeText(str(profile / 'diagnostics'))
                notice.setStandardButtons(QMessageBox.Ok)
                notice.show()
            if args.startup_check:
                from googleapiclient.discovery import build
                from googleapiclient.discovery_cache import get_static_doc
                from google.oauth2.credentials import Credentials
                from google.auth.transport.requests import Request, AuthorizedSession
                from google_auth_oauthlib.flow import InstalledAppFlow
                from httplib2 import Http
                import certifi
                result = {
                    'version': __import__('build_version').APP_VERSION,
                    'release_number': __import__('build_version').APP_RELEASE_NUMBER,
                    'windows_version': __import__('build_version').APP_WINDOWS_VERSION,
                    'product_id': __import__('build_version').APP_PRODUCT_ID,
                    'mode': 'application', 'actual_gui': type(window).__module__ + '.' + type(window).__name__,
                    'qt_platform': app.platformName(), 'frozen': bool(getattr(sys, 'frozen', False)),
                    'source_loader': 'verified-source', 'network_disabled': True,
                    'sdk_imports_verified': all(callable(value) for value in (build, Credentials, Request, AuthorizedSession, InstalledAppFlow, Http)),
                    'certificate_bundle_present': Path(certifi.where()).is_file(),
                    'drive_discovery_present': bool(get_static_doc('drive', 'v3')),
                    'diagnostics': {'enabled': diagnostics.available, 'local_only': True,
                                    'native_capture': native_capture,
                                    'helper_processes': int(native_capture)},
                }
                def finish_check():
                    pixmap = window.grab()
                    result.update(rendered_size=[pixmap.width(), pixmap.height()],
                        signed_in=controller.signed_in,
                        has_credentials=controller.auth_manager.get_creds() is not None,
                        drive_client_created=hasattr(controller, 'drive_client'),
                        setup_required=controller.auth_manager.state == 'setup_missing')
                    if pixmap.isNull():
                        slot_errors.append('Window render failed')
                    app.quit()
                if args.diagnostic_test == 'qt':
                    def fail_qt_callback():
                        raise RuntimeError('Synthetic diagnostic callback failure')
                    QTimer.singleShot(0, fail_qt_callback)
                QTimer.singleShot(80, finish_check)
            exit_code = app.exec()
            diagnostics.mark('shutdown_started')
            account_stopped = controller.shutdown()
            pools_stopped = thread_pool.waitForDone(5000) and network_pool.waitForDone(5000)
            if not account_stopped or not pools_stopped:
                # Do not remove a temporary profile or tear down QObject workers
                # underneath unfinished work. The process owns final termination.
                diagnostics.mark('shutdown_timeout')
                diagnostics.record_event('shutdown-timeout')
                diagnostics.close()
                os._exit(2)
            app.processEvents()
            for database in controller._retired_databases + ([controller.db_manager] if hasattr(controller, 'db_manager') else []):
                database.conn.close()
            from shiboken6 import isValid
            if isValid(window.tray_icon):
                window.tray_icon.hide()
                window.tray_icon.deleteLater()
            window.deleteLater()
            app.processEvents()
            if args.startup_check:
                result.update(
                    active_threads=thread_pool.activeThreadCount() + network_pool.activeThreadCount(),
                    external_attempts=attempts,
                    development_modules_loaded=[name for name in sys.modules if name == 'development' or name.startswith('development.')],
                    forbidden_modules_loaded=sorted(BLOCKED_MODULES.intersection(sys.modules)),
                    credential_files=[p.name for p in profile.rglob('*') if p.name in
                                      ('token.json', 'creds.json', 'temp.json', 'google-session.dpapi')
                                      and p.relative_to(profile).as_posix() not in preserved],
                    errors=list(slot_errors))
                if getattr(args, 'preserved_profile_check', False):
                    after = snapshot_preserved_profile(profile)
                    result['preserved_profile_unchanged'] = after == preserved
                    if after != preserved:
                        result['errors'].append('Preserved upgrade fixture bytes changed during startup')
                if (result['signed_in'] or result['has_credentials'] or result['drive_client_created']
                        or attempts or result['active_threads'] or result['credential_files']
                        or result['development_modules_loaded'] or result['forbidden_modules_loaded']):
                    result['errors'].append('Signed-out startup safety invariant failed')
                exit_code = 1 if result['errors'] else exit_code
            coordinator = getattr(controller, '_update_coordinator', None)
            if (coordinator is not None and pending_updates is not None
                    and coordinator.install_pending):
                pending_updates.append(coordinator)
            diagnostics.mark('shutdown_finished')
            diagnostics.set_qt_callback(None)
    if result is not None:
        print('CACHECAD_APPLICATION_RESULT=' + json.dumps(result, sort_keys=True))
    return exit_code


def main(argv=None):
    args = parse_arguments(argv)
    diagnostics = None
    lifetime = ExitStack()
    pending_updates = []
    try:
        root = args.app_source.resolve(strict=True)
        sources = verify_source(root)
        sys.dont_write_bytecode = True
        install_source_importer(root, sources)
        import crash_diagnostics
        import build_version
        crash_diagnostics.APP_VERSION = build_version.APP_VERSION
        catalog = crash_diagnostics.source_catalog(root, sources)
        build = hashlib.sha256((root / 'assembly-manifest.json').read_bytes()).hexdigest()
        if args.diagnostic_native_helper:
            return run_native_helper(args, crash_diagnostics, catalog, build)
        diagnostics = crash_diagnostics.CrashDiagnostics(catalog, build).install()
        exit_code = run_application(args, root, diagnostics, lifetime, pending_updates)
        diagnostics_stopped = diagnostics.close()
        for coordinator in pending_updates:
            if exit_code == 0 and diagnostics_stopped:
                if not coordinator.commit_after_clean_shutdown():
                    exit_code = 1
            else:
                coordinator.abort_pending_install()
        return exit_code
    except Exception:
        if diagnostics is not None:
            diagnostics.record_exception('startup-failure', *sys.exc_info())
            if getattr(args, 'startup_check', False):
                # Only the existing redacted record is exposed by disposable CI.
                # Never print the exception value, raw traceback, or local paths.
                try:
                    if diagnostics.pending:
                        print('CACHECAD_STARTUP_DIAGNOSTIC=' +
                              json.dumps(diagnostics.pending[-1], sort_keys=True), file=sys.stderr)
                except Exception:
                    pass
        # No exception traceback may expose OAuth material or local file metadata.
        print('CacheCAD could not start. Check the reviewed installation and isolated application profile.', file=sys.stderr)
        return 1
    finally:
        for coordinator in pending_updates:
            coordinator.abort_pending_install()
        if diagnostics is not None:
            diagnostics.close()
        lifetime.close()


if __name__ == '__main__':
    raise SystemExit(main())
