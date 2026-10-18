from PySide6.QtCore import QObject, Signal, QModelIndex
from PySide6.QtWidgets import QListWidgetItem
from PySide6.QtGui import QPixmap
from gui.page import Page
from google.oauth2.credentials import Credentials
from utils.paths import CaseInsensitivePathDict
from pathlib import Path
from core.models import SyncAction

class SignalManager(QObject):
    notify_user = Signal(str)
    sign_in_clicked = Signal()
    auth_success = Signal(object)
    auth_failed = Signal(str)
    auth_started = Signal()
    sign_out = Signal()
    has_internet = Signal(bool)
    token_refreshed = Signal(bool, Credentials)
    user_validation_data_fetched = Signal(set, set)
    user_profile_loaded = Signal(dict, QPixmap)
    drive_file_list_acquired = Signal(list)
    drive_file_list_finished = Signal()
    local_file_list_acquired = Signal(list)
    local_file_list_finished = Signal()
    error_occurred = Signal(str)
    sync_analysis_started = Signal()
    sync_analysis_finished = Signal(list, list, CaseInsensitivePathDict)
    sync_count_updated = Signal(int, int)
    sync_error = Signal()
    project_read_only = Signal()
    update_sync_selection_treeview = Signal(list)
    check_all = Signal()
    uncheck_all = Signal()
    file_progress = Signal(int, float)
    action_complete = Signal(SyncAction)
    action_failed = Signal()
    sync_cancelled = Signal()
    sync_execution_finished = Signal()
    dropped_actions = Signal(list)
    dropped_actions_acknowledged = Signal()
    refresh_clicked = Signal()
    options_clicked = Signal()
    settings_clicked = Signal()
    bug_clicked = Signal()
    bug_report = Signal(int, str)
    project_list_changed = Signal(list)
    page_change = Signal(Page)
    project_list_item_activated = Signal(QListWidgetItem, int)
    remove_project_clicked = Signal(int)
    move_project_up_clicked = Signal(int)
    move_project_down_clicked = Signal(int)
    disable_buttons = Signal()
    enable_buttons = Signal()
    upload_card_clicked = Signal()
    download_card_clicked = Signal()
    back_clicked = Signal()
    execute_sync = Signal(tuple)
    options_clicked = Signal()
    latest_app_version = Signal(str)
    new_install = Signal()
    prompt_for_usage_data = Signal()
    download_app_update = Signal()
    change_working_directory = Signal(Path, Path, bool)

    def __init__(self):
        super().__init__()

signal_manager = SignalManager()
import traceback
import logging

def debug_slot(*args, **kwargs):
    logging.debug('Signal received with args:', args, kwargs)
    traceback.print_stack()

signal_manager.project_read_only.connect(debug_slot)
