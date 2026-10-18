from PySide6.QtCore import QObject, Signal, QRunnable, QEventLoop
from PySide6.QtCore import QTimer
from core.signal_manager import signal_manager
import logging
from pathlib import Path
from core.models import SyncActionType
from core.sync_consent import (StaleSyncPlan, approved_action, revalidate_action,
                               staged_revision_upload, validate_selection,
                               project_path, verify_downloaded_revision, verify_folder_creation)
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
    guard_failed = Signal(str)

    def __init__(self, drive_client, local_client, project_name, project_id, working_directory, action_tuple, path_dict: CaseInsensitivePathDict, *, consent=None):
        super().__init__()
        self.drive_client = drive_client
        self.local_client = local_client
        self.project_name = project_name
        self.project_id = project_id
        self.working_directory = working_directory
        self.action_tuple = tuple(action_tuple)
        self.consent = consent
        self._approved_selection = self.action_tuple
        self._started = False
        self._finished = False
        self._created_folders = {}
        self.cancelled = False
        if consent is not None or any(item.action in (
                SyncActionType.UPLOAD_REVISION, SyncActionType.DOWNLOAD_REVISION,
                SyncActionType.UPLOAD_UNKNOWN, SyncActionType.DOWNLOAD_UNKNOWN,
                SyncActionType.DELETE_LOCAL, SyncActionType.DELETE_CLOUD)
                for _, item in self.action_tuple):
            validate_selection(consent, self.action_tuple, working_directory, project_name, project_id)
        self.path_dict = path_dict
        self.path_dict_lock = threading.Lock()
        self.sync_log_lock = threading.Lock()
        self.sync_log_path = get_project_app_folder(working_directory, project_name) / 'sync_log.csv'
        self.dropped_actions = []
        self.folders_to_create = []
        self.pre_sync_checks()
        signal_manager.dropped_actions_acknowledged.connect(self.start_sync)
        signal_manager.action_complete.connect(self.on_action_complete)
        signal_manager.action_failed.connect(self.on_action_failed)
        signal_manager.sync_cancelled.connect(self.on_sync_cancelled)
        QTimer.singleShot(0, lambda: signal_manager.dropped_actions.emit(self.dropped_actions))

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
        if self._started:
            return
        self._started = True
        if self.consent is not None:
            try:
                validate_selection(self.consent, self._approved_selection,
                                   self.working_directory, self.project_name, self.project_id)
            except Exception:
                # A disappeared/unreadable root is stale too; never let a Qt
                # start callback escape without settling the pending executor.
                self.guard_failed.emit('stale')
                self._finish_execution()
                return
        logging.info('Starting sync execution')
        action_count = len(self.action_tuple)
        if self.cancelled or action_count == 0:
            self._finish_execution()
            return
        if action_count > 0:
            self.complete_action_count = 0
            self.cancelled_action_count = 0
            self.failed_action_count = 0
            self.completed_actions = []
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
        folder_errors = []
        def create_folders_worker():
            try:
                for folder in self.folders_to_create:
                    if self.cancelled:
                        raise StaleSyncPlan('sync was cancelled before creating folders')
                    parent_id = (verify_folder_creation(self.consent, folder, self.drive_client,
                                 self._created_folders) if self.consent is not None else self.get_parent_id(folder))
                    if self.cancelled:
                        raise StaleSyncPlan('sync was cancelled before creating folders')
                    folder_id = self.drive_client.create_folder(folder.name, parent_id)
                    if folder_id is None:
                        raise StaleSyncPlan(f'the Drive folder {folder} could not be created')
                    self._created_folders[str(folder)] = folder_id
                    self.update_path_dict(str(folder), folder_id)
            except Exception as error:
                folder_errors.append(error)
                logging.error(f'Folder creation stopped: {error}')
            finally:
                self.thread_finished.emit()
        if len(self.folders_to_create) > 0:
            create_folders_task = _FunctionRunnable(create_folders_worker)
            thread_pool.start(create_folders_task)
            loop.exec_()
        if folder_errors or self.cancelled:
            if folder_errors and not self.cancelled:
                self.guard_failed.emit('failed')
            for _ in self.action_tuple:
                signal_manager.action_failed.emit()
            return
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
        if self._finished:
            return
        self.completed_actions.append(action)
        self.complete_action_count += 1
        self.check_if_all_actions_finished()

    def on_action_failed(self):
        if self._finished:
            return
        if self.cancelled:
            self.cancelled_action_count += 1
        else:
            self.failed_action_count += 1
        self.check_if_all_actions_finished()

    def check_if_all_actions_finished(self):
        if self.failed_action_count + self.complete_action_count + self.cancelled_action_count == len(self.action_tuple):
            self._finish_execution()

    def _finish_execution(self):
        if self._finished:
            return
        self._finished = True
        # Global signals must not retain a finished executor into another run.
        for signal, slot in (
                (signal_manager.dropped_actions_acknowledged, self.start_sync),
                (signal_manager.action_complete, self.on_action_complete),
                (signal_manager.action_failed, self.on_action_failed),
                (signal_manager.sync_cancelled, self.on_sync_cancelled)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        logging.info('All sync actions settled')
        signal_manager.sync_execution_finished.emit()
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
            if file.action == SyncActionType.UPLOAD_NEW and file.folder is not None
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
        # publication. Revisions additionally require approved observed versions.
        if not revision:
            try:
                import hashlib
                import stat
                import tempfile
                if self.cancelled:
                    raise RuntimeError('Download cancelled before staging')
                selected = action[1]
                consent = getattr(self, 'consent', None)
                if consent is not None:
                    revalidate_action(consent, action, self.drive_client)
                    if self.cancelled:
                        raise StaleSyncPlan('the download was cancelled before transfer')
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
                    if consent is not None:
                        revalidate_action(consent, action, self.drive_client)
                        if self.cancelled:
                            raise StaleSyncPlan('the download was cancelled before publication')
                    # os.link creates the destination only if it is absent. No
                    # rename/replace fallback is allowed on unsupported filesystems.
                    os.link(str(temp_path), str(local_path), follow_symlinks=False)
                self.log_sync_action(action)
            except Exception as e:
                logging.error(f'New download failed without replacing an existing file: {e}\n{traceback.format_exc()}')
                failure_signal = getattr(self, 'guard_failed', None)
                if not self.cancelled and failure_signal is not None:
                    failure_signal.emit('stale' if isinstance(e, StaleSyncPlan) else 'failed')
                signal_manager.action_failed.emit()
                return
            signal_manager.action_complete.emit(action[1])
            return
        if self.cancelled:
            signal_manager.action_failed.emit()
            return
        try:
            import tempfile
            observed = revalidate_action(getattr(self, 'consent', None), action, self.drive_client)
            if self.cancelled:
                raise StaleSyncPlan('the download was cancelled before transfer')
            local_path = project_path(self.consent.snapshot.project_root, action[1].path)
            with tempfile.TemporaryDirectory(prefix='.cachecad-revision-download-', dir=str(local_path.parent)) as folder:
                returned = self.drive_client.download_file(
                    action[1].drive_id, str(Path(folder) / local_path.name), action[0])
                temp_path = verify_downloaded_revision(returned, folder, observed.remote)
                if self.cancelled:
                    raise StaleSyncPlan('the download was cancelled before replacing a file')
                # Recheck both sides after transfer, immediately before trashing.
                # This is an observed-version guard, not a conditional filesystem
                # operation; a residual check/trash race remains.
                revalidate_action(self.consent, action, self.drive_client)
                if self.cancelled:
                    raise StaleSyncPlan('the download was cancelled before replacing a file')
                send2trash(local_path)
                # Never clobber a new object that appears after the approved one
                # was trashed. No replacing rename fallback is permitted.
                os.link(str(temp_path), str(local_path), follow_symlinks=False)
            self.log_sync_action(action)
        except Exception as error:
            logging.error(f'Revision download stopped: {error}\n{traceback.format_exc()}')
            failure_signal = getattr(self, 'guard_failed', None)
            if not self.cancelled and failure_signal is not None:
                failure_signal.emit('stale' if isinstance(error, StaleSyncPlan) else 'download_failed')
            signal_manager.action_failed.emit()
            return
        signal_manager.action_complete.emit(action[1])

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
                consent = getattr(self, 'consent', None)
                observed = approved_action(consent, action)
                expected_md5 = observed.local.md5
                # Upload only the immutable bytes the user approved. Never hand
                # the live CAD file to a resumable transfer.
                with staged_revision_upload(consent, action) as staged_path:
                    revalidate_action(consent, action, self.drive_client)
                    if self.cancelled:
                        raise StaleSyncPlan('the upload was cancelled before transfer')
                    # Drive v3 update has no validated compare-and-swap in this
                    # app. A writer can still race the final check and update.
                    new_id, drive_md5 = self.drive_client.upload_file(
                        str(staged_path), parent_id, idx, existing_drive_id)
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
                    consent = getattr(self, 'consent', None)
                    if consent is not None:
                        observed = revalidate_action(consent, action, self.drive_client,
                            created_folders=getattr(self, '_created_folders', {}))
                        if len(observed.folders) != len(relative.parts) - 1:
                            raise StaleSyncPlan('the new upload destination has no verified parent')
                        verified_parent = (observed.folders[-1].file_id if observed.folders
                                           else consent.snapshot.project_id)
                        if parent_id != verified_parent:
                            raise StaleSyncPlan('the upload destination parent changed')
                        if self.cancelled:
                            raise StaleSyncPlan('the upload was cancelled before transfer')
                    new_id, drive_md5 = self.drive_client.upload_file(str(staged_path), parent_id, idx, None)
        except Exception as e:
            logging.error(f'Upload failed before a result was available: {e}\n{traceback.format_exc()}')
            failure_signal = getattr(self, 'guard_failed', None)
            if not self.cancelled and failure_signal is not None:
                failure_signal.emit('stale' if isinstance(e, StaleSyncPlan) else 'failed')
            signal_manager.action_failed.emit()
            return
        logging.info(f'File uploaded to : {new_id}')
        logging.info(f'MD5 checksum of uploaded file: {drive_md5}')
        checksum_matches = isinstance(drive_md5, str) and drive_md5.lower() == expected_md5.lower()
        if new_id is not None and checksum_matches:
            logging.info(f'MD5 checksum matches for uploaded file: {local_path}')
            try:
                self.log_sync_action(action, id=new_id)
            except Exception:
                logging.exception('Upload completed but its local sync log could not be written')
                failure_signal = getattr(self, 'guard_failed', None)
                if failure_signal is not None:
                    failure_signal.emit('failed')
                signal_manager.action_failed.emit()
                return
            signal_manager.action_complete.emit(action[1])
        else:
            logging.error(f'Upload failed (no ID returned) for file: {local_path}')
            failure_signal = getattr(self, 'guard_failed', None)
            if failure_signal is not None:
                failure_signal.emit('failed')
            signal_manager.action_failed.emit()

    def _delete_local(self, action):
        if self.cancelled:
            signal_manager.action_failed.emit()
            return
        try:
            revalidate_action(getattr(self, 'consent', None), action, self.drive_client)
            if self.cancelled:
                raise StaleSyncPlan('local deletion was cancelled before moving the file')
            local_path = project_path(self.consent.snapshot.project_root, action[1].path)
            send2trash(local_path)
            self.log_sync_action(action)
        except Exception as error:
            logging.error(f'Local deletion stopped: {error}\n{traceback.format_exc()}')
            failure_signal = getattr(self, 'guard_failed', None)
            if not self.cancelled and failure_signal is not None:
                failure_signal.emit('stale' if isinstance(error, StaleSyncPlan) else 'failed')
            signal_manager.action_failed.emit()
            return
        signal_manager.action_complete.emit(None)

    def _delete_cloud(self, action):
        if self.cancelled:
            signal_manager.action_failed.emit()
            return
        try:
            revalidate_action(getattr(self, 'consent', None), action, self.drive_client)
            if self.cancelled:
                raise StaleSyncPlan('cloud deletion was cancelled before moving the file')
            drive_id = action[1].drive_id
            if not self.drive_client.delete_file(drive_id):
                raise RuntimeError('Cloud deletion was not confirmed')
            self.log_sync_action(action, id=drive_id)
        except Exception as error:
            logging.error(f'Cloud deletion stopped: {error}\n{traceback.format_exc()}')
            failure_signal = getattr(self, 'guard_failed', None)
            if not self.cancelled and failure_signal is not None:
                failure_signal.emit('stale' if isinstance(error, StaleSyncPlan) else 'failed')
            signal_manager.action_failed.emit()
            return
        signal_manager.action_complete.emit(None)

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
