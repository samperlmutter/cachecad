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

class AppController:
    def __init__(self):
        self.development = current_development_mode()
        try:
            if self.development is None:
                from core.auth_manager import AuthManager
                self.single = SingleInstance('CacheCAD ' + APP_VERSION)
                if not self.single.is_primary:
                    sys.exit(0)
                self.auth_manager = AuthManager()
            else:
                self.single = None
                self.auth_manager = None
            self.creds = None
            self.user = None
            self.signed_in = False
            self.has_internet = self.development is None
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
            signal_manager.user_validation_data_fetched.connect(self.on_user_validation_data_fetched)
            signal_manager.project_read_only.connect(self.debug_slot)
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f'Error initializing AppController: {e}\n{tb}')
            if self.development is not None:
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
        if self.auth_manager.check_creds():
            self.creds = self.auth_manager.get_creds()
            signal_manager.auth_success.emit(self.creds)
        from core.bug_report import BugReport
        self.bug_report = BugReport()
        def on_other_instance_message(msg):
            if msg == 'raise-window':
                self.main_window.showNormal()
                self.main_window.activateWindow()
                self.main_window.raise_()
        self.single.messageReceived.connect(on_other_instance_message)

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
            self.auth_manager.refresh_token()
            if not self.auth_manager.check_creds():
                self.signed_in = False
                return
        if self.buttons_disabled or self.sync_executing:
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
        pass

    def on_settings_clicked(self):
        logging.info('Settings button clicked')
        signal_manager.page_change.emit(Page.SETTINGS)
        if getattr(self, 'development', None) is not None:
            return
        from core.github_updater import github_updater
        signal_manager.latest_app_version.emit(github_updater.get_latest_release_info())

    def on_bug_clicked(self):
        if getattr(self, 'development', None) is not None:
            return
        logging.info('Bug Report Clicked')
        dlg = BugReportDialog(self)
        dlg.exec()

    def on_bug_report(self, idx, report):
        if getattr(self, 'development', None) is not None:
            return
        project, _ = self.get_project_list()[idx]
        project_path = Path(self.working_directory) / project
        self.bug_report.new_bug_report(project_path, report, self.user)

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
        if getattr(self, 'development', None) is not None:
            return
        if not self.signed_in:
            self.auth_manager.authorize()
        else:
            self.auth_manager.sign_out()
            self.signed_in = False
            signal_manager.disable_buttons.emit()
            self.buttons_disabled = True

    def user_clicked_sign_in(self):
        if getattr(self, 'development', None) is not None:
            return
        print('Sign-in button clicked')
        logging.info('Sign-in button clicked')
        self.auth_manager.authorize()

    def on_auth_started(self):
        print('Auth started... show spinner or loading UI')
        logging.info('Auth started... show spinner or loading UI')

    def on_auth_success(self, creds):
        if getattr(self, 'development', None) is not None:
            return
        if hasattr(self, 'sync_analyzer'):
            self.sync_analyzer.cancel_sync_analysis()
        self.signed_in = True
        self.creds = creds
        from core.drive_client import DriveClient
        self.drive_client = DriveClient(self.creds)
        self.local_client = LocalClient()
        self.drive_client.get_user_profile()
        self.db_manager = DBManager()
        analyzer = SyncAnalyzer(self.drive_client, self.local_client, self.db_manager)
        self.sync_analyzer = analyzer
        analyzer.analysis_started.connect(lambda generation: self.on_scan_started(analyzer, generation))
        analyzer.analysis_succeeded.connect(lambda generation, downloads, uploads, paths: self.on_scan_succeeded(analyzer, generation, downloads, uploads, paths))
        analyzer.analysis_failed.connect(lambda generation, message: self.on_scan_failed(analyzer, generation, message))
        analyzer.analysis_cancelled.connect(lambda generation: self.on_scan_cancelled(analyzer, generation))
        self._clear_sync_plan()
        self.enable_buttons()

    def on_user_profile_loaded(self, user_data: dict, args):
        if getattr(self, 'development', None) is not None:
            return
        self.user = user_data
        from core.user_validation import UserValidation
        self.user_validation = UserValidation()

    def on_auth_failed(self, error_msg):
        logging.error('Auth failed: %s', error_msg)
        print('Auth failed:', error_msg)

    def on_internet_check(self, has_internet):
        if not has_internet:
            self.creds = None
            self.signed_in = False

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
            self.sync_executing = False
        else:
            if not self.sync_analyzer._settled:
                self.sync_analyzer.cancel_sync_analysis()
            signal_manager.page_change.emit(Page.PROJECT)

    def on_execute_sync(self, action_list):
        if getattr(self, 'development', None) is not None:
            return
        if not self.analysis_ready or self.sync_executing or self.buttons_disabled:
            return
        self.disable_buttons()
        self.sync_executing = True
        self.analysis_ready = False
        self.sync_executor = SyncExecutor(self.drive_client, self.local_client, self.project_name, self.project_id, self.working_directory, action_list, self.path_dict)

    def on_dropped_actions(self, dropped_actions):
        logging.info(f'{str(len(dropped_actions))} dropped actions received at app controller')
        if len(dropped_actions) > 0:
            signal_manager.dropped_actions_acknowledged.emit()
        else:
            signal_manager.dropped_actions_acknowledged.emit()

    def on_sync_execution_finished(self):
        self.sync_executing = False
        self._begin_sync_analysis()
        signal_manager.page_change.emit(Page.PROJECT)

    def on_change_working_directory(self, new_directory: Path, old_directory: Path, move_projects: bool = False):
        if getattr(self, 'development', None) is not None:
            return
        settings.set_working_directory(new_directory)
        self.working_directory = settings.get_working_directory()
        logging.info(f'Working directory changed to: {self.working_directory}')

    def on_new_installation(self):
        if getattr(self, 'development', None) is not None:
            return
        logging.info('New installation detected - running initial settings dialog')
        select_working_directory()

    def prompt_for_usage_data(self):
        if getattr(self, 'development', None) is not None:
            return
        dlg = CacheCADUsageDialog()
        if dlg.exec() == QDialog.Accepted:
            print(dlg.get_results())

    def on_download_app_update(self):
        if getattr(self, 'development', None) is not None:
            return
        from core.github_updater import github_updater
        if github_updater.download_latest_version():
            downloads = Path.home() / 'Downloads'
            os.startfile(downloads)

    def on_error_occurred(self, error_str):
        pass

    def on_user_validation_data_fetched(self, banned_domains: set, banned_user_hashes: set):
        logging.info('User validation data fetched!')
        user_email_hash = self.user.get('email_hash', '')
        user_email_doimain = self.user.get('email_domain', '')
        if user_email_hash in banned_user_hashes or user_email_doimain in banned_domains:
            logging.error(f"Banned user attempted to use the app: {self.user.get('email', '')}")
            self.show_banned_user_dialog()
            signal_manager.sign_in_clicked.emit()
            return

    def show_banned_user_dialog(self):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(None, 'User Being Logged Out', 'Your access to CacheCAD has been disabled. If you believe this is a mistake, please contact support.')

    def debug_slot(*args, **kwargs):
        logging.debug('Signal received with args:', args, kwargs)
        tb = ''.join(traceback.format_stack())
        logging.debug(tb)
