"""Launch the actual reviewed GUI in disposable, identity-only Google sign-in mode.

Real sign-in uses the user's own locally selected Desktop client JSON and system
browser. No recovered vendor authentication, Drive adapter, transfer, telemetry,
or updater is enabled. --smoke is a visibly synthetic, network-denied fixture.
These audited Python guards are not an OS sandbox for hostile/native code.
"""
import argparse
import hashlib
import importlib.abc
import importlib.util
import json
import os
import socket
from pathlib import Path
import sys
import threading
import time


# The Windows entrypoint preloads this reviewed sibling from verified bytes.
# Source execution imports a trusted local helper without running its main().
def _offline_launcher():
    if 'development.launch' in sys.modules:
        return sys.modules['development.launch']
    path = Path(__file__).resolve().with_name('launch.py')
    manifest = path.parent.parent / 'assembly-manifest.json'
    raw = path.read_bytes()
    if manifest.is_file():
        records = json.loads(manifest.read_text(encoding='utf-8'))['files']
        entries = [item for item in records if item['path'] == 'development/launch.py']
        if len(entries) != 1 or hashlib.sha256(raw).hexdigest() != entries[0]['sha256']:
            raise ValueError('Offline launcher differs from reviewed assembly')
    name = '_cachecad_signin_offline_launcher'
    module = type(sys)(name)
    module.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-source', type=Path)
    parser.add_argument('--smoke', action='store_true', help='Synthetic identity lifecycle; all network/browser/process operations denied')
    parser.add_argument('--scenario', choices=('success', 'failure', 'timeout', 'cancel-retry', 'stale-close', 'lifecycle'), default='lifecycle')
    parser.add_argument('--platform', choices=('offscreen', 'windows'))
    parser.add_argument('--style', choices=('platform-native', 'Fusion'), default='platform-native')
    args = parser.parse_args(argv)
    if args.app_source is None:
        if not getattr(sys, 'frozen', False) or not hasattr(sys, '_MEIPASS'):
            parser.error('--app-source is required when running from source')
        args.app_source = Path(sys._MEIPASS) / 'app-source'
    if args.platform == 'windows' and sys.platform != 'win32':
        parser.error('--platform windows requires Windows')
    return args


