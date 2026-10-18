"""Launch the actual assembled GUI with explicit offline, disposable development state.

Audited-code tripwires, not an OS sandbox for hostile native code. No real project
path, credential option, or account login is accepted by this launcher.
"""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import logging
import os
from pathlib import Path, PurePosixPath
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser


FORBIDDEN_MODULES = ('core.auth_manager', 'core.bug_report', 'core.service_credential_manager',
                     'core.user_validation', 'utils.oauth_credential_import', 'utils.cloud_logging',
                     'core.drive_client', 'core.github_updater')


def verify_source(root):
    """Return verified Python bytes; never execute or trust adjacent bytecode."""
    root = root.resolve(strict=True)
    manifest = json.loads((root / 'assembly-manifest.json').read_text(encoding='utf-8'))
    listed, seen, sources = set(), set(), {}
    for entry in manifest['files']:
        name = entry['path']
        relative = PurePosixPath(name)
        if (not relative.parts or relative.is_absolute() or '..' in relative.parts
                or '\\' in name or ':' in name):
            raise ValueError('Unsafe application manifest path')
        path = root / relative
        if path in seen:
            raise ValueError('Duplicate application manifest path')
        seen.add(path)
        for parent in (path, *path.parents):
            if parent == root:
                break
            if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
                raise ValueError('Unsafe application source path')
        if not path.resolve().is_relative_to(root):
            raise ValueError('Unsafe application source path')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Application source differs from reviewed assembly')
        if relative.parts[0] == 'src' and path.suffix == '.py':
            listed.add(path.resolve())
            sources[relative.as_posix()] = raw
        elif relative.as_posix() in ('development/resources_rc.py', 'development/build_resources.py'):
            sources[relative.as_posix()] = raw
    if listed != {p.resolve() for p in (root / 'src').rglob('*.py')}:
        raise ValueError('Unmanifested application module')
    return sources


class VerifiedSourceLoader(importlib.abc.Loader):
    """Compile only manifest-verified bytes, including in a frozen distribution."""

    def __init__(self, path, source):
        self.path, self.source = path, source

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(compile(self.source, str(self.path), 'exec'), module.__dict__)


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
            self.modules[name] = (root / relative, raw, is_package)
            for index in range(1, len(parts)):
                self.packages['.'.join(parts[:index])] = root / path.parts[0] / Path(*parts[:index])
        self.roots = {name.split('.')[0] for name in self.modules} | {'resources_rc', 'build_resources'}

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.modules:
            location, raw, is_package = self.modules[fullname]
            return importlib.util.spec_from_file_location(
                fullname, location, loader=VerifiedSourceLoader(location, raw),
                submodule_search_locations=[str(location.parent)] if is_package else None)
        if fullname in self.packages:
            spec = importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [str(self.packages[fullname])]
            return spec
        if fullname.split('.')[0] in self.roots:
            raise ModuleNotFoundError('Unmanifested application import: ' + fullname)
        return None


def install_source_importer(root, sources):
    finder = VerifiedSourceFinder(root, sources)
    if any(name.split('.')[0] in finder.roots for name in sys.modules):
        raise RuntimeError('Application modules loaded before source verification')
    # Precede PyInstaller's archive finder: bundled hidden imports are dependency
    # discovery inputs, never an alternate authority for first-party code.
    sys.meta_path.insert(0, finder)
    return finder


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-source', type=Path,
                        help='Reviewed assembly; defaults to the bundled source in a frozen build')
    parser.add_argument('--smoke', action='store_true', help='Exercise startup and a synthetic scan, then exit')
    parser.add_argument('--scenario', choices=('success', 'lifecycle', 'close-pending'), default='success')
    parser.add_argument('--platform', choices=('offscreen', 'windows'),
                        help='Qt backend; smoke tests default to offscreen')
    args = parser.parse_args(argv)
    if args.app_source is None:
        if not getattr(sys, 'frozen', False) or not hasattr(sys, '_MEIPASS'):
            parser.error('--app-source is required when running from source')
        args.app_source = Path(sys._MEIPASS) / 'app-source'
    if args.platform == 'windows' and sys.platform != 'win32':
        parser.error('--platform windows requires Windows')
    return args


