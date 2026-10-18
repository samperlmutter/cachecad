from PySide6.QtCore import QObject, Signal, QRunnable
from PySide6.QtCore import Qt
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
    scan_outcome = Signal(int, str, object, str)
    analysis_started = Signal(int)
    analysis_succeeded = Signal(int, list, list, object)
    analysis_failed = Signal(int, str)
    analysis_cancelled = Signal(int)
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
        self._generation = 0
        self._settled = True
        self._comparing = False
        self._drive_files = None
        self._local_files = None
        self.scan_outcome.connect(self._on_scan_outcome, Qt.QueuedConnection)

    def start_sync_analysis(self, project_name: str, project_id: str, working_dir: str, background: bool = False, read_only: bool = False):
        self.cancel_sync_analysis()
        self._generation += 1
        generation = self._generation
        self._settled = False
        self._comparing = False
        self._cancelled = False
        self._background_mode = background
        self.read_only = read_only
        self.project_id = project_id
        self.working_dir = working_dir
        self.project_name = project_name
        self._drive_files = None
        self._local_files = None
        self._drive_files_ready = False
        self._local_files_ready = False
        self.actions = []
        self.local_not_drive = []
        self.drive_not_local = []
        self.md5_mismatch = []
        self.folder_dict = {}
        self.sync_log_dict = {}
        self.analysis_started.emit(generation)
        if not background:
            signal_manager.sync_analysis_started.emit()
        try:
            self.sync_log_path = get_project_app_folder(working_dir, project_name) / 'sync_log.csv'
            self.folder_dict_path = get_project_app_folder(working_dir, project_name) / 'folder_dict.csv'
            cached_folders = self._load_cached_folders()
            self.local_task = _FunctionRunnable(lambda: self.local_worker(generation, project_name, working_dir))
            self.drive_task = _FunctionRunnable(lambda: self.drive_worker(generation, project_id, cached_folders))
            thread_pool.start(self.local_task)
            thread_pool.start(self.drive_task)
        except Exception as e:
            self._fail_analysis(generation, str(e))

    def local_worker(self, generation, project_name, working_dir):
        if generation != self._generation or self._settled:
            return
        try:
            self.local_client.get_local_items_from_path(
                project_name, working_dir, scan_id=generation,
                on_success=lambda scan_id, items: self.scan_outcome.emit(scan_id, 'local', items, ''),
                on_error=lambda scan_id, message: self.scan_outcome.emit(scan_id, 'local', None, str(message)))
        except Exception as e:
            self.scan_outcome.emit(generation, 'local', None, str(e))

    def _on_local_file_list_acquired(self, local_items):
        # Untagged legacy results cannot safely identify their originating scan.
        return

    def drive_worker(self, generation, project_id, cached_folders):
        if generation != self._generation or self._settled:
            return
        try:
            self.drive_client.get_drive_items_from_project_id(
                project_id, cached_folders, scan_id=generation,
                on_success=lambda scan_id, items: self.scan_outcome.emit(scan_id, 'drive', items, ''),
                on_error=lambda scan_id, message: self.scan_outcome.emit(scan_id, 'drive', None, str(message)))
        except Exception as e:
            self.scan_outcome.emit(generation, 'drive', None, str(e))

    def _on_drive_file_list_acquired(self, drive_items):
        # Only scan_outcome carries generation-bound results.
        return

    def cancel_sync_analysis(self):
        """Invalidate pending outcomes without waiting for scanner threads."""
        self._cancelled = True
        if not self._settled:
            self._settled = True
            self.actions = []
            self._drive_files = None
            self._local_files = None
            self.analysis_cancelled.emit(self._generation)

    def is_background_running(self):
        return self._background_mode and not self._settled

    def _on_local_task_finished(self):
        return

    def _on_drive_task_finished(self):
        return

    def _on_error(self, message):
        # Untagged errors cannot identify the originating scan.
        return

    def _check_ready(self):
        if not self._settled and not self._comparing and self._drive_files_ready and self._local_files_ready:
            self._comparing = True
            self._compare_worker(self._generation)

    def _compare_worker(self, generation):
        # Runs serially in the analyzer's result slot: no shared DB/plan worker race.
        if generation != self._generation or self._settled:
            return
        try:
            self.db_manager.reset()
            self.db_manager.write_local_items(self._local_files)
            self.db_manager.write_drive_items(self._drive_files)
            self.save_local_as_csv(self._local_files)
            self.save_drive_as_csv(self._drive_files)
            self.read_sync_log()
            self.local_not_drive, self.drive_not_local, self.md5_mismatch, self.folder_dict = self.db_manager.compare_local_and_drive()
            self.actions = self.compute_sync_actions()
            self.write_folder_dict_csv()
            download_actions, upload_actions = self.sort_actions()
            if generation != self._generation or self._settled:
                return
            self._settled = True
            signal_manager.sync_count_updated.emit(len(download_actions), len(upload_actions))
            self.analysis_succeeded.emit(generation, download_actions, upload_actions, self.folder_dict)
        except Exception as e:
            self._fail_analysis(generation, str(e))

    def _on_scan_outcome(self, generation, side, items, message):
        if generation != self._generation or self._settled or side not in ('local', 'drive'):
            return
        if (side == 'local' and self._local_files_ready) or (side == 'drive' and self._drive_files_ready):
            return
        if message or items is None:
            self._fail_analysis(generation, message or 'Scan returned no complete result')
            return
        try:
            items = list(items)
        except Exception as e:
            self._fail_analysis(generation, str(e))
            return
        if side == 'local':
            if self._local_files_ready:
                return
            self._local_files = list(items)
            self._local_files_ready = True
        else:
            if self._drive_files_ready:
                return
            self._drive_files = list(items)
            self._drive_files_ready = True
        self._check_ready()

    def _fail_analysis(self, generation, message):
        if generation != self._generation or self._settled:
            return
        self._settled = True
        self._cancelled = True
        self.actions = []
        self._drive_files = None
        self._local_files = None
        self._drive_files_ready = False
        self._local_files_ready = False
        self.analysis_failed.emit(generation, message or 'Sync analysis failed')

    def write_folder_dict_csv(self):
        # Propagate errors to the generation-bound comparison handler.
        # Do not open a nested GUI error dialog while shared comparison state is active.
        if self.folder_dict_path.exists():
            self.folder_dict_path.unlink()
        with open(self.folder_dict_path, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['path', 'id'])
            for path, id in self.folder_dict.items():
                writer.writerow([path, id])

    def _load_cached_folders(self):
        folder_id_list = []
        if self.folder_dict_path.exists():
            with open(self.folder_dict_path, mode='r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    folder_id_list.append(row['id'])
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
