import sys
import os
from pathlib import Path
import logging
import re
import traceback
import shutil
from utils.app_settings import settings
from utils.paths import BASE_DIR
from gui.page import Page
from core.signal_manager import signal_manager
from PySide6.QtCore import Qt
from core.local_client import LocalClient
from core.sync_analyzer import SyncAnalyzer
from core.sync_executor import SyncExecutor
from core.models import SyncActionType
from core.db_manager import DBManager
from utils.paths import CaseInsensitivePathDict
from gui.bug_report_dialog import BugReportDialog
from gui.initial_settings_dialog import select_working_directory, CacheCADUsageDialog
from PySide6.QtWidgets import QDialog
from PySide6.QtWidgets import QMessageBox
from utils.paths import is_valid_windows_path
from core.single_instance import SingleInstance
from app.app_version import APP_VERSION
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from core.project_manager import ProjectManager
logger = logging.getLogger(__name__)

from utils.path_advisory import confirm_project_location
from utils.development_mode import current as current_development_mode
from gui.destructive_confirmation import confirm_destructive_actions, selection_identity
from core.sync_consent import StaleSyncPlan, approve_sync_actions

class AppController:
    def __init__(self):
        self.development = current_development_mode()
        self.application = None
        self._shutting_down = False
        self._retired_databases = []
        try:
            if self.development is None:
                from core.auth_manager import AuthManager
                from utils.application_mode import current
                self.application = current()
                if self.application is None:
                    raise RuntimeError('Use application/launch.py for the real application')
                # The launcher holds a profile-wide OS lock before any state read.
                self.single = None
                self.auth_manager = AuthManager(self.application)
            else:
                self.single = None
                self.auth_manager = None
            self.creds = None
            self.user = None
            self.signed_in = False
            self.has_internet = False
            self.main_window = None
            self.project_name = None
            self.project_id = None
            self.project_read_only = False
            self.buttons_disabled = False
            self.upload_actions = None
            self.download_actions = None
            self.sync_executing = False
            self.analysis_ready = False
            self.path_dict = CaseInsensitivePathDict()
            self.upload_count = 0
            self.download_count = 0
            signal_manager.prompt_for_usage_data.connect(self.prompt_for_usage_data)
            signal_manager.new_install.connect(self.on_new_installation)
            signal_manager.change_working_directory.connect(self.on_change_working_directory)
            settings.load()
            self.working_directory = settings.get_working_directory()
            if self.development is not None:
                self.working_directory = self.development.require_profile_path(self.working_directory)
            self.project_manager = ProjectManager(self.working_directory)
            signal_manager.project_list_changed.connect(self.on_project_list_changed)
            signal_manager.project_read_only.connect(self.debug_slot)
        except Exception as e:
            tb = traceback.format_exc()
            logging.error('Application initialization failed')
            raise

    def set_main_window(self, window):
        self.main_window = window
        signal_manager.sign_in_clicked.connect(self.on_signin_clicked)
        signal_manager.auth_started.connect(self.on_auth_started)
        signal_manager.auth_success.connect(self.on_auth_success)
        signal_manager.auth_failed.connect(self.on_auth_failed)
        signal_manager.has_internet.connect(self.on_internet_check)
        signal_manager.project_list_item_activated.connect(self.project_selected)
        signal_manager.refresh_clicked.connect(self.on_refresh_clicked)
        signal_manager.options_clicked.connect(self.on_options_clicked)
        signal_manager.settings_clicked.connect(self.on_settings_clicked)
        signal_manager.bug_clicked.connect(self.on_bug_clicked)
        signal_manager.user_profile_loaded.connect(self.on_user_profile_loaded)
        signal_manager.upload_card_clicked.connect(self.on_upload_card_clicked)
        signal_manager.download_card_clicked.connect(self.on_download_card_clicked)
        signal_manager.back_clicked.connect(self.on_back_button_clicked)
        signal_manager.execute_sync.connect(self.on_execute_sync)
        signal_manager.dropped_actions.connect(self.on_dropped_actions)
        signal_manager.sync_execution_finished.connect(self.on_sync_execution_finished)
        signal_manager.download_app_update.connect(self.on_download_app_update)
        signal_manager.bug_report.connect(self.on_bug_report)
        signal_manager.disable_buttons.emit()
        self.buttons_disabled = True
        if self.development is not None:
            self.bug_report = None
            if self.development.drive_client_factory is not None:
                self._configure_offline_drive(self.development.drive_client_factory())
            return
        self.bug_report = None
        from gui.application_account import ApplicationAccount
        old_button = window.signin_button
        signal_manager.user_profile_loaded.disconnect(old_button.set_btn_to_signout)
        signal_manager.has_internet.disconnect(old_button.on_internet_check)
        row = old_button.parentWidget().layout().itemAt(0).layout()
        window.signin_button = ApplicationAccount(self, old_button.parentWidget())
        row.replaceWidget(old_button, window.signin_button)
        old_button.hide()
        old_button.deleteLater()
        self.auth_manager.status_changed.connect(self.on_account_status)
        signal_manager.sign_out.connect(self.on_session_signed_out)
        self._configure_owned_ui()
        if self.single is not None:
            def on_other_instance_message(msg):
                if msg == 'raise-window':
                    self.main_window.showNormal()
                    self.main_window.activateWindow()
                    self.main_window.raise_()
            self.single.messageReceived.connect(on_other_instance_message)
        from PySide6.QtWidgets import QApplication
        QApplication.instance().aboutToQuit.connect(self.shutdown)
        if not self.application.startup_check:
            self.auth_manager.restore()

    def _configure_offline_drive(self, drive_client):
        if self.development is None:
            raise RuntimeError('An offline fixture requires the explicit development launcher')
        if getattr(drive_client, 'is_offline_fixture', False) is not True:
            raise TypeError('Only an explicitly marked offline Drive fixture can be injected')
        self.drive_client = drive_client
        self.local_client = LocalClient()
        self.db_manager = DBManager()
        analyzer = SyncAnalyzer(self.drive_client, self.local_client, self.db_manager)
        self.sync_analyzer = analyzer
        analyzer.analysis_started.connect(lambda generation: self.on_scan_started(analyzer, generation))
        analyzer.analysis_succeeded.connect(lambda generation, downloads, uploads, paths: self.on_scan_succeeded(analyzer, generation, downloads, uploads, paths))
        analyzer.analysis_failed.connect(lambda generation, message: self.on_scan_failed(analyzer, generation, message))
        analyzer.analysis_cancelled.connect(lambda generation: self.on_scan_cancelled(analyzer, generation))
        self.signed_in = True
        self._clear_sync_plan()
        self.enable_buttons()

    def create_project_from_link(self, link, read_only=False):
        if getattr(self, 'development', None) is not None:
            return 'External projects are disabled in offline development. Use a local fixture.'
        if not self.signed_in or self.buttons_disabled:
            return 'Sign in to Google before creating a project.'
        logging.info(f'Creating project from link: {link}')
        link = link.strip()
        id = 'None'
        if re.fullmatch('[A-Za-z0-9_-]{10,}', link):
            id = link
        else:
            match = re.search('/folders/([A-Za-z0-9_-]+)', link)
            if match:
                id = match.group(1)
            else:
                id = link.rstrip('/').split('/')[-1].split('?')[0]
        logging.info(f'Extracted ID: {id} from link: {link}')
        if len(id) not in (33, 19):
            self.main_window.show_status_message('Invalid Google Drive folder ID.')
            logging.error(f'Invalid Google Drive folder ID: {id}')
            return
        project_name = self.drive_client.get_folder_name_from_id(id)
        logging.info(f'Project name extracted: {project_name}')
        if project_name == '':
            return 'Could not get folder name from Google Drive.'
        if any(id in x[1] for x in self.get_project_list()):
            return f'{project_name} already exists.'
        if not is_valid_windows_path(project_name):
            return f'{project_name} is not a valid folder name.'
        success = self.project_manager.create_project(project_name, id, str(read_only))
        return success

    def on_project_list_changed(self, project_list):
        self.main_window.project_list_widget.populate_projects(project_list)

    def get_project_list(self):
        return self.project_manager.get_projects()

    def project_selected(self, item):
        if getattr(self, 'development', None) is not None:
            if not hasattr(self, 'sync_analyzer'):
                return
        elif not self.auth_manager.check_creds():
            self.signed_in = False
            return
        if not self.signed_in or self.buttons_disabled or self.sync_executing:
            return
        project = item.data(Qt.UserRole)
        if not confirm_project_location(Path(self.working_directory) / project['name'], self.main_window):
            return
        self.project_name = project['name']
        self.project_id = project['id']
        self.project_read_only = project.get('read_only', False)
        self._begin_sync_analysis()
        signal_manager.page_change.emit(Page.PROJECT)

    def on_sync_analysis_finished(self, download_actions, upload_actions, path_dict):
        self.download_actions = download_actions
        self.upload_actions = upload_actions
        self.path_dict = path_dict
        self.analysis_ready = True
        self.enable_buttons()

    def on_refresh_clicked(self):
        if not self.signed_in or not self.project_id or self.sync_executing or self.buttons_disabled:
            return
        if not confirm_project_location(Path(self.working_directory) / self.project_name, self.main_window):
            return
        self._begin_sync_analysis()

    def _clear_sync_plan(self):
        self.analysis_ready = False
        self.download_actions = []
        self.upload_actions = []
        self.path_dict = CaseInsensitivePathDict()
        self.download_count = 0
        self.upload_count = 0
        signal_manager.sync_count_updated.emit(0, 0)
        signal_manager.update_sync_selection_treeview.emit([])

    def _begin_sync_analysis(self):
        if getattr(self, '_update_install_pending', False):
            return
        if getattr(self, 'development', None) is not None:
            self.development.require_profile_path(Path(self.working_directory) / self.project_name)
        self._clear_sync_plan()
        self.disable_buttons()
        self.sync_analyzer.start_sync_analysis(self.project_name, self.project_id, self.working_directory, background=False, read_only=self.project_read_only)

    def _scan_is_current(self, analyzer, generation):
        return analyzer is self.sync_analyzer and generation == analyzer._generation

    def _settle_scan(self, analyzer, generation):
        ticket = (analyzer, generation)
        if not self._scan_is_current(analyzer, generation) or getattr(self, '_settled_scan', None) == ticket:
            return False
        self._settled_scan = ticket
        return True

    def on_scan_started(self, analyzer, generation):
        if not self._scan_is_current(analyzer, generation) or analyzer._settled:
            return
        self._clear_sync_plan()
        self.disable_buttons()

    def on_scan_succeeded(self, analyzer, generation, download_actions, upload_actions, path_dict):
        if not self._scan_is_current(analyzer, generation) or analyzer._cancelled:
            return
        if self._settle_scan(analyzer, generation):
            self.on_sync_analysis_finished(download_actions, upload_actions, path_dict)

    def on_scan_failed(self, analyzer, generation, message):
        if not self._settle_scan(analyzer, generation):
            return
        self._clear_sync_plan()
        self.enable_buttons()
        signal_manager.error_occurred.emit('Sync analysis failed. Refresh to retry. ' + message)
        QMessageBox.warning(self.main_window, 'Sync analysis failed', 'The scan did not complete. No sync actions are available. Refresh to retry.\n\n' + message)

    def on_scan_cancelled(self, analyzer, generation):
        if not self._settle_scan(analyzer, generation):
            return
        self._clear_sync_plan()
        self.enable_buttons()

    def on_options_clicked(self):
        self.show_transfer_results()

    def show_transfer_results(self):
        if self.main_window is None or getattr(self, '_shutting_down', False):
            return
        from gui.transfer_results import TransferResultsDialog
        dialog = TransferResultsDialog(getattr(self, 'last_transfer_results', ()), self.main_window,
                                       context=getattr(self, 'last_transfer_context', {}))
        dialog.show()
        self._transfer_results_dialog = dialog

    def on_settings_clicked(self):
        signal_manager.page_change.emit(Page.SETTINGS)

    def on_bug_clicked(self):
        if getattr(self, 'development', None) is not None:
            return
        self.main_window.show_status_message('Remote diagnostics are unavailable in this build. No report was sent.', 10000)

    def on_bug_report(self, idx, report):
        if getattr(self, 'development', None) is not None:
            return
        self.on_bug_clicked()

    def enable_buttons(self):
        logging.debug('emitting enable buttons')
        signal_manager.enable_buttons.emit()
        self.buttons_disabled = False

    def disable_buttons(self):
        logging.debug('emitting disable buttons')
        signal_manager.disable_buttons.emit()
        self.buttons_disabled = True

    def get_working_directory(self) -> Path:
        return self.working_directory

    def on_signin_clicked(self):
        if getattr(self, '_update_install_pending', False):
            return
        if getattr(self, 'development', None) is not None:
            return
        if self.auth_manager.state == 'cleanup_required':
            self.auth_manager.sign_out()
        elif not self.signed_in and not self.auth_manager.check_creds():
            self.auth_manager.authorize()

    def user_clicked_sign_in(self):
        if getattr(self, 'development', None) is not None:
            return
        self.on_signin_clicked()

    def on_auth_started(self):
        if getattr(self, 'development', None) is not None:
            return
        self.disable_buttons()

    def on_auth_success(self, creds):
        if getattr(self, 'development', None) is not None:
            return
        if self._shutting_down or creds is not self.auth_manager.get_creds() or self.auth_manager.identity is None:
            return
        self._retire_sync_clients()
        self.creds = creds
        from core.drive_client import DriveClient
        try:
            client = DriveClient(creds)
            if client.service is None:
                raise RuntimeError('Drive service unavailable')
        except Exception:
            self.signed_in = False
            self.disable_buttons()
            self.main_window.show_status_message('Signed in, but Drive could not start. Sign out and retry.', 15000)
            return
        # DriveClient may process Qt events while its service is constructed.
        if self._shutting_down or creds is not self.auth_manager.get_creds():
            client.close()
            return
        self.drive_client = client
        self.local_client = LocalClient()
        self.db_manager = DBManager()
        analyzer = SyncAnalyzer(self.drive_client, self.local_client, self.db_manager)
        self.sync_analyzer = analyzer
        analyzer.analysis_started.connect(lambda generation: self.on_scan_started(analyzer, generation))
        analyzer.analysis_succeeded.connect(lambda generation, downloads, uploads, paths: self.on_scan_succeeded(analyzer, generation, downloads, uploads, paths))
        analyzer.analysis_failed.connect(lambda generation, message: self.on_scan_failed(analyzer, generation, message))
        analyzer.analysis_cancelled.connect(lambda generation: self.on_scan_cancelled(analyzer, generation))
        self.user = {'email': self.auth_manager.identity.email, 'sub': self.auth_manager.identity.sub}
        self.signed_in = True
        self._clear_sync_plan()
        self.enable_buttons()

    def on_user_profile_loaded(self, user_data: dict, args):
        if getattr(self, 'development', None) is not None:
            return
        # Identity comes only from the validated ID token, never profile-image URLs.

    def on_auth_failed(self, error_msg):
        if getattr(self, 'development', None) is not None:
            return
        self.main_window.show_status_message(error_msg, 12000)
        if not self.auth_manager.check_creds():
            self.disable_buttons()

    def on_internet_check(self, has_internet):
        self.has_internet = bool(has_internet)

    def on_upload_card_clicked(self):
        if not self.analysis_ready or self.buttons_disabled:
            return
        signal_manager.update_sync_selection_treeview.emit(self.upload_actions)
        if self.project_read_only == 'True' or self.project_read_only == True:
            signal_manager.project_read_only.emit()
        signal_manager.page_change.emit(Page.SELECTION)

    def on_download_card_clicked(self):
        if not self.analysis_ready or self.buttons_disabled:
            return
        signal_manager.update_sync_selection_treeview.emit(self.download_actions)
        signal_manager.page_change.emit(Page.SELECTION)

    def on_back_button_clicked(self):
        logging.info('Back button clicked, go to project page')
        print('Back button clicked, go to project page')
        if self.sync_executing:
            signal_manager.sync_cancelled.emit()
            logging.info('Sync cancelled by user.')
            # Keep account switching blocked until the executor's terminal result.
        else:
            if getattr(self, 'sync_analyzer', None) is None:
                return
            if not self.sync_analyzer._settled:
                self.sync_analyzer.cancel_sync_analysis()
            signal_manager.page_change.emit(Page.PROJECT)

    def on_execute_sync(self, action_list):
        if getattr(self, '_update_install_pending', False):
            return
        if getattr(self, 'development', None) is not None:
            return
        # A modal dialog runs a nested Qt loop. A second click or queued signal
        # must not open another dialog or submit the same selection twice.
        if getattr(self, '_sync_confirmation_pending', False):
            return
        if not self.signed_in or not self.analysis_ready or self.sync_executing or self.buttons_disabled:
            if not self.sync_executing:
                self._reset_sync_selection()
            return
        self._sync_confirmation_pending = True
        try:
            selection = tuple(action_list)
            if not selection:
                return
            page = self.main_window.pages[Page.SELECTION]
            if self.main_window.main_stack.currentWidget() is not page:
                return
            identity = selection_identity(selection)
            if selection_identity(page.selection_tree.get_selected_actions()) != identity:
                raise StaleSyncPlan('the selected files changed')
            planned = {id(action) for action in self.upload_actions + self.download_actions}
            if any(id(action) not in planned for _, action in selection):
                raise StaleSyncPlan('the selected files no longer belong to the current scan')
            if self.project_read_only in (True, 'True') and any(
                    action.action.value in ('upload_new', 'upload_revision', 'upload_unknown', 'delete_cloud')
                    for _, action in selection):
                raise StaleSyncPlan('this project is read-only; remote files cannot be changed')
            analyzer = self.sync_analyzer
            snapshot = getattr(analyzer, 'observed_snapshot', None)
            if snapshot is None or analyzer._cancelled or not analyzer._settled:
                raise StaleSyncPlan('the scan has no verified file snapshot; use Refresh to retry')
            context = self._sync_confirmation_context()
            if not confirm_destructive_actions(self.main_window, selection, self.project_name,
                                               self.project_id, self.working_directory):
                return
            # Never bind approval to a replacement selection, project, session,
            # analyzer or generation that arrived while the dialog was open.
            if (not self.signed_in or not self.analysis_ready or self.sync_executing
                    or self.buttons_disabled or getattr(self, '_shutting_down', False)
                    or self._sync_confirmation_context() != context
                    or self.main_window.main_stack.currentWidget() is not page
                    or selection_identity(action_list) != identity
                    or selection_identity(page.selection_tree.get_selected_actions()) != identity):
                raise StaleSyncPlan('the project, scan or selection changed while the warning was open')
            consent = approve_sync_actions(selection, snapshot)
            self.disable_buttons()
            self.sync_executing = True
            self._executor_unsettled = True
            self.analysis_ready = False
            try:
                self.sync_executor = SyncExecutor(self.drive_client, self.local_client,
                    self.project_name, self.project_id, self.working_directory,
                    selection, self.path_dict, consent=consent)
                # Executor startup is deferred until the next Qt turn, so this
                # reviewed code-only error channel is connected before any work.
                self.sync_executor.guard_failed.connect(self.on_sync_guard_failed, Qt.QueuedConnection)
            except Exception:
                self.sync_executing = False
                self._executor_unsettled = False
                self._clear_sync_plan()
                self.enable_buttons()
                raise
        except Exception as error:
            message = (str(error) if isinstance(error, StaleSyncPlan) else
                       'The scan or selection could not be verified. Refresh, review the files and try again.')
            self.main_window.show_status_message('Sync did not start. ' + message, 15000)
        finally:
            self._sync_confirmation_pending = False
            if not self.sync_executing:
                self._reset_sync_selection()

    def _sync_confirmation_context(self):
        analyzer = self.sync_analyzer
        return (id(analyzer), getattr(analyzer, '_generation', None),
                id(getattr(analyzer, 'observed_snapshot', None)),
                getattr(analyzer, '_cancelled', True), getattr(analyzer, '_settled', False),
                self.project_name, self.project_id, str(self.working_directory),
                self.project_read_only, id(self.drive_client), id(self.local_client),
                id(self.path_dict), tuple(sorted(self.path_dict.items())),
                tuple(id(action) for action in self.upload_actions),
                tuple(id(action) for action in self.download_actions))

    def _reset_sync_selection(self):
        # The recovered selection page shows progress before emitting execute.
        # Cancelling consent is not a finished transfer and must not trigger a scan.
        window = self.main_window
        if window is None or not hasattr(window, 'pages'):
            return
        page = window.pages.get(Page.SELECTION)
        if page is not None:
            page.sync_started = False
            page.hide_progress_bar()
            page.set_back_button_to_back()

    def on_sync_guard_failed(self, code):
        if getattr(self, '_shutting_down', False) or self.main_window is None:
            return
        messages = {
            'outcome_unknown': 'A transfer may have changed a file, but its final outcome is unknown. '
                               'Do not repeat it. Open transfer results and review the local recovery record.',
            'recovery_required': 'An earlier transfer needs recovery review before this file can be changed again. '
                                 'Open transfer results or the project .cachecad/recovery folder.',
            'committed_recording_failed': 'A file change completed, but its local recording is incomplete. '
                                         'Do not repeat the transfer. Review transfer results and recovery records.',
            'stale': 'Sync stopped because a file or folder changed after the scan. '
                     'Refresh, review the new versions and confirm again. Some earlier files may already have changed.',
            'download_failed': 'A local transfer could not finish. Review transfer results and any retained '
                               'recovery record before retrying. Recovery is not guaranteed.',
        }
        message = messages.get(code, 'A sync operation could not finish. Refresh and review the project '
                                     'before retrying; some files may already have changed.')
        self.main_window.show_status_message(message, 20000)

    def on_dropped_actions(self, dropped_actions):
        logging.info(f'{str(len(dropped_actions))} dropped actions received at app controller')
        if len(dropped_actions) > 0:
            signal_manager.dropped_actions_acknowledged.emit()
        else:
            signal_manager.dropped_actions_acknowledged.emit()

    def on_sync_execution_finished(self):
        self._executor_unsettled = False
        self.sync_executing = False
        if getattr(self, '_shutting_down', False) or not self.signed_in:
            return
        executor = getattr(self, 'sync_executor', None)
        self.last_transfer_results = tuple(dict(item) for item in getattr(executor, 'action_results', ()))
        self.last_transfer_context = {
            'project_name': str(getattr(executor, 'project_name', 'Unavailable')),
            'project_id': str(getattr(executor, 'project_id', 'Unavailable')),
            'project_root': str(Path(getattr(executor, 'working_directory', '.')) /
                                str(getattr(executor, 'project_name', 'Unavailable'))),
        }
        if self.last_transfer_results:
            self.show_transfer_results()
        self._begin_sync_analysis()
        signal_manager.page_change.emit(Page.PROJECT)

    def on_change_working_directory(self, new_directory: Path, old_directory: Path, move_projects: bool = False):
        if getattr(self, 'development', None) is not None:
            return
        settings.set_working_directory(new_directory)
        self.working_directory = settings.get_working_directory()
        self.project_manager.working_dir = self.working_directory
        logging.info(f'Working directory changed to: {self.working_directory}')

    def on_new_installation(self):
        if getattr(self, 'development', None) is not None:
            return
        # First launch stays signed out. Folder choice remains in Settings.

    def prompt_for_usage_data(self):
        if getattr(self, 'development', None) is not None:
            return
        # This derivative has no usage-data collection service.

    def on_download_app_update(self):
        if getattr(self, 'development', None) is not None:
            return
        if getattr(self, '_update_install_pending', False):
            return
        previous = getattr(self, '_update_dialog', None)
        if previous is not None:
            from shiboken6 import isValid
            if isValid(previous) and not getattr(previous, '_closed', False):
                previous.show()
                previous.raise_()
                return
            if isValid(previous):
                if not previous.shutdown(0):
                    self.main_window.show_status_message('The previous update operation is still stopping. Try again shortly.', 10000)
                    return
                previous.deleteLater()
        from core.application_updates import UpdateService, InstalledRelease, load_update_config
        from core.windows_update_install import SignerPolicy, VerifiedInstaller
        from gui.application_updates import ApplicationUpdateDialog
        from app.app_version import APP_PRODUCT_ID, APP_RELEASE_NUMBER, APP_BUILD_DATE
        source_root = Path(__file__).resolve().parents[2]
        try:
            config = load_update_config(source_root / 'application')
            installed = InstalledRelease(product_id=APP_PRODUCT_ID, version=APP_VERSION,
                build=APP_RELEASE_NUMBER, build_date=APP_BUILD_DATE, architecture='x86_64', profile_schema=1,
                updater_version=1, windows_build=sys.getwindowsversion().build if sys.platform == 'win32' else 0)
            service = UpdateService(config, self.application.profile_root / 'updates', installed)
        except Exception:
            self.main_window.show_status_message('The owner update configuration could not be verified. Reinstall from an approved distributor.', 10000)
            return
        def verify(download, cancel):
            if cancel.is_set():
                raise RuntimeError('cancelled')
            policy = SignerPolicy(frozenset(config.publisher_certificate_sha256))
            verified = VerifiedInstaller.open(download.path, expected_sha256=download.offer.sha256,
                expected_length=download.offer.length, policy=policy)
            if cancel.is_set():
                verified.close()
                raise RuntimeError('cancelled')
            from types import SimpleNamespace
            return SimpleNamespace(download=download, close=verified.close,
                signer_certificate_sha256=verified.signer_certificate_sha256)
        self._update_configuration = config
        self._update_installed_release = installed
        dialog = ApplicationUpdateDialog(service, verify_download=verify,
            request_install=self._request_update_install, installed_version=APP_VERSION,
            parent=self.main_window)
        self._update_dialog = dialog
        dialog.show()

    def _update_busy_reason(self):
        if getattr(self, '_shutting_down', False):
            return 'The application is already closing. Reopen it before updating.'
        if self.sync_executing or getattr(self, '_executor_unsettled', False):
            return 'Wait for the current transfer to finish before installing an update.'
        analyzer = getattr(self, 'sync_analyzer', None)
        if analyzer is not None and (not getattr(analyzer, '_settled', True) or analyzer.is_background_running()):
            return 'Wait for the project scan to finish before installing an update.'
        from core.thread_manager import thread_pool, network_pool
        if thread_pool.activeThreadCount() or network_pool.activeThreadCount():
            return 'Background work is still settling. Try installing again when it finishes.'
        manager = getattr(self, 'auth_manager', None)
        thread = getattr(manager, '_thread', None)
        if (getattr(manager, '_auth_in_progress', False) or
                thread is not None and thread.is_alive()):
            return 'Wait for the account operation to finish before installing an update.'
        return None

    def _request_update_install(self, verified):
        reason = self._update_busy_reason()
        if reason:
            return reason
        if getattr(self, '_update_install_pending', False):
            return 'An update handoff is already in progress.'
        if not getattr(sys, 'frozen', False) or sys.platform != 'win32':
            return 'Install updates from the packaged Windows application.'
        from core.windows_update_install import start_update_handoff
        from gui.update_install_coordinator import UpdateInstallCoordinator
        from PySide6.QtWidgets import QApplication
        self._update_install_pending = True
        self._clear_sync_plan()
        self.disable_buttons()
        self.main_window.setEnabled(False)
        def prepare(cancel):
            try:
                return start_update_handoff(verified.download, self._update_configuration,
                    Path(sys.executable).parent, self.application.profile_root,
                    cache_dir=self.application.profile_root / 'updates', old_pid=os.getpid(), cancel=cancel)
            finally:
                verified.close()
        coordinator = UpdateInstallCoordinator(prepare, self._update_busy_reason, self.main_window)
        self._update_coordinator = coordinator
        def failed(message):
            self._update_install_pending = False
            self.main_window.setEnabled(True)
            self.main_window.show_status_message(message, 15000)
            dialog = getattr(self, '_update_dialog', None)
            if dialog is not None:
                from shiboken6 import isValid
                if isValid(dialog):
                    dialog.handoff_failed(message)
            if self.signed_in:
                self.enable_buttons()
        coordinator.failed.connect(failed)
        coordinator.ready_to_exit.connect(QApplication.instance().quit)
        if not coordinator.start():
            self._update_install_pending = False
            self.main_window.setEnabled(True)
            return 'The application could not prepare this update. Try again later.'
        return None

    def on_error_occurred(self, error_str):
        pass

    def on_user_validation_data_fetched(self, banned_domains: set, banned_user_hashes: set):
        # Vendor account bans and licensing services are not used by this app.
        return

    def show_banned_user_dialog(self):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(None, 'User Being Logged Out', 'Your access to CacheCAD has been disabled. If you believe this is a mistake, please contact support.')

    def debug_slot(*args, **kwargs):
        logging.debug('Signal received with args:', args, kwargs)
        tb = ''.join(traceback.format_stack())
        logging.debug(tb)

    def _configure_owned_ui(self):
        from PySide6.QtWidgets import QTextBrowser, QLabel, QPushButton
        window = self.main_window
        window.setWindowTitle('CacheCAD Reconstructed')
        window.sponsor_label.hide()
        window.sponsor_logo.hide()
        window.bug_btn.setToolTip('Remote diagnostics are unavailable; no report is sent')
        for browser in window.pages[Page.HOME].findChildren(QTextBrowser):
            browser.setOpenExternalLinks(False)
            browser.setOpenLinks(False)
            browser.setPlainText('CacheCAD Reconstructed connects your local project folders to Google Drive. '
                'Sign in with Google to select a Drive folder, compare files and choose transfers. '
                'Google permission is requested only when you click Sign in. '
                'Saved sessions are encrypted for your Windows account. '
                'Updates require your confirmation; crash reports stay on this computer. '
                'Review each sync plan before changing your files.')
        page = window.pages[Page.SETTINGS]
        page.latest_version_label.setText('Updates are checked only when you ask.')
        page.update_button.setText('Check for Updates')
        page.update_button.setEnabled(True)
        for label in page.findChildren(QLabel):
            if label.text().startswith(('Update user data:', 'This is the only info collected')):
                label.hide()
        for button in page.findChildren(QPushButton):
            if button.text() == 'Update Usage Info':
                button.hide()
        window.settings_btn.setEnabled(True)

    def on_account_status(self, state, message):
        if state in ('authorize', 'restore', 'sign_out', 'disconnect', 'cancelling'):
            self.disable_buttons()
        elif state == 'signed_in' and self.signed_in and not self.sync_executing:
            self.enable_buttons()
        if self.main_window is not None:
            self.main_window.settings_btn.setEnabled(not self.sync_executing)

    def _retire_sync_clients(self):
        if getattr(self, 'sync_analyzer', None) is not None:
            self.sync_analyzer.cancel_sync_analysis()
            self.sync_analyzer = None
        if hasattr(self, 'drive_client'):
            self.drive_client.close()
            for signal, slot in ((signal_manager.token_refreshed, self.drive_client.on_token_refresh),
                                 (signal_manager.sync_execution_finished, self.drive_client.on_sync_finished),
                                 (signal_manager.sync_cancelled, self.drive_client.on_sync_cancelled)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            del self.drive_client
        if hasattr(self, 'db_manager'):
            self._retired_databases.append(self.db_manager)
            del self.db_manager
        self.signed_in = False

    def on_session_signed_out(self):
        self.last_transfer_results = ()
        self.last_transfer_context = {}
        previous_report = getattr(self, '_transfer_results_dialog', None)
        if previous_report is not None:
            try:
                previous_report.close()
            except RuntimeError:
                pass  # Qt may already have deleted the closed report.
        self._transfer_results_dialog = None
        if self.auth_manager.last_invalidation is not None and (self.sync_executing or hasattr(self, 'sync_executor')):
            self.auth_manager.require_restart()
        if self.sync_executing:
            signal_manager.sync_cancelled.emit()
            self.sync_executing = False
        self._retire_sync_clients()
        self.creds = self.user = None
        self.project_name = self.project_id = None
        self._clear_sync_plan()
        self.disable_buttons()
        self.main_window.settings_btn.setEnabled(True)
        signal_manager.page_change.emit(Page.HOME)

    def on_local_signout(self):
        if getattr(self, '_update_install_pending', False):
            return
        if self.sync_executing or getattr(self, '_executor_unsettled', False):
            self.main_window.show_status_message('Wait for the current transfer to finish before signing out.', 10000)
            return
        if getattr(self, 'sync_analyzer', None) is not None:
            self.sync_analyzer.cancel_sync_analysis()
        self.auth_manager.sign_out()

    def on_disconnect_clicked(self):
        if getattr(self, '_update_install_pending', False):
            return
        if self.sync_executing or getattr(self, '_executor_unsettled', False):
            self.main_window.show_status_message('Wait for the current transfer to finish before disconnecting.', 10000)
            return
        from gui.application_account import confirm_disconnect
        if confirm_disconnect(self.main_window):
            if getattr(self, 'sync_analyzer', None) is not None:
                self.sync_analyzer.cancel_sync_analysis()
            self.auth_manager.disconnect()

    def shutdown(self):
        update_stopped = True
        coordinator = getattr(self, '_update_coordinator', None)
        if coordinator is not None:
            update_stopped = coordinator.shutdown()
        dialog = getattr(self, '_update_dialog', None)
        if dialog is not None:
            from shiboken6 import isValid
            if isValid(dialog):
                update_stopped = dialog.shutdown() and update_stopped
        self._shutting_down = True
        if self.sync_executing:
            signal_manager.sync_cancelled.emit()
        if getattr(self, 'sync_analyzer', None) is not None:
            self.sync_analyzer.cancel_sync_analysis()
        if getattr(self, 'development', None) is None and hasattr(self, 'drive_client'):
            self.drive_client.close()
        self.signed_in = False
        self.creds = self.user = None
        return (self.auth_manager.shutdown() if self.auth_manager is not None else True) and update_stopped