@contextmanager
def disposable_profile():
    """Restore cwd before removal, required for Windows directory cleanup."""
    old_cwd, old_tempdir = Path.cwd(), tempfile.tempdir
    keys = ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HOMEDRIVE', 'HOMEPATH',
            'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR',
            'TMPDIR', 'TEMP', 'TMP')
    previous = {key: os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory(prefix='cachecad-offline-') as directory:
        profile = Path(directory).resolve()
        try:
            os.chdir(profile)
            for key in keys:
                os.environ[key] = str(profile)
            if sys.platform == 'win32':
                drive, tail = os.path.splitdrive(str(profile))
                os.environ['HOMEDRIVE'], os.environ['HOMEPATH'] = drive, tail
            runtime = profile / 'runtime'
            runtime.mkdir(mode=0o700)
            os.environ['XDG_RUNTIME_DIR'] = str(runtime)
            # TemporaryDirectory cached the parent temp directory; updating env
            # alone would send later Python temporary files outside this profile.
            tempfile.tempdir = str(profile)
            yield profile
        finally:
            # Windows cannot remove files with live logging handles.
            logging.shutdown()
            os.chdir(old_cwd)
            tempfile.tempdir = old_tempdir
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def install_tripwires(profile):
    attempts = []
    def deny(*args, **kwargs):
        attempts.append('network/process/browser')
        raise RuntimeError('External operation disabled in offline development')
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.socket.bind = deny
    socket.create_connection = deny
    socket.getaddrinfo = deny
    socket.gethostbyname = deny
    socket.gethostbyname_ex = deny
    socket.socket.sendto = deny
    original_open = os.open
    def guarded_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            attempts.append('dir-fd-write')
            raise RuntimeError('Directory-relative writes disabled in offline development')
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)
    os.open = guarded_open
    subprocess.Popen = deny
    os.system = deny
    if hasattr(os, 'startfile'):
        os.startfile = deny
    webbrowser.open = deny
    webbrowser.open_new = deny
    webbrowser.open_new_tab = deny

    def audit(event, args):
        if event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo', 'socket.gethostbyname',
                     'socket.sendto', 'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.exec', 'os.fork',
                     'os.spawn', 'os.startfile', 'os.startfile/2'):
            deny()
        if event == 'open':
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(char in mode for char in 'wax+')) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if writing and isinstance(path, (str, bytes, os.PathLike)):
                if not Path(os.fsdecode(path)).resolve().is_relative_to(profile):
                    attempts.append('write-outside-profile')
                    raise RuntimeError('Write outside disposable profile denied')
        if event == 'os.symlink':
            attempts.append('symlink-creation')
            raise RuntimeError('Symlink creation disabled in offline development')
        if event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.rename', 'os.link', 'os.symlink'):
            paths = args[:2] if event in ('os.rename', 'os.link', 'os.symlink') else args[:1]
            for index, path in enumerate(paths):
                if not isinstance(path, (str, bytes, os.PathLike)):
                    continue
                target = Path(os.fsdecode(path))
                fd_index = index + 2 if event in ('os.rename', 'os.link') else (2 if event == 'os.mkdir' else 1)
                fd = args[fd_index] if len(args) > fd_index else -1
                if not target.is_absolute() and isinstance(fd, int) and fd >= 0:
                    if not Path('/proc/self/fd').is_dir():
                        attempts.append('unsupported-dir-fd-mutation')
                        raise RuntimeError('Directory-relative mutation cannot be verified on this platform')
                    target = Path('/proc/self/fd', str(fd)).resolve(strict=True) / target
                if not target.resolve().is_relative_to(profile):
                    attempts.append('mutation-outside-profile')
                    raise RuntimeError('Filesystem mutation outside disposable profile denied')
    class BlockLiveImports(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname in FORBIDDEN_MODULES:
                attempts.append('live-module:' + fullname)
                raise RuntimeError('Live account module disabled in offline development')
            return None
    sys.meta_path.insert(0, BlockLiveImports())
    sys.addaudithook(audit)
    return attempts, deny


def main():
    args = parse_arguments()
    root = args.app_source.resolve(strict=True)
    sources = verify_source(root)
    sys.dont_write_bytecode = True
    install_source_importer(root, sources)
    with disposable_profile() as profile:
        if args.platform or args.smoke:
            os.environ['QT_QPA_PLATFORM'] = args.platform or 'offscreen'
        app_data = profile / 'app_data'
        app_data.mkdir()
        working = profile / 'projects'
        working.mkdir()
        (app_data / 'settings.json').write_text(json.dumps(dict(
            working_directory=str(working), usage='offline-development', use_tray_icon=False,
            keep_revision_forever=False, default_drive_folder='', last_opened_project=None)))
        attempts, deny = install_tripwires(profile)
        from utils.development_mode import configure_offline

        class FixtureDrive:
            """Only remote I/O is synthetic; controller, scans, DB and Qt remain real."""
            is_offline_fixture = True

            def __init__(self):
                self.calls = []

            def get_drive_items_from_project_id(self, project_id, cached_folders=None, *, scan_id, on_success, on_error):
                self.calls.append((scan_id, on_success, on_error))
                if args.scenario in ('lifecycle', 'close-pending'):
                    if len(self.calls) == 1:
                        return  # A delayed remote response for the cancelled generation.
                    if len(self.calls) == 2:
                        on_error(scan_id, 'Synthetic remote scan failure')
                        return
                on_success(scan_id, [])

        configure_offline(profile, drive_client_factory=FixtureDrive if args.smoke else None)
        from PySide6.QtWidgets import QApplication, QTextBrowser, QLabel, QFileDialog, QPushButton, QSystemTrayIcon, QMessageBox
        from PySide6.QtGui import QDesktopServices, QPixmap, QMovie
        from PySide6.QtCore import QTimer, QProcess, qInstallMessageHandler
        slot_errors, qt_messages = [], []
        def exception_hook(kind, value, tb):
            slot_errors.append(kind.__name__ + ': ' + str(value))
            sys.__excepthook__(kind, value, tb)
        sys.excepthook = exception_hook
        qInstallMessageHandler(lambda kind, context, message: qt_messages.append(str(message)))
        QSystemTrayIcon.show = lambda self: None
        QDesktopServices.openUrl = deny
        QProcess.start = deny
        QProcess.startDetached = deny
        QFileDialog.getExistingDirectory = lambda *a, **k: ''
        import resources_rc  # Authored development placeholders, never vendor assets.
        assert resources_rc is not None
        from app.app_controller import AppController
        from gui.main_window import MainWindow
        from core.thread_manager import thread_pool, network_pool
        application = QApplication(['cachecad-offline'])
        controller = AppController()
        window = MainWindow(controller)
        controller.set_main_window(window)
        window.setWindowTitle('CacheCAD - OFFLINE DEVELOPMENT - disposable data')
        window.tray_icon.hide()
        def restrict_controls():
            for widget in window.findChildren(QTextBrowser):
                widget.setOpenExternalLinks(False)
                widget.setOpenLinks(False)
            for widget in window.findChildren(QLabel):
                widget.setOpenExternalLinks(False)
            for widget in window.findChildren(QPushButton):
                if (type(widget).__name__ in ('OpenFolderButton', 'OpenURLButton', 'SponsorLogo', 'NewProjectButton', 'RemoveProjectButton', 'ReorderProjectButton', 'SignInButton')
                        or widget.text() in ('Browse...', 'Update Usage Info', 'Check for Updates', 'Download Update')):
                    widget.setEnabled(False)
            from gui.page import Page
            window.pages[Page.SELECTION].execute_sync_button.setEnabled(False)
            window.pages[Page.SETTINGS].update_button.setEnabled(False)
        from core.signal_manager import signal_manager
        signal_manager.enable_buttons.connect(restrict_controls)
        signal_manager.upload_card_clicked.connect(restrict_controls)
        signal_manager.download_card_clicked.connect(restrict_controls)
        signal_manager.latest_app_version.connect(restrict_controls)
        restrict_controls()
        window.show()
        result = {'mode': 'offline-development', 'actual_gui': type(window).__module__ + '.' + type(window).__name__,
                  'qt_platform': application.platformName(), 'synthetic_remote': bool(args.smoke),
                  'frozen': bool(getattr(sys, 'frozen', False)), 'source_loader': 'verified-source'}
        errors = []
        from build_resources import ALIASES
        missing = [name for name in ALIASES if (not QMovie(':/assets/' + name).isValid()
                   if name.endswith('.gif') else QPixmap(':/assets/' + name).isNull())]
        result['missing_placeholder_resources'] = missing
        rendered = window.grab()
        result['rendered_size'] = [rendered.width(), rendered.height()]
        if missing or rendered.isNull():
            errors.append('Development resource/render validation failed')
        if args.smoke:
            project = working / 'fixture'
            project.mkdir()
            (project / 'part.txt').write_text('synthetic CAD placeholder; no real design data\n')
            controller.project_name = 'fixture'
            controller.project_id = 'offline-fixture'
            controller.project_read_only = False
            deadline = time.monotonic() + 15
            outcomes = {'cancelled': [], 'failed': [], 'succeeded': []}
            controller.sync_analyzer.analysis_cancelled.connect(lambda generation: outcomes['cancelled'].append(generation))
            controller.sync_analyzer.analysis_failed.connect(lambda generation, message: outcomes['failed'].append(generation))
            controller.sync_analyzer.analysis_succeeded.connect(lambda generation, *a: outcomes['succeeded'].append(generation))
            writes = []
            real_write = controller.db_manager.write_local_items
            def record_write(items):
                writes.append(controller.sync_analyzer._generation)
                return real_write(items)
            controller.db_manager.write_local_items = record_write
            real_warning = QMessageBox.warning
            def auto_warning(*a, **k):
                def close_modal():
                    modal = QApplication.activeModalWidget()
                    if modal is not None:
                        modal.accept()
                QTimer.singleShot(0, close_modal)
                return real_warning(*a, **k)
            QMessageBox.warning = auto_warning
            phase = [0]
            def check():
                if args.scenario == 'close-pending' and controller.drive_client.calls:
                    result['closed_pending_scan'] = not controller.analysis_ready
                    result['outcomes'] = outcomes
                    result['database_write_generations'] = writes
                    window.close()
                    return
                if args.scenario == 'lifecycle':
                    if phase[0] == 0 and controller.drive_client.calls:
                        controller.sync_analyzer.cancel_sync_analysis()
                        controller.on_refresh_clicked()
                        phase[0] = 1
                    elif phase[0] == 1 and outcomes['failed']:
                        controller.on_refresh_clicked()
                        old_id, old_success, old_error = controller.drive_client.calls[0]
                        old_success(old_id, [])
                        old_error(old_id, 'Late obsolete error')
                        phase[0] = 2
                if controller.analysis_ready:
                    result['scan_ready'] = True
                    result['outcomes'] = outcomes
                    result['database_write_generations'] = writes
                    if args.scenario == 'lifecycle' and (len(outcomes['cancelled']) != 1 or len(outcomes['failed']) != 1 or len(outcomes['succeeded']) != 1 or len(writes) != 1):
                        errors.append('Lifecycle isolation invariant failed')
                    result['planned_uploads'] = len(controller.upload_actions)
                    result['planned_paths'] = [str(action.path) for action in controller.upload_actions]
                    signal_manager.upload_card_clicked.emit()
                    from gui.page import Page
                    result['transfer_button_enabled'] = window.pages[Page.SELECTION].execute_sync_button.isEnabled()
                    if result['transfer_button_enabled']:
                        errors.append('Offline transfer button became enabled')
                    if result['planned_paths'] != ['part.txt']:
                        errors.append('Unexpected synthetic scan plan')
                    application.quit()
                elif time.monotonic() > deadline:
                    errors.append('Actual Qt scan did not complete before deadline')
                    application.quit()
                else:
                    QTimer.singleShot(20, check)
            QTimer.singleShot(0, controller._begin_sync_analysis)
            QTimer.singleShot(20, check)
        exit_code = application.exec()
        if hasattr(controller, 'sync_analyzer'):
            controller.sync_analyzer.cancel_sync_analysis()
        if not thread_pool.waitForDone(5000) or not network_pool.waitForDone(5000):
            print('Offline workers did not terminate; refusing normal cleanup', file=sys.stderr)
            os._exit(2)  # Preserve disposable profile rather than delete under live workers.
        application.processEvents()
        from shiboken6 import isValid
        if isValid(window.tray_icon):
            window.tray_icon.hide()
            window.tray_icon.deleteLater()
        if isValid(window):
            window.deleteLater()
        application.processEvents()
        if hasattr(controller, 'db_manager'):
            controller.db_manager.conn.close()
        result['forbidden_modules_loaded'] = [name for name in FORBIDDEN_MODULES if name in sys.modules]
        result['external_attempts'] = attempts
        result['active_threads'] = thread_pool.activeThreadCount() + network_pool.activeThreadCount()
        result['credential_files'] = [p.name for p in profile.rglob('*') if p.name in ('token.json', 'creds.json', 'temp.json')]
        if result['forbidden_modules_loaded'] or attempts or result['active_threads'] or result['credential_files']:
            errors.append('Offline safety invariant failed')
        if args.smoke and args.scenario == 'close-pending':
            if not result.get('closed_pending_scan') or result['database_write_generations'] or len(result['outcomes']['cancelled']) != 1:
                errors.append('Close-pending scan was not cancelled cleanly')
        result['slot_errors'] = slot_errors
        result['qt_warnings'] = qt_messages
        if slot_errors:
            errors.append('Unhandled Qt callback exception')
        result['errors'] = errors
        print('CACHECAD_OFFLINE_RESULT=' + json.dumps(result, sort_keys=True))
        return 1 if errors else exit_code


if __name__ == '__main__':
    raise SystemExit(main())