def install_signin_guards(profile, forbidden_modules):
    """Keep reviewed application writes disposable while allowing OAuth I/O.

    This deliberately does not replace socket/process functions: the identity
    helper needs HTTPS, a loopback listener and an explicit browser launch.
    Recovered vendor adapters remain import-blocked. Qt external launch paths
    are blocked separately below because the reviewed auth helper uses none.
    """
    profile = profile.resolve()
    attempts = []
    def deny(*args, **kwargs):
        attempts.append('external-ui-operation')
        raise RuntimeError('This operation is disabled in sign-in-only mode')

    def validate(path, fd=-1):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return
        target = Path(os.fsdecode(path))
        if not target.is_absolute() and isinstance(fd, int) and fd >= 0:
            attempts.append('directory-relative-write')
            raise RuntimeError('Directory-relative writes disabled in sign-in-only mode')
        if not target.resolve().is_relative_to(profile):
            attempts.append('write-outside-profile')
            raise RuntimeError('Write outside disposable sign-in profile denied')

    original_open = os.open
    def guarded_open(path, flags, mode=0o777, *, dir_fd=None):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            validate(path, -1 if dir_fd is None else dir_fd)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)
    os.open = guarded_open

    def audit(event, args):
        if event == 'open':
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(char in mode for char in 'wax+')) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if writing:
                validate(path)
        if event == 'os.symlink':
            attempts.append('symlink-creation')
            raise RuntimeError('Symlink creation disabled in sign-in-only mode')
        if event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.rename', 'os.link'):
            paths = args[:2] if event in ('os.rename', 'os.link') else args[:1]
            for index, path in enumerate(paths):
                fd_index = index + 2 if event in ('os.rename', 'os.link') else (2 if event == 'os.mkdir' else 1)
                fd = args[fd_index] if len(args) > fd_index else -1
                # shutil's descriptor-based profile cleanup is safe only when
                # its directory can be resolved on this platform.
                if isinstance(fd, int) and fd >= 0 and isinstance(path, (str, bytes, os.PathLike)):
                    if not Path('/proc/self/fd').is_dir():
                        validate(path, fd)
                    path = Path('/proc/self/fd', str(fd)).resolve(strict=True) / os.fsdecode(path)
                validate(path)
    class BlockVendorImports(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname in forbidden_modules:
                attempts.append('live-module:' + fullname)
                raise RuntimeError('Vendor/Drive module disabled in sign-in-only mode')
            return None
    sys.meta_path.insert(0, BlockVendorImports())
    sys.addaudithook(audit)
    return attempts, deny


def main(argv=None):
    args = parse_arguments(argv)
    offline = _offline_launcher()
    root = args.app_source.resolve(strict=True)
    sources = offline.verify_source(root)
    sys.dont_write_bytecode = True
    offline.install_source_importer(root, sources)
    with offline.disposable_profile() as profile:
        if args.platform or args.smoke:
            os.environ['QT_QPA_PLATFORM'] = args.platform or 'offscreen'
        app_data, working = profile / 'app_data', profile / 'projects'
        app_data.mkdir()
        working.mkdir()
        (app_data / 'settings.json').write_text(json.dumps(dict(
            working_directory=str(working), usage='signin-only', use_tray_icon=False,
            keep_revision_forever=False, default_drive_folder='', last_opened_project=None)), encoding='utf-8')
        attempts, deny = (offline.install_tripwires(profile) if args.smoke else
                          install_signin_guards(profile, offline.FORBIDDEN_MODULES))
        from utils.development_mode import configure_offline
        configure_offline(profile)  # No Drive factory, not even a fixture Drive client.
        from PySide6.QtWidgets import QApplication, QTextBrowser, QLabel, QAbstractButton, QSystemTrayIcon
        from PySide6.QtGui import QDesktopServices, QPixmap, QMovie, QIcon
        from PySide6.QtCore import QTimer, QProcess, qInstallMessageHandler
        slot_errors, qt_messages = [], []
        sys.excepthook = lambda kind, value, tb: slot_errors.append(kind.__name__)
        qInstallMessageHandler(lambda kind, context, message: qt_messages.append(str(message)))
        QSystemTrayIcon.show = lambda self: None
        QDesktopServices.openUrl = deny
        QProcess.start = deny
        QProcess.startDetached = deny
        import resources_rc
        assert resources_rc is not None
        from app.app_controller import AppController
        from gui.main_window import MainWindow
        from gui.desktop_signin_dialog import DesktopSignInDialog
        from core.desktop_signin import DesktopSignInError, SignInIdentity
        from core.signal_manager import signal_manager
        from core.thread_manager import thread_pool, network_pool
        application = QApplication(['cachecad-signin-only'])
        if args.style == 'Fusion':
            application.setStyle('Fusion')
        controller = AppController()
        window = MainWindow(controller)
        controller.set_main_window(window)
        window.setWindowTitle('CacheCAD - ' + ('SYNTHETIC SIGN-IN TEST' if args.smoke else 'GOOGLE SIGN-IN ONLY') + ' - disposable data')
        window.tray_icon.hide()
        from gui.page import Page
        for browser in window.pages[Page.HOME].findChildren(QTextBrowser):
            browser.setPlainText(
                'Google sign-in only\n\n'
                'Use the Google sign-in button and your own Desktop OAuth client JSON. '
                'This review build displays a verified email for this session only.\n\n'
                'Google Drive, CAD project changes, transfers, vendor services and updates '
                'are disabled. No tokens are saved. Application settings are disposable.\n\n'
                'This is an independently maintained reconstruction with placeholder artwork.')
        window.sponsor_label.hide()
        window.sponsor_logo.hide()
        result = {'mode': 'signin-only', 'kind': 'actual-app-signin',
                  'actual_gui': type(window).__module__ + '.' + type(window).__name__,
                  'actual_dialog': 'gui.desktop_signin_dialog.DesktopSignInDialog',
                  'qt_platform': application.platformName(), 'qt_style': application.style().objectName(),
                  'synthetic_auth': args.smoke, 'network_disabled': args.smoke,
                  'frozen': bool(getattr(sys, 'frozen', False)), 'source_loader': 'verified-source',
                  'outcomes': []}
        errors, dialogs, session = [], [], {'identity': None}
        if args.smoke:
            # Exercise the installed/frozen dependency closure without making
            # any request. The fixture backend itself needs no OAuth libraries.
            # Locked urllib3 probes IPv6 by binding ::1 during import. In this
            # fixture there is no networking, so declare IPv6 unavailable only
            # during import. All socket/DNS/browser/process denials stay active;
            # no attempted operation is ignored or removed from the report.
            ipv6_available = socket.has_ipv6
            try:
                socket.has_ipv6 = False
                from google_auth_oauthlib.flow import InstalledAppFlow
                from google.oauth2 import id_token
                from google.auth.transport.requests import Request
                import certifi
            finally:
                socket.has_ipv6 = ipv6_available
            result['certificate_bundle_present'] = Path(certifi.where()).is_file()
            result['sdk_imports_verified'] = bool(InstalledAppFlow and id_token.verify_oauth2_token and
                                                   Request and result['certificate_bundle_present'])
            if not result['sdk_imports_verified']:
                errors.append('Sign-in dependency/certificate bundle validation failed')
        def restrict_controls():
            central = window.centralWidget()
            for widget in central.findChildren(QTextBrowser):
                widget.setOpenExternalLinks(False)
                widget.setOpenLinks(False)
            for widget in central.findChildren(QLabel):
                widget.setOpenExternalLinks(False)
            for widget in central.findChildren(QAbstractButton):
                widget.setEnabled(widget is window.signin_button)
            window.project_list_widget.setEnabled(False)
            window.project_list_widget.setAcceptDrops(False)
            window.setAcceptDrops(False)
        for signal in (signal_manager.enable_buttons, signal_manager.disable_buttons,
                       signal_manager.upload_card_clicked, signal_manager.download_card_clicked,
                       signal_manager.latest_app_version):
            signal.connect(restrict_controls)
        def identity_changed(identity):
            session['identity'] = identity
            window.signin_button.setText('Google identity\n' + identity.email if identity else 'Google sign-in only')
            window.signin_button.setIcon(QIcon())
            window.show_status_message('Google identity verified; Drive and transfers remain disabled.' if identity
                                       else 'Signed out locally. Drive and transfers remain disabled.', 0)
            restrict_controls()
        identity_changed(None)

        fixture_steps, fixture_calls = [], []
        fixture_release = threading.Event()
        if args.smoke:
            if args.scenario == 'success':
                fixture_steps.extend(('success', 'success'))
            elif args.scenario == 'failure':
                fixture_steps.extend(('denied', 'success'))
            elif args.scenario == 'timeout':
                fixture_steps.extend(('timeout', 'success'))
            elif args.scenario == 'cancel-retry':
                fixture_steps.extend(('cancel', 'success'))
            elif args.scenario == 'stale-close':
                fixture_steps.extend(('stale', 'success'))
            else:
                fixture_steps.extend(('success', 'denied', 'timeout', 'cancel', 'stale', 'success'))
        class FixtureBackend:
            def __init__(self, path):
                self.path = path
            def run(self, cancel_event=None, timeout=120):
                mode = fixture_steps[len(fixture_calls)]
                fixture_calls.append(mode)
                if mode in ('denied', 'timeout'):
                    raise DesktopSignInError(mode)
                if mode in ('cancel', 'stale'):
                    while not cancel_event.wait(0.01):
                        pass
                    if mode == 'stale':
                        fixture_release.wait(5)
                    # Deliberately return late after cancellation to test the UI guard.
                    return SignInIdentity(email='stale@example.invalid', sub='stale-fixture')
                return SignInIdentity(email='fixture@example.invalid', sub='synthetic-fixture')
        def show_signin():
            for old in dialogs:
                if old.closed and not old.shutdown(0):
                    window.show_status_message('The previous sign-in is still cancelling. Try again shortly.', 0)
                    return
            if dialogs and not dialogs[-1].closed:
                dialogs[-1].show()
                dialogs[-1].raise_()
                dialogs[-1].activateWindow()
                return
            options = {}
            if args.smoke:
                options = dict(backend_factory=FixtureBackend,
                               config_picker=lambda *a, **k: (str(profile / 'synthetic-client.json'), 'JSON'), timeout=2)
            dialog = DesktopSignInDialog(window, identity=session['identity'], **options)
            dialogs.append(dialog)
            dialog.identity_changed.connect(identity_changed)
            dialog.attempt_finished.connect(result['outcomes'].append)
            dialog.show()
        # The recovered handler is also guarded by configure_offline. Disconnect
        # its route and never emit auth_success or user_profile_loaded here.
        signal_manager.sign_in_clicked.disconnect(controller.on_signin_clicked)
        signal_manager.sign_in_clicked.connect(show_signin)
        restrict_controls()
        window.show()
        from build_resources import ALIASES
        result['missing_placeholder_resources'] = [name for name in ALIASES
            if (not QMovie(':/assets/' + name).isValid() if name.endswith('.gif')
                else QPixmap(':/assets/' + name).isNull())]
        rendered = window.grab()
        result['rendered_size'] = [rendered.width(), rendered.height()]
        if result['missing_placeholder_resources'] or rendered.isNull():
            errors.append('Resource/render validation failed')
        if args.smoke:
            deadline = time.monotonic() + 20
            phase, expected_calls = ['open'], [0]
            result.update(repeated_click_ignored=True, stale_completion_ignored=True,
                          signout_cleared_identity=True, cancelled_identity_hidden=True)
            def check():
                try:
                    if time.monotonic() > deadline:
                        raise AssertionError('Synthetic sign-in lifecycle timed out')
                    if phase[0] == 'open':
                        window.signin_button.click()
                        dialog = dialogs[-1]
                        dialog.choose_button.click()
                        if fixture_calls:
                            raise AssertionError('Unexpected fixture call before explicit browser click')
                        phase[0] = 'start'
                    dialog = dialogs[-1]
                    if phase[0] == 'start':
                        if dialog._config_path is None:
                            dialog.choose_button.click()
                        expected_calls[0] += 1
                        dialog.signin_button.click()
                        dialog.start_signin()  # Repeated event must not create another worker.
                        phase[0] = 'running'
                    elif phase[0] == 'running' and len(fixture_calls) == expected_calls[0]:
                        mode = fixture_calls[-1]
                        if mode == 'cancel' and not dialog._cancelling:
                            dialog.cancel_button.click()
                        if mode == 'stale' and not dialog.closed:
                            before = time.monotonic()
                            dialog.close()
                            if time.monotonic() - before > 1.8:
                                raise AssertionError('Dialog close exceeded bounded join')
                            fixture_release.set()
                        if dialog.closed:
                            if not dialog.shutdown(0):
                                QTimer.singleShot(20, check)
                                return
                            if session['identity'] is not None:
                                raise AssertionError('Closed attempt published stale identity')
                            result['outcomes'].append('stale-close')
                            phase[0] = 'advance'
                        elif not dialog.busy:
                            if mode in ('denied', 'timeout', 'cancel') and dialog.identity is not None:
                                raise AssertionError('Failed/cancelled attempt published identity')
                            if mode == 'success':
                                if session['identity'] is None or session['identity'].email != 'fixture@example.invalid':
                                    raise AssertionError('Verified synthetic identity not displayed')
                                # Closing a successful dialog preserves the session,
                                # but its own account references must be cleared.
                                dialog.close()
                                if dialog.identity is not None or dialog.identity_label.text():
                                    raise AssertionError('Closed dialog retained identity')
                                window.signin_button.click()
                                dialog = dialogs[-1]
                                if dialog.identity is not session['identity']:
                                    raise AssertionError('Reopened dialog lost verified session identity')
                                dialog.signout_button.click()
                                if any(old.identity is not None or old.identity_label.text() for old in dialogs):
                                    raise AssertionError('Local sign-out left identity in a retained dialog')
                                result['closed_dialog_identity_cleared'] = True
                                if session['identity'] is not None or dialog.identity is not None or dialog.identity_label.text():
                                    raise AssertionError('Local sign-out retained identity')
                            phase[0] = 'advance'
                    if phase[0] == 'advance':
                        if len(fixture_calls) == len(fixture_steps):
                            result['fixture_lifecycle_passed'] = True
                            application.quit()
                            return
                        if dialogs[-1].closed:
                            window.signin_button.click()
                            dialogs[-1].choose_button.click()
                        phase[0] = 'start'
                    if len(fixture_calls) > expected_calls[0]:
                        raise AssertionError('Repeated click started duplicate worker')
                    QTimer.singleShot(20, check)
                except Exception as error:
                    errors.append(str(error))
                    fixture_release.set()
                    application.quit()
            QTimer.singleShot(0, check)
        application.aboutToQuit.connect(lambda: [dialog.shutdown(0) for dialog in dialogs])
        exit_code = application.exec()
        fixture_release.set()
        stopped = all([dialog.shutdown(1.5) for dialog in dialogs])
        if not stopped or not thread_pool.waitForDone(5000) or not network_pool.waitForDone(5000):
            print('Sign-in worker still stopping; disposable profile retained.', file=sys.stderr)
            os._exit(2)
        application.processEvents()
        result['transfer_button_enabled'] = window.pages[Page.SELECTION].execute_sync_button.isEnabled()
        result['non_signin_buttons_enabled'] = [type(widget).__name__ for widget in
            window.centralWidget().findChildren(QAbstractButton)
            if widget is not window.signin_button and widget.isEnabled()]
        from shiboken6 import isValid
        for dialog in dialogs:
            if isValid(dialog):
                dialog.deleteLater()
        if isValid(window.tray_icon):
            window.tray_icon.hide()
            window.tray_icon.deleteLater()
        if isValid(window):
            window.deleteLater()
        application.processEvents()
        result['forbidden_modules_loaded'] = [name for name in offline.FORBIDDEN_MODULES if name in sys.modules]
        result['external_attempts'] = attempts
        result['active_threads'] = thread_pool.activeThreadCount() + network_pool.activeThreadCount() + sum(dialog.busy for dialog in dialogs)
        result['credential_files'] = [p.name for p in profile.rglob('*') if p.name in ('token.json', 'creds.json', 'temp.json')]
        result['controller_signed_in'] = controller.signed_in
        result['controller_has_credentials'] = controller.creds is not None
        result['drive_client_created'] = hasattr(controller, 'drive_client')
        result['project_files'] = [p.name for p in working.rglob('*')]
        result['slot_errors'] = slot_errors
        result['qt_warnings'] = qt_messages if args.smoke else []
        result['qt_warning_count'] = len(qt_messages)
        session['identity'] = None
        if (result['forbidden_modules_loaded'] or attempts or result['active_threads'] or
                result['credential_files'] or controller.signed_in or controller.creds is not None or
                result['drive_client_created'] or result['project_files'] or slot_errors or
                result['transfer_button_enabled'] or result['non_signin_buttons_enabled']):
            errors.append('Sign-in-only safety invariant failed')
        result['errors'] = errors
        print('CACHECAD_SIGNIN_RESULT=' + json.dumps(result, sort_keys=True))
        return 1 if errors else exit_code


if __name__ == '__main__':
    raise SystemExit(main())
