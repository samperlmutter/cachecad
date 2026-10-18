from PySide6.QtCore import QObject, Signal, QRunnable
from core.thread_manager import thread_pool
from core.signal_manager import signal_manager
from core.models import SyncAction, SyncActionType
from utils.timer import tic, toc
from collections import Counter
from utils.paths import get_project_app_folder
import json
import csv
import logging
from pathlib import Path
import traceback

logger = logging.getLogger(__name__)

class SyncAnalyzer(QObject):
    sync_status_updated = Signal(list)
    sync_error = Signal(str)
    background_sync_completed = Signal()

    def __init__(self, drive_client, local_client, db_manager):
        super().__init__()
        self.drive_client = drive_client
        self.local_client = local_client
        self.db_manager = db_manager
        self._background_mode = False
        self._cancelled = False
        self._drive_files = None
        self._local_files = None
        signal_manager.local_file_list_acquired.connect(self._on_local_file_list_acquired)
        signal_manager.drive_file_list_acquired.connect(self._on_drive_file_list_acquired)
        signal_manager.local_file_list_finished.connect(self._on_local_task_finished)
        signal_manager.drive_file_list_finished.connect(self._on_drive_task_finished)
        signal_manager.sync_execution_finished.connect(self.start_sync_analysis)

    def start_sync_analysis(self, project_name: str, project_id: str, working_dir: str, background: bool = False, read_only: bool = False):
        self.cancel_sync_analysis()
        self.read_only = read_only
        self._background_mode = background
        self._cancelled = False
        self.project_id = project_id
        self.working_dir = working_dir
        self.project_name = project_name
        self.sync_log_path = get_project_app_folder(self.working_dir, self.project_name) / 'sync_log.csv'
        self.folder_dict_path = get_project_app_folder(self.working_dir, self.project_name) / 'folder_dict.csv'
        self._drive_files_ready = False
        self._local_files_ready = False
        self.cloud_not_local = []
        self.local_not_cloud = []
        self.md5_mismatch = []
        self.sync_log_dict = {}
        if not background:
            self.db_manager.reset()
            signal_manager.sync_analysis_started.emit()
        self.cached_folders = self._load_cached_folders()
        self.local_task = _FunctionRunnable(self.local_worker)
        self.drive_task = _FunctionRunnable(self.drive_worker)
        thread_pool.start(self.local_task)
        thread_pool.start(self.drive_task)

    def local_worker(self):
        if self._cancelled: return
        self.local_items = self.local_client.get_local_items_from_path(self.project_name, self.working_dir)

    def _on_local_file_list_acquired(self, local_items):
        if self._cancelled:
            return
        self.db_manager.write_local_items(local_items)
        self.save_local_as_csv(local_items)
        signal_manager.local_file_list_finished.emit()

    def drive_worker(self):
        if self._cancelled: return
        self.drive_items = self.drive_client.get_drive_items_from_project_id(self.project_id, self.cached_folders)

    def _on_drive_file_list_acquired(self, drive_items):
        if self._cancelled:
            return
        self.db_manager.write_drive_items(drive_items)
        self.save_drive_as_csv(drive_items)
        signal_manager.drive_file_list_finished.emit()

    def cancel_sync_analysis(self):
        """Marks the sync operation as cancelled."""
        self._cancelled = True

    def is_background_running(self):
        return self._background_mode and not self._cancelled

    def _on_local_task_finished(self):
        logging.info('Local file list received')
        print('Local file list received')
        self._local_files_ready = True
        self._check_ready()

    def _on_drive_task_finished(self):
        logging.info('Drive file list received')
        print('Drive file list received')
        self._drive_files_ready = True
        self._check_ready()

    def _on_error(self, message):
        if self._cancelled:
            return
        if not self._background_mode:
            self.sync_error.emit(message)
        else:
            print(f'[Background Sync Error] {message}')
        self.sync_finished.emit()

    def _check_ready(self):
        if self._drive_files_ready and self._local_files_ready:
            self.compare_task = _FunctionRunnable(self._compare_worker)
            thread_pool.start(self.compare_task)

    def _compare_worker(self):
        tic()
        self.read_sync_log()
        self.local_not_drive, self.drive_not_local, self.md5_mismatch, self.folder_dict = self.db_manager.compare_local_and_drive()
        self.actions = self.compute_sync_actions()
        self.write_folder_dict_csv()
        download_actions, upload_actions = self.sort_actions()
        signal_manager.sync_count_updated.emit(len(download_actions), len(upload_actions))
        signal_manager.sync_analysis_finished.emit(download_actions, upload_actions, self.folder_dict)
        toc('Comparison Time: ')

    def write_folder_dict_csv(self):
        if self.folder_dict_path.exists():
            try:
                self.folder_dict_path.unlink()
            except Exception as e:
                tb = traceback.format_exc()
                logging.error(f'Error deleting existing folder_dict.csv: {e}\n{tb}')
                signal_manager.error_occurred.emit('')
        with open(self.folder_dict_path, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['path', 'id'])
            for path, id in self.folder_dict.items():
                writer.writerow([path, id])

    def _load_cached_folders(self):
        folder_id_list = []
        if self.folder_dict_path.exists():
            try:
                with open(self.folder_dict_path, mode='r', newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        id = row['id']
                        folder_id_list.append(id)
            except Exception as e:
                tb = traceback.format_exc()
                logging.error(f'Error reading folder_dict.csv: {e}\n{tb}')
                signal_manager.error_occurred.emit('')
        return folder_id_list

    def read_sync_log(self) -> dict:
        self.sync_log_dict.clear()
        if self.sync_log_path.exists():
            with open(self.sync_log_path, mode='r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    path = row['path']
                    self.sync_log_dict[path] = row

    def compute_sync_actions(self):
        actions = []
        for cloud_item in self.drive_not_local:
            path = cloud_item.path
            md5 = cloud_item.md5_checksum
            size = cloud_item.size
            parent_id = cloud_item.parent
            drive_id = cloud_item.id
            log_entry = self.sync_log_dict.get(path)
            modified_by = cloud_item.modified_by
            modified_time = cloud_item.modified_time
            if log_entry:
                if log_entry.get('md5') == md5 and not self.read_only:
                    actions.append(SyncAction(path=path, action=SyncActionType.DELETE_CLOUD, reason='missing_local', size=size, md5_checksum=md5, parent_id=parent_id, modified_by=modified_by, modified_time='', drive_id=drive_id))
                else:
                    actions.append(SyncAction(path=path, action=SyncActionType.DOWNLOAD_NEW, reason='cloud_only_changed', size=size, md5_checksum=md5, parent_id=parent_id, modified_by=modified_by, modified_time=modified_time, drive_id=drive_id))
            else:
                actions.append(SyncAction(path=path, action=SyncActionType.DOWNLOAD_NEW, reason='cloud_only', size=size, md5_checksum=md5, parent_id=parent_id, modified_by=modified_by, modified_time=modified_time, drive_id=drive_id))
        for local_item in self.local_not_drive:
            path = local_item.path
            md5 = local_item.md5_checksum
            size = local_item.size
            parent_id = ''
            log_entry = self.sync_log_dict.get(path)
            modified_by = ''
            modified_time = local_item.modified_time
            if log_entry:
                if log_entry.get('md5') == md5:
                    actions.append(SyncAction(path=path, action=SyncActionType.DELETE_LOCAL, reason='missing_cloud', size=size, md5_checksum=md5, parent_id=parent_id, modified_by=modified_by, modified_time=''))
                else:
                    actions.append(SyncAction(path=path, action=SyncActionType.UPLOAD_NEW, reason='local_only_changed', size=size, md5_checksum=md5, parent_id=parent_id, modified_by=modified_by, modified_time=modified_time))
            else:
                actions.append(SyncAction(path=path, action=SyncActionType.UPLOAD_NEW, reason='local_only', size=size, md5_checksum=md5, parent_id=parent_id, modified_by=modified_by, modified_time=modified_time))
        for cloud_item, local_item in self.md5_mismatch:
            path = local_item.path
            local_md5 = local_item.md5_checksum
            cloud_md5 = cloud_item.md5_checksum
            log_entry = self.sync_log_dict.get(path)
            modified_by = cloud_item.modified_by
            drive_id = cloud_item.id
            if log_entry and log_entry.get('md5') == local_md5:
                actions.append(SyncAction(path=path, action=SyncActionType.DOWNLOAD_REVISION, reason='md5_mismatch', size=cloud_item.size, md5_checksum=cloud_md5, parent_id=cloud_item.parent, modified_by=modified_by, modified_time=cloud_item.modified_time, drive_id=drive_id))
            elif log_entry and log_entry.get('md5') == cloud_md5:
                actions.append(SyncAction(path=path, action=SyncActionType.UPLOAD_REVISION, reason='md5_mismatch', size=local_item.size, md5_checksum=local_md5, parent_id=cloud_item.parent, modified_by='', modified_time=local_item.modified_time, drive_id=drive_id))
            else:
                actions.append(SyncAction(path=path, action=SyncActionType.UPLOAD_UNKNOWN, reason='md5_mismatch', size=local_item.size, md5_checksum=local_md5, parent_id=cloud_item.parent, modified_by='', modified_time=local_item.modified_time, drive_id=drive_id))
                actions.append(SyncAction(path=path, action=SyncActionType.DOWNLOAD_UNKNOWN, reason='md5_mismatch', size=cloud_item.size, md5_checksum=cloud_md5, parent_id=cloud_item.parent, modified_by=modified_by, modified_time=cloud_item.modified_time, drive_id=drive_id))
        return actions

    def sort_actions(self):
        upload_actions = [SyncActionType.UPLOAD_NEW, SyncActionType.UPLOAD_REVISION, SyncActionType.DELETE_CLOUD, SyncActionType.UPLOAD_UNKNOWN]
        download_actions = [SyncActionType.DOWNLOAD_NEW, SyncActionType.DOWNLOAD_REVISION, SyncActionType.DELETE_LOCAL, SyncActionType.DOWNLOAD_UNKNOWN]
        upload_order = {action: i for i, action in enumerate(upload_actions)}
        download_order = {action: i for i, action in enumerate(download_actions)}
        uploads = [a for a in self.actions if a.action in upload_actions]
        downloads = [a for a in self.actions if a.action in download_actions]
        uploads.sort(key=lambda a: (upload_order[a.action], a.path.lower()))
        downloads.sort(key=lambda a: (download_order[a.action], a.path.lower()))
        self.action_log_path = get_project_app_folder(self.working_dir, self.project_name) / 'action_log.csv'
        all_actions = uploads + downloads
        if all_actions:
            fieldnames = ['path', 'action', 'reason', 'size', 'md5_checksum', 'parent_id', 'modified_by', 'modified_time', 'drive_id']
            with open(self.action_log_path, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for action in all_actions:
                    writer.writerow({'path': action.path, 'action': action.action.value, 'reason': action.reason, 'size': action.size, 'md5_checksum': action.md5_checksum, 'parent_id': action.parent_id, 'modified_by': action.modified_by, 'modified_time': action.modified_time, 'drive_id': action.drive_id})
        return downloads, uploads

    def save_local_as_csv(self, local_files):
        csv_path = get_project_app_folder(self.working_dir, self.project_name) / 'local.csv'
        rows = [(info.path, info.full_path, info.size, info.modified_time, info.md5_checksum, int(info.is_folder)) for info in local_files]
        with csv_path.open(mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['path', 'full_path', 'size', 'modified_time', 'md5_checksum', 'is_folder'])
            writer.writerows(rows)

    def save_drive_as_csv(self, drive_items):
        csv_path = get_project_app_folder(self.working_dir, self.project_name) / 'drive.csv'
        rows = [(info.path, info.id, info.name, info.parent, info.md5_checksum, info.version, info.size, info.modified_time, info.drive_id, info.modified_by, int(info.is_folder)) for info in drive_items]
        with csv_path.open(mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['path', 'id', 'name', 'parent', 'md5_checksum', 'version', 'size', 'modified_time', 'drive_id', 'modified_by', 'is_folder'])
            logging.debug(f'Writing drive CSV with {len(rows)} rows to {csv_path}')
            writer.writerows(rows)

class _FunctionRunnable(QRunnable):
    """Simple wrapper to run any function in a thread."""
    def __init__(self, func):
        super().__init__()
        self.func = func

    def run(self):
        try:
            self.func()
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f'Error in _FunctionRunnable: {e}\n{tb}')
            signal_manager.error_occurred.emit('')
