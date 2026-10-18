from PySide6.QtCore import QObject, Signal, QRunnable, QEventLoop
from core.signal_manager import signal_manager
import logging
from pathlib import Path
from core.models import SyncActionType
from utils.paths import CaseInsensitivePathDict, get_project_app_folder
import threading
from core.thread_manager import thread_pool, network_pool
import os
from send2trash import send2trash
import traceback
import csv
from app.app_version import APP_VERSION, APP_BUILD_DATE, APP_IS_BETA
from utils.app_settings import settings

class SyncExecutor(QObject):
    thread_finished = Signal()

    def __init__(self, drive_client, local_client, project_name, project_id, working_directory, action_tuple, path_dict: CaseInsensitivePathDict):
        super().__init__()
        self.drive_client = drive_client
        self.local_client = local_client
        self.project_name = project_name
        self.project_id = project_id
        self.working_directory = working_directory
        self.action_tuple = action_tuple
        self.path_dict = path_dict
        self.path_dict_lock = threading.Lock()
        self.sync_log_lock = threading.Lock()
        self.sync_log_path = get_project_app_folder(working_directory, project_name) / 'sync_log.csv'
        self.dropped_actions = []
        self.folders_to_create = []
        signal_manager.dropped_actions_acknowledged.connect(self.start_sync)
        signal_manager.action_complete.connect(self.on_action_complete)
        signal_manager.action_failed.connect(self.on_action_failed)
        self.pre_sync_checks()
        signal_manager.dropped_actions.emit(self.dropped_actions)

    def pre_sync_checks(self):
        action_types = {action[1].action for action in self.action_tuple}
        if SyncActionType.UPLOAD_NEW in action_types:
            self.folders_to_create = self.get_folders_to_create_on_drive()
        if SyncActionType.DOWNLOAD_REVISION in action_types or SyncActionType.DOWNLOAD_UNKNOWN in action_types:
            for _, action in self.action_tuple:
                if action.action in (SyncActionType.DOWNLOAD_REVISION, SyncActionType.DOWNLOAD_UNKNOWN):
                    local_path = Path(self.working_directory) / self.project_name / action.path
                    if self.local_client.is_file_open(local_path):
                        logging.warning(f'File is open: {local_path}. Cannot replace with version from Drive.')
                        self.dropped_actions.append(action)
            self.action_tuple = [action for action in self.action_tuple if action[1] not in self.dropped_actions]

    def start_sync(self):
        logging.info('Starting sync execution')
        action_count = len(self.action_tuple)
        if action_count > 0:
            self.complete_action_count = 0
            self.cancelled_action_count = 0
            self.failed_action_count = 0
            self.completed_actions = []
            self.cancelled = False
            sync_task = _FunctionRunnable(self.sync_worker)
            network_pool.start(sync_task)

    def log_sync_action(self, action, id=None):
        log_tuple = action[1].get_log_tuple()
        if id is not None:
            log_tuple = (id,) + log_tuple[1:]
        with self.sync_log_lock:
            file_exists = self.sync_log_path.exists()
            with open(self.sync_log_path, mode='a', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                if not file_exists:
                    writer.writerow(['drive_id', 'name', 'path', 'md5', 'modified_time', 'size', 'timestamp', 'action'])
                writer.writerow(log_tuple)

    def sync_worker(self):
        logging.debug('Sync worker started')
        loop = QEventLoop()
        self.thread_finished.connect(loop.quit)
        def create_folders_worker():
            for folder in self.folders_to_create:
                logging.info(f'Creating folder: {folder}')
                parent_id = self.get_parent_id(folder)
                folder_name = folder.name
                folder_id = self.drive_client.create_folder(folder_name, parent_id)
                if folder_id is not None:
                    self.update_path_dict(str(folder), folder_id)
                    logging.info(f'Folder created: {folder} (ID: {folder_id})')
                else:
                    logging.error(f'Failed to create folder: {folder}')
            self.thread_finished.emit()
        if len(self.folders_to_create) > 0:
            create_folders_task = _FunctionRunnable(create_folders_worker)
            thread_pool.start(create_folders_task)
            loop.exec_()
        def action_worker(action_tuple):
            action = action_tuple
            if action[1].action == SyncActionType.UPLOAD_NEW:
                self._upload(action, revision=False)
            elif action[1].action == SyncActionType.DOWNLOAD_NEW:
                self._download(action, revision=False)
            elif action[1].action == SyncActionType.UPLOAD_REVISION:
                self._upload(action, revision=True)
            elif action[1].action == SyncActionType.DOWNLOAD_REVISION:
                self._download(action, revision=True)
            elif action[1].action == SyncActionType.DELETE_LOCAL:
                self._delete_local(action)
            elif action[1].action == SyncActionType.DELETE_CLOUD:
                self._delete_cloud(action)
            elif action[1].action == SyncActionType.UPLOAD_UNKNOWN:
                self._upload(action, revision=True)
            elif action[1].action == SyncActionType.DOWNLOAD_UNKNOWN:
                self._download(action, revision=True)
        if len(self.action_tuple) > 0:
            for action in self.action_tuple:
                action_task = _FunctionRunnable(lambda a=action: action_worker(a))
                network_pool.start(action_task)

    def on_action_complete(self, action):
        self.completed_actions.append(action)
        self.complete_action_count += 1
        self.check_if_all_actions_finished()

    def on_action_failed(self):
        if self.cancelled:
            self.cancelled_action_count += 1
        else:
            self.failed_action_count += 1
        self.check_if_all_actions_finished()

    def check_if_all_actions_finished(self):
        if self.failed_action_count + self.complete_action_count + self.cancelled_action_count == len(self.action_tuple):
            from utils.development_mode import is_enabled
            if not is_enabled():
                total_size = 0
                user_info = self.drive_client.get_user_info_for_cloud_logging()
                try:
                    email = user_info['email_hash']
                except:
                    email = 'unknown_user'
                try:
                    domain = user_info['email_domain']
                except:
                    domain = 'unknown_domain'
                try:
                    usage = settings.get('usage')
                except:
                    usage = 'unknown'
                log_required = False
                for action in self.completed_actions:
                    if action is None:
                        continue
                    log_required = True
                    total_size += action.size if action.size is not None else 0
                    if action.action in (SyncActionType.UPLOAD_NEW, SyncActionType.UPLOAD_REVISION, SyncActionType.UPLOAD_UNKNOWN, SyncActionType.DELETE_CLOUD):
                        action_type = 'upload'
                    else:
                        action_type = 'download'
                    version = f'CacheCAD v{APP_VERSION}'
                    if APP_IS_BETA:
                        version += f' - (Beta {APP_BUILD_DATE})'
                if log_required:
                    log = {
                        'email': email,
                        'domain': domain,
                        'usage': usage,
                        'action': action_type,
                        'succeed': self.complete_action_count,
                        'size': total_size,
                        'fail': self.failed_action_count,
                        'version': version,
                    }
                    from utils.cloud_logging import cloud_logging
                    cloud_logging.log_sync(log)
            logging.info('All actions completed')
            signal_manager.sync_execution_finished.emit()
            self.cancelled = False
            self.complete_action_count = 0
            self.cancelled_action_count = 0
            self.failed_action_count = 0
            self.deleteLater()

    def on_sync_cancelled(self):
        logging.info('Sync cancelled by user')
        self.cancelled = True

    def get_parent_id(self, folder: Path) -> str:
        if isinstance(folder, str):
            folder = Path(folder)
        parent = folder.parent
        if parent == Path('.'):
            parent_id = self.project_id
        else:
            parent_id = self.path_dict.get(str(parent))
            if parent_id is None:
                logging.error(f'Parent folder not found in path_dict: {parent}')
                raise ValueError(f'Parent folder not found in path_dict: {parent}')
        return parent_id

    def get_folders_to_create_on_drive(self):
        folders = {
            Path(file.folder)
            for _, file in self.action_tuple
            if file.folder is not None
        }
        required_folders = set(folders)
        logging.debug('Required Folders based on actions:')
        for folder in folders:
            logging.debug(f'  {folder}')
            for parent in folder.parents:
                if parent != Path('.'):
                    required_folders.add(parent)
        folders_to_create = []
        if len(required_folders) > 0:
            for folder in required_folders:
                folder_id = self.path_dict.get(str(folder))
                if folder_id is None:
                    folders_to_create.append(folder)
        folders_to_create.sort(key=lambda p: len(p.parents))
        logging.debug('Folders to create on Drive:')
        for folder in folders_to_create:
            logging.debug(f'  {folder}')
        return folders_to_create

    def update_path_dict(self, path: str, drive_id: str):
        with self.path_dict_lock:
            self.path_dict[path] = drive_id
            if path not in self.path_dict:
                logging.debug(f'Adding new path to path_dict: {path} -> {drive_id}')
            else:
                logging.warning(f'Updating existing path in path_dict: {path} -> {drive_id}')

    def _download(self, action, revision=False):
        # New files use a verified private staging directory and atomic no-clobber
        # publication. Revision replacement below remains the original behavior.
        if not revision:
            try:
                import hashlib
                import stat
                import tempfile
                if self.cancelled:
                    raise RuntimeError('Download cancelled before staging')
                selected = action[1]
                relative = Path(selected.path)
                if relative.is_absolute() or not relative.parts or '..' in relative.parts:
                    raise ValueError('Download path must remain inside the project')
                root = (Path(self.working_directory) / self.project_name).resolve()
                local_path = root / relative
                if not local_path.resolve().is_relative_to(root):
                    raise ValueError('Download path resolves outside the project')
                expected_md5 = selected.md5_checksum
                expected_size = selected.size
                if (not isinstance(expected_md5, str) or len(expected_md5) != 32
                        or any(c not in '0123456789abcdefABCDEF' for c in expected_md5)
                        or type(expected_size) is not int or expected_size < 0):
                    raise ValueError('Selected download lacks a valid checksum or size')
                if os.path.lexists(local_path):
                    raise FileExistsError('New download destination already exists')
                local_path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='.cachecad-download-', dir=str(local_path.parent)) as folder:
                    staging = Path(folder)
                    returned = self.drive_client.download_file(
                        selected.drive_id, str(staging / local_path.name), action[0])
                    if returned is None:
                        raise RuntimeError('Download did not return a staged file')
                    temp_path = Path(returned)
                    if (temp_path.is_symlink() or temp_path.parent.resolve() != staging.resolve()
                            or temp_path == local_path):
                        raise ValueError('Download returned a file outside private staging')
                    before = temp_path.stat(follow_symlinks=False)
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError('Downloaded staging object is not a regular file')
                    digest = hashlib.md5()
                    with temp_path.open('rb') as source:
                        opened = os.fstat(source.fileno())
                        for chunk in iter(lambda: source.read(1024 * 1024), b''):
                            digest.update(chunk)
                        after = os.fstat(source.fileno())
                    def fingerprint(info):
                        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    if (not before.st_ino or fingerprint(before) != fingerprint(opened)
                            or fingerprint(before) != fingerprint(after)
                            or fingerprint(before) != fingerprint(temp_path.stat(follow_symlinks=False))):
                        raise RuntimeError('Downloaded staging file changed during verification')
                    if before.st_size != expected_size or digest.hexdigest() != expected_md5.lower():
                        raise ValueError('Downloaded bytes do not match the selected action')
                    if self.cancelled:
                        raise RuntimeError('Download cancelled before publication')
                    # os.link creates the destination only if it is absent. No
                    # rename/replace fallback is allowed on unsupported filesystems.
                    os.link(str(temp_path), str(local_path), follow_symlinks=False)
                self.log_sync_action(action)
            except Exception as e:
                logging.error(f'New download failed without replacing an existing file: {e}\n{traceback.format_exc()}')
                signal_manager.action_failed.emit()
                if not self.cancelled:
                    signal_manager.error_occurred.emit('')
                return
            signal_manager.action_complete.emit(action[1])
            return
        if self.cancelled:
            signal_manager.action_failed.emit()
            return
        local_path = Path(self.working_directory) / self.project_name / action[1].path
        self._make_local_folders(local_path.parent)
        drive_id = action[1].drive_id
        idx = action[0]
        logging.info(f'Action {str(idx)} Downloading file: {local_path} from {drive_id}')
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            if local_path.exists() and not revision:
                logging.warning(f"Attempting to download {local_path}, but it already already exists and this isn't a revision.")
                logging.error(f'This is action {str(idx)} in the action list and drive id {drive_id}.')
                return
            temp_path = self.drive_client.download_file(drive_id, str(local_path), idx)
            if temp_path is None:
                signal_manager.action_failed.emit()
                return
            if temp_path == local_path:
                logging.error("download_file didn't give a temp name")
            if local_path.exists():
                logging.error('local path somehow exists now')
            md5 = self.local_client.calculate_md5(temp_path)
            logging.info(f'MD5 checksum of downloaded file: {md5}')
            if md5 == action[1].md5_checksum:
                logging.info(f'MD5 checksum matches for downloaded file: {local_path}')
                if revision:
                    send2trash(local_path)
                if local_path.exists() and not revision:
                    logging.error(f'File already exists locally and action is not a revision. This should have been caught already {local_path.name}')
                os.rename(str(temp_path), str(local_path))
                self.log_sync_action(action)
                signal_manager.action_complete.emit(action[1])
            else:
                logging.error(f'MD5 checksum mismatch for downloaded file: {local_path}. Expected: {action[1].md5_checksum}, Got: {md5}')
                try:
                    if temp_path:
                        if Path(temp_path).exists():
                            os.remove(temp_path)
                except Exception:
                    pass
                signal_manager.action_failed.emit()
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f'Failed to download new file {action[1].path}: {e}\n{tb}')
            signal_manager.action_failed.emit()
            if not self.cancelled:
                signal_manager.error_occurred.emit('')

    def _upload(self, action, revision=False):
        if self.cancelled:
            signal_manager.action_failed.emit()
            return
        try:
            path = action[1].path
            local_path = Path(self.working_directory) / self.project_name / path
            parent_id = self.get_parent_id(path)
            existing_drive_id = action[1].drive_id if revision else None
            idx = action[0]
            logging.info(f'Uploading new file: {path} in parent ID: {parent_id}, existing_drive_id: {str(existing_drive_id)}')
            if revision:
                # The legacy revision path has no validated remote conditional
                # commit. Preserve its API contract; do not claim race protection.
                new_id, drive_md5 = self.drive_client.upload_file(str(local_path), parent_id, idx, existing_drive_id)
            else:
                import hashlib
                import stat
                import tempfile
                relative = Path(path)
                if relative.is_absolute() or not relative.parts or '..' in relative.parts:
                    raise ValueError('Upload path must remain inside the project')
                root = (Path(self.working_directory) / self.project_name).resolve()
                if local_path.is_symlink() or not local_path.resolve().is_relative_to(root):
                    raise ValueError('Upload source is a link or resolves outside the project')
                expected_md5 = action[1].md5_checksum
                expected_size = action[1].size
                expected_mtime = action[1].modified_time
                if (not isinstance(expected_md5, str) or len(expected_md5) != 32
                        or any(c not in '0123456789abcdefABCDEF' for c in expected_md5)
                        or type(expected_size) is not int or expected_size < 0
                        or type(expected_mtime) not in (int, float)):
                    raise ValueError('Selected upload lacks a valid checksum, size, or timestamp')
                before = local_path.stat(follow_symlinks=False)
                if (not stat.S_ISREG(before.st_mode) or not before.st_ino
                        or before.st_size != expected_size or before.st_mtime != expected_mtime):
                    raise RuntimeError('Upload source no longer matches the selected action')
                def fingerprint(info):
                    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                # Keep the original filename: Drive create derives its name from
                # the upload path. Only this private read-only copy is uploaded.
                with tempfile.TemporaryDirectory(prefix='cachecad-upload-') as folder:
                    staged_path = Path(folder) / local_path.name
                    digest = hashlib.md5()
                    with local_path.open('rb') as source, staged_path.open('xb') as target:
                        opened = os.fstat(source.fileno())
                        for chunk in iter(lambda: source.read(1024 * 1024), b''):
                            digest.update(chunk)
                            target.write(chunk)
                        after = os.fstat(source.fileno())
                        target.flush()
                        os.fsync(target.fileno())
                    if (fingerprint(before) != fingerprint(opened)
                            or fingerprint(before) != fingerprint(after)
                            or fingerprint(before) != fingerprint(local_path.stat(follow_symlinks=False))
                            or digest.hexdigest() != expected_md5.lower()):
                        raise RuntimeError('Upload source changed while its snapshot was staged')
                    staged_digest = hashlib.md5()
                    with staged_path.open('rb') as staged:
                        for chunk in iter(lambda: staged.read(1024 * 1024), b''):
                            staged_digest.update(chunk)
                    if staged_path.stat().st_size != expected_size or staged_digest.hexdigest() != expected_md5.lower():
                        raise RuntimeError('Staged upload bytes failed verification')
                    staged_path.chmod(stat.S_IRUSR)
                    if self.cancelled:
                        raise RuntimeError('Upload cancelled before transfer')
                    new_id, drive_md5 = self.drive_client.upload_file(str(staged_path), parent_id, idx, None)
        except Exception as e:
            logging.error(f'Upload failed before a result was available: {e}\n{traceback.format_exc()}')
            signal_manager.action_failed.emit()
            if not self.cancelled:
                signal_manager.error_occurred.emit('')
            return
        logging.info(f'File uploaded to : {new_id}')
        logging.info(f'MD5 checksum of uploaded file: {drive_md5}')
        checksum_matches = (drive_md5 == action[1].md5_checksum if revision else
                            isinstance(drive_md5, str) and drive_md5.lower() == expected_md5.lower())
        if new_id is not None and checksum_matches:
            logging.info(f'MD5 checksum matches for uploaded file: {local_path}')
            self.log_sync_action(action, id=new_id)
            signal_manager.action_complete.emit(action[1])
        else:
            logging.error(f'Upload failed (no ID returned) for file: {local_path}')
            signal_manager.action_failed.emit()

    def _delete_local(self, action):
        if self.cancelled:
            signal_manager.action_failed.emit()
            return
        logging.info(f'Deleting local file: {action[1].path}')
        local_path = Path(self.working_directory) / self.project_name / action[1].path
        try:
            send2trash(local_path)
            self.log_sync_action(action)
            signal_manager.action_complete.emit(None)
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f'Failed to delete local file {local_path}: {e}\n{tb}')
            signal_manager.error_occurred.emit('')

    def _delete_cloud(self, action):
        if self.cancelled:
            signal_manager.action_failed.emit()
            return
        path = action[1].path
        drive_id = action[1].drive_id
        logging.info(f'Deleting cloud file: {path} (ID: {drive_id})')
        try:
            if not self.drive_client.delete_file(drive_id):
                logging.error(f'Cloud deletion was not confirmed for file: {path} (ID: {drive_id})')
                signal_manager.action_failed.emit()
                return
            self.log_sync_action(action, id=drive_id)
            signal_manager.action_complete.emit(None)
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f'Failed to delete cloud file {path} (ID: {drive_id}): {e}\n{tb}')
            signal_manager.action_failed.emit()
            if not self.cancelled:
                signal_manager.error_occurred.emit('')

    def _make_local_folders(self, folder_path: Path):
        for parent in reversed(folder_path.parents):
            if not parent.exists():
                try:
                    parent.mkdir(exist_ok=True)
                    logging.info(f'Created parent folder: {parent}')
                except Exception as e:
                    logging.error(f'Failed to create parent folder {parent}: {e}')

class _FunctionRunnable(QRunnable):
    """Simple wrapper to run any function in a thread."""
    def __init__(self, func):
        super().__init__()
        self.func = func

    def run(self):
        try:
            self.func()
        except Exception as e:
            logging.error(f'Error in _FunctionRunnable: {e}\n{traceback.format_exc()}')
