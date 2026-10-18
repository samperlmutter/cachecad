from PySide6.QtCore import QObject, Signal, QRunnable, QEventLoop
from PySide6.QtCore import Qt
from PySide6.QtCore import QTimer
from core.signal_manager import signal_manager
import logging
from pathlib import Path
from core.models import SyncActionType
from core.sync_consent import (StaleSyncPlan, approved_action, revalidate_action,
                               staged_revision_upload, validate_selection,
                               project_path, verify_downloaded_revision, verify_folder_creation)
from core.transfer_journal import TransferJournal, PendingTransferRecovery, fsync_directory
from utils.paths import CaseInsensitivePathDict, get_project_app_folder
import threading
from core.thread_manager import thread_pool, network_pool
import os
from send2trash import send2trash
import traceback
import csv
import stat
from types import SimpleNamespace
from app.app_version import APP_VERSION, APP_BUILD_DATE, APP_IS_BETA
from utils.app_settings import settings

class SyncExecutor(QObject):
    thread_finished = Signal()
    guard_failed = Signal(str)
    outcome_ready = Signal(object)

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
        self.action_results = []
        self._result_lock = threading.Lock()
        self._settled_actions = set()
        self._active_operations = {}
        self._counted_actions = set()
        self._transfer_journal = None
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
        self.outcome_ready.connect(self.on_transfer_outcome, Qt.QueuedConnection)
        signal_manager.sync_cancelled.connect(self.on_sync_cancelled)
        QTimer.singleShot(0, lambda: signal_manager.dropped_actions.emit(self.dropped_actions))

    def pre_sync_checks(self):
        action_types = {action[1].action for action in self.action_tuple}
        if SyncActionType.UPLOAD_NEW in action_types:
            self.folders_to_create = self.get_folders_to_create_on_drive()
        if SyncActionType.DOWNLOAD_REVISION in action_types or SyncActionType.DOWNLOAD_UNKNOWN in action_types:
            for index, action in self.action_tuple:
                if action.action in (SyncActionType.DOWNLOAD_REVISION, SyncActionType.DOWNLOAD_UNKNOWN):
                    local_path = Path(self.working_directory) / self.project_name / action.path
                    if self.local_client.is_file_open(local_path):
                        logging.warning(f'File is open: {local_path}. Cannot replace with version from Drive.')
                        self.dropped_actions.append(action)
                        # Preflight drops are still identified user selections.
                        # Retain their outcome, but exclude them from scheduled
                        # worker accounting and do not emit a terminal UI tick.
                        self._settle_transfer((index, action), None, 'not_started',
                            warning='Local file is open; close it and rescan before retrying.',
                            terminal=False)
            self.action_tuple = [action for action in self.action_tuple if action[1] not in self.dropped_actions]

    def start_sync(self):
        if self._started:
            return
        self._started = True
        self.complete_action_count = 0
        self.cancelled_action_count = 0
        self.failed_action_count = 0
        self.completed_actions = []
        if self.consent is not None:
            try:
                validate_selection(self.consent, self._approved_selection,
                                   self.working_directory, self.project_name, self.project_id)
            except Exception as error:
                # A disappeared/unreadable root is stale too; never let a Qt
                # start callback escape without settling the pending executor.
                for action in self.action_tuple:
                    self._settle_transfer(action, None, 'not_started', error=error)
                if not self.action_tuple:
                    self._finish_execution()
                return
        logging.info('Starting sync execution')
        action_count = len(self.action_tuple)
        if action_count == 0:
            self._finish_execution()
            return
        if self.cancelled:
            for action in self.action_tuple:
                self._settle_transfer(action, None, 'not_started',
                    error=StaleSyncPlan('sync was cancelled before starting'), cancelled=True)
            return
        if action_count > 0:
            sync_task = _FunctionRunnable(self.sync_worker)
            try:
                network_pool.start(sync_task)
            except Exception as error:
                for action in self.action_tuple:
                    self._settle_transfer(action, None, 'not_started', error=error)

    def log_sync_action(self, action, id=None):
        log_tuple = action[1].get_log_tuple()
        if id is not None:
            log_tuple = (id,) + log_tuple[1:]
        with self.sync_log_lock:
            path = Path(self.sync_log_path).absolute()
            def inspect_path():
                info = None
                for component in (*reversed(path.parents), path):
                    try:
                        current = component.lstat()
                    except FileNotFoundError:
                        if component != path:
                            raise
                        return None
                    if (stat.S_ISLNK(current.st_mode)
                            or getattr(current, 'st_file_attributes', 0) & 0x400
                            or (hasattr(component, 'is_junction') and component.is_junction())):
                        raise OSError('The sync log path contains a link or reparse point')
                    if component != path and not stat.S_ISDIR(current.st_mode):
                        raise OSError('The sync log ancestor is not a directory')
                    info = current
                if not stat.S_ISREG(info.st_mode) or not info.st_ino or info.st_nlink != 1:
                    raise OSError('The sync log is not a privately owned regular file')
                return info
            def identity(info):
                return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            before = inspect_path()
            flags = os.O_WRONLY | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
            if before is None:
                flags |= os.O_CREAT | os.O_EXCL
            fd = os.open(path, flags, 0o600)
            try:
                opened = os.fstat(fd)
                current = inspect_path()
                if (not stat.S_ISREG(opened.st_mode) or not opened.st_ino or opened.st_nlink != 1
                        or current is None or identity(current) != identity(opened)
                        or before is not None and identity(before) != identity(opened)):
                    raise OSError('The sync log identity changed before append')
                # Passing the validated descriptor avoids a second path open.
                with os.fdopen(fd, mode='a', newline='', encoding='utf-8') as csvfile:
                    fd = None
                    writer = csv.writer(csvfile)
                    if before is None:
                        writer.writerow(['drive_id', 'name', 'path', 'md5', 'modified_time', 'size', 'timestamp', 'action'])
                    writer.writerow(log_tuple)
                    csvfile.flush()
                    os.fsync(csvfile.fileno())
                    after = os.fstat(csvfile.fileno())
                    current = inspect_path()
                    if current is None or after.st_nlink != 1 or identity(current) != identity(after):
                        raise OSError('The sync log identity changed during append')
                if before is None:
                    fsync_directory(path.parent)
            finally:
                if fd is not None:
                    os.close(fd)

    def _prepare_transfer(self, action, kind, observed=None):
        """Persist immutable intent before any target mutation can be attempted."""
        if self._transfer_journal is None:
            root = Path(self.working_directory) / self.project_name
            self._transfer_journal = TransferJournal(root, self.project_id)
        operation = self._transfer_journal.prepare_intent(
            action_kind=kind, path=str(action[1].path),
            approved_local=getattr(observed, 'local', None),
            approved_remote=getattr(observed, 'remote', None),
            expected_payload=({'md5': action[1].md5_checksum, 'size': action[1].size}
                              if kind in ('upload_new', 'download_new') else None))
        with self._result_lock:
            if not hasattr(self, '_active_operations'):
                self._active_operations = {}
            self._active_operations[(action[0], str(action[1].path))] = operation
        return operation

    def _settle_transfer(self, action, operation, outcome, *, error=None,
                         drive_id=None, deleted=False, warning=None,
                         write_log=True, terminal=True, kind=None, cancelled=False,
                         reconcile_only=False):
        """Keep confirmed commits separate from unknown outcomes and log failures.

        Recording, cleanup and UI failures cannot retroactively undo a successful
        mutation. A pending durable record is deliberately retained on all errors;
        no mutation is retried here and recovery never replaces a competing file.
        """
        kind = kind or getattr(action[1].action, 'value', action[1].action)
        key = (action[0], str(action[1].path), str(kind))
        with self._result_lock:
            if key in self._settled_actions:
                return
            self._settled_actions.add(key)
        warnings = [warning] if warning else []
        committed = outcome in ('committed', 'committed_recording_failed')
        if committed and reconcile_only:
            # An interrupted dispatcher can recognize durable commit evidence,
            # but cannot reconstruct provider identity or replay bookkeeping.
            # Completed evidence remains completed; incomplete evidence remains
            # pending explicit review, with no fresh CSV append.
            if outcome == 'committed_recording_failed':
                warnings.append('A committed change has an incomplete local record; review its retained evidence.')
                if operation is not None:
                    try:
                        operation.record_outcome(outcome, error_type=type(error).__name__ if error else None)
                    except Exception:
                        logging.exception('Could not record an interrupted confirmed mutation')
        elif committed:
            try:
                if operation is not None:
                    operation.record_outcome('committed', phase='published', drive_id=drive_id)
                if write_log:
                    if drive_id is None:
                        self.log_sync_action(action)
                    else:
                        self.log_sync_action(action, id=drive_id)
                if operation is not None:
                    operation.record_outcome('committed', phase='completed', drive_id=drive_id)
            except Exception as recording_error:
                outcome = 'committed_recording_failed'
                error = recording_error
                warnings.append('The change committed, but its local completion record could not be saved.')
                if operation is not None:
                    try:
                        operation.record_outcome(outcome, error_type=type(recording_error).__name__)
                    except Exception:
                        logging.exception('A confirmed mutation still has an unresolved durable record')
        elif operation is not None:
            try:
                operation.record_outcome(outcome, error_type=type(error).__name__ if error else None)
            except Exception:
                warnings.append('The latest local outcome record could not be saved; retained evidence requires review.')
                logging.exception('Could not persist a stopped transfer outcome')
        result = {
            'action_index': action[0], 'path': str(action[1].path), 'action': str(kind),
            'outcome': outcome,
            'operation_id': operation.operation_id if operation is not None else None,
            'recovery_dir': str(operation.directory) if operation is not None else None,
            'warnings': tuple(warnings),
            'error_type': type(error).__name__ if error else None,
            'cancelled': bool(cancelled and outcome == 'not_started'),
        }
        with self._result_lock:
            self.action_results.append(result)
        # Results must be visible before terminal signals can finish the batch.
        try:
            if outcome == 'committed_recording_failed':
                self.guard_failed.emit('committed_recording_failed')
            elif not committed and not result['cancelled']:
                code = ('recovery_required' if isinstance(error, PendingTransferRecovery)
                        else 'stale' if isinstance(error, StaleSyncPlan)
                        else 'outcome_unknown' if outcome == 'unknown'
                        else 'download_failed' if str(kind).startswith('download')
                        else 'failed')
                self.guard_failed.emit(code)
        except Exception:
            logging.exception('A transfer warning callback failed')
        try:
            if terminal:
                if committed:
                    signal_manager.action_complete.emit(None if deleted else action[1])
                else:
                    signal_manager.action_failed.emit()
        except Exception:
            # UI progress is not commit evidence and cannot suppress settlement.
            logging.exception('A transfer progress callback failed')
        self.outcome_ready.emit(dict(result))
        return result

    def sync_worker(self):
        logging.debug('Sync worker started')
        loop = QEventLoop()
        self.thread_finished.connect(loop.quit)
        folder_errors = []
        def create_folders_worker():
            try:
                for folder in self.folders_to_create:
                    action = (-1, SimpleNamespace(path=str(folder), action='create_folder'))
                    operation = None
                    attempted = False
                    committed = False
                    cancelled = False
                    folder_id = None
                    error = None
                    try:
                        if self.cancelled:
                            cancelled = True
                            raise StaleSyncPlan('sync was cancelled before creating folders')
                        parent_id = (verify_folder_creation(self.consent, folder, self.drive_client,
                                     self._created_folders) if self.consent is not None else self.get_parent_id(folder))
                        operation = self._prepare_transfer(action, 'create_folder')
                        if self.cancelled:
                            cancelled = True
                            raise StaleSyncPlan('sync was cancelled before creating folders')
                        operation.record_outcome('unknown', phase='mutation_started', parent_id=parent_id)
                        attempted = True
                        folder_id = self.drive_client.create_folder(folder.name, parent_id)
                        if not isinstance(folder_id, str) or not folder_id:
                            raise RuntimeError('Drive folder creation was not confirmed')
                        committed = True
                        self._created_folders[str(folder)] = folder_id
                        self.update_path_dict(str(folder), folder_id)
                    except Exception as stopped:
                        error = stopped
                    result = self._settle_transfer(action, operation,
                        'committed' if committed else 'unknown' if attempted else 'not_started',
                        error=error, drive_id=folder_id if committed else None,
                        warning='The folder was created, but local bookkeeping needs review.' if committed and error else None,
                        write_log=False, terminal=False, kind='create_folder', cancelled=cancelled)
                    if error is not None or result['outcome'] != 'committed':
                        raise error or RuntimeError('Folder completion could not be recorded')
            except Exception as error:
                folder_errors.append(error)
                logging.error(f'Folder creation stopped: {error}')
            finally:
                self.thread_finished.emit()
        if len(self.folders_to_create) > 0:
            create_folders_task = _FunctionRunnable(create_folders_worker)
            try:
                thread_pool.start(create_folders_task)
                loop.exec_()
            except Exception as error:
                folder_errors.append(error)
        if folder_errors or self.cancelled:
            error = folder_errors[0] if folder_errors else StaleSyncPlan('sync was cancelled before transfer')
            for action in self.action_tuple:
                self._settle_transfer(action, None, 'not_started', error=error,
                                      cancelled=bool(self.cancelled and not folder_errors))
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
            else:
                self._settle_transfer(action, None, 'not_started', error=ValueError('Unknown sync action'))
        if len(self.action_tuple) > 0:
            def run_action(action):
                try:
                    action_worker(action)
                except Exception as error:
                    # An unexpected dispatch error must settle the identified
                    # action. Journal evidence distinguishes an unstarted action
                    # from a mutation whose response/recording was interrupted.
                    self._settle_unexpected(action, error)
            for position, action in enumerate(self.action_tuple):
                try:
                    action_task = _FunctionRunnable(lambda a=action: run_action(a))
                    network_pool.start(action_task)
                except Exception as error:
                    for unscheduled in self.action_tuple[position:]:
                        self._settle_transfer(unscheduled, None, 'not_started', error=error)
                    break

    def _settle_unexpected(self, action, error):
        with self._result_lock:
            existing = next((item for item in self.action_results
                             if (item['action_index'], item['path']) == (action[0], str(action[1].path))), None)
        if existing is not None:
            # Retrying a notification is safe; replaying a mutation or CSV append
            # is not. The authoritative receiver ignores duplicate identities.
            self.outcome_ready.emit(dict(existing))
            return
        operation = getattr(self, '_active_operations', {}).get((action[0], str(action[1].path)))
        outcome = 'not_started' if operation is None else 'unknown'
        reconcile_only = False
        if operation is not None:
            try:
                state = operation.journal.read(operation.operation_id)['state']
                outcome = state['outcome']
                if outcome in ('committed', 'committed_recording_failed'):
                    reconcile_only = True
                    if not (outcome == 'committed' and state['phase'] == 'completed'):
                        outcome = 'committed_recording_failed'
            except Exception:
                logging.exception('Could not read interrupted action state')
        self._settle_transfer(action, operation, outcome, error=error,
                              reconcile_only=reconcile_only)

    def on_transfer_outcome(self, result):
        """Only identified outcomes from this executor may settle its batch."""
        if self._finished or result.get('action') == 'create_folder':
            return
        key = (result.get('action_index'), result.get('path'))
        selected = next((item for index, item in self.action_tuple
                         if (index, str(item.path)) == key), None)
        if selected is None or key in self._counted_actions:
            return
        self._counted_actions.add(key)
        if result['outcome'] in ('committed', 'committed_recording_failed'):
            self.completed_actions.append(selected)
            self.complete_action_count += 1
        elif result.get('cancelled'):
            self.cancelled_action_count += 1
        else:
            self.failed_action_count += 1
        self.check_if_all_actions_finished()

    def on_action_complete(self, action):
        # Legacy global UI notifications carry no unique operation identity.
        # They are deliberately not trusted as batch-settlement evidence.
        return

    def on_action_failed(self):
        return

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
                (self.outcome_ready, self.on_transfer_outcome),
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
        operation = None
        attempted = False
        committed = False
        cancelled = False
        error = None
        warning = None
        kind = (getattr(getattr(action[1], 'action', None), 'value', None)
                or ('download_revision' if revision else 'download_new'))
        try:
            import hashlib
            import stat
            import tempfile
            if self.cancelled:
                cancelled = True
                raise StaleSyncPlan('the download was cancelled before staging')
            selected = action[1]
            consent = getattr(self, 'consent', None)
            observed = None
            if revision or consent is not None:
                observed = revalidate_action(consent, action, self.drive_client)
                if self.cancelled:
                    cancelled = True
                    raise StaleSyncPlan('the download was cancelled before transfer')
            relative = Path(selected.path)
            if relative.is_absolute() or not relative.parts or '..' in relative.parts:
                raise ValueError('Download path must remain inside the project')
            root = (Path(self.working_directory) / self.project_name).resolve()
            local_path = (project_path(consent.snapshot.project_root, selected.path)
                          if consent is not None else root / relative)
            if not local_path.resolve().is_relative_to(root):
                raise ValueError('Download path resolves outside the project')
            expected_md5 = observed.remote.md5 if revision else selected.md5_checksum
            expected_size = observed.remote.size if revision else selected.size
            if (not isinstance(expected_md5, str) or len(expected_md5) != 32
                    or any(c not in '0123456789abcdefABCDEF' for c in expected_md5)
                    or type(expected_size) is not int or expected_size < 0):
                raise ValueError('Selected download lacks a valid checksum or size')
            if not revision and os.path.lexists(local_path):
                raise FileExistsError('New download destination already exists')
            operation = self._prepare_transfer(action, kind, observed)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            prefix = '.cachecad-revision-download-' if revision else '.cachecad-download-'
            with tempfile.TemporaryDirectory(prefix=prefix, dir=str(local_path.parent)) as folder:
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
                # All recoverable bytes, including an independent publication
                # copy, are prepared and fsynced before the destructive boundary.
                operation.preserve_incoming(temp_path, md5=expected_md5, size=expected_size)
                if revision:
                    operation.preserve_local_backup(local_path, md5=observed.local.md5,
                        size=observed.local.size, identity=observed.local.identity)
                operation.verify_recovery_material()
                if self.cancelled:
                    cancelled = True
                    raise StaleSyncPlan('the download was cancelled before publication')
                if consent is not None:
                    # This remains an observed-version guard, not a filesystem
                    # compare-and-swap. A residual final check/Trash race remains.
                    revalidate_action(consent, action, self.drive_client)
                    if self.cancelled:
                        cancelled = True
                        raise StaleSyncPlan('the download was cancelled before publication')
                operation.record_outcome('unknown', phase='mutation_started')
                attempted = True
                if revision:
                    send2trash(local_path)
                try:
                    # No-clobber publication. Never restore over a newer arrival.
                    operation.publish_incoming()
                except Exception as publication_error:
                    committed = bool(getattr(publication_error, 'committed', False))
                    raise
                committed = True
        except Exception as stopped:
            error = stopped
            if committed:
                warning = 'The change committed, but post-publication durability or staging cleanup needs review.'
            logging.error('Download stopped (%s); committed=%s', type(stopped).__name__, committed)
        self._settle_transfer(action, operation,
            'committed' if committed else 'unknown' if attempted else 'not_started',
            error=error, warning=warning, kind=kind, cancelled=cancelled)

    def _upload(self, action, revision=False):
        operation = None
        attempted = False
        committed = False
        cancelled = False
        new_id = None
        error = None
        warning = None
        kind = (getattr(getattr(action[1], 'action', None), 'value', None)
                or ('upload_revision' if revision else 'upload_new'))
        try:
            if self.cancelled:
                cancelled = True
                raise StaleSyncPlan('the upload was cancelled before staging')
            path = action[1].path
            local_path = Path(self.working_directory) / self.project_name / path
            parent_id = self.get_parent_id(path)
            existing_drive_id = action[1].drive_id if revision else None
            idx = action[0]
            if revision:
                consent = getattr(self, 'consent', None)
                observed = approved_action(consent, action)
                expected_md5 = observed.local.md5
                operation = self._prepare_transfer(action, kind, observed)
                with staged_revision_upload(consent, action) as staged_path:
                    operation.preserve_incoming(staged_path, md5=expected_md5, size=observed.local.size)
                    operation.verify_recovery_material()
                    revalidate_action(consent, action, self.drive_client)
                    if self.cancelled:
                        cancelled = True
                        raise StaleSyncPlan('the upload was cancelled before transfer')
                    operation.record_outcome('unknown', phase='mutation_started',
                        parent_id=parent_id, requested_drive_id=existing_drive_id)
                    attempted = True
                    # No validated Drive CAS: a remote writer may race this check.
                    new_id, drive_md5 = self.drive_client.upload_file(
                        str(staged_path), parent_id, idx, existing_drive_id)
                    committed = (new_id == existing_drive_id and isinstance(new_id, str)
                        and bool(new_id) and isinstance(drive_md5, str)
                        and drive_md5.lower() == expected_md5.lower())
                    if not committed:
                        raise RuntimeError('Upload commit identity or checksum was not confirmed')
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
                consent = getattr(self, 'consent', None)
                observed = approved_action(consent, action) if consent is not None else None
                operation = self._prepare_transfer(action, kind, observed)
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
                    operation.preserve_incoming(staged_path, md5=expected_md5, size=expected_size)
                    operation.verify_recovery_material()
                    if self.cancelled:
                        cancelled = True
                        raise StaleSyncPlan('the upload was cancelled before transfer')
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
                            cancelled = True
                            raise StaleSyncPlan('the upload was cancelled before transfer')
                    operation.record_outcome('unknown', phase='mutation_started',
                        parent_id=parent_id, requested_drive_id=existing_drive_id)
                    attempted = True
                    new_id, drive_md5 = self.drive_client.upload_file(str(staged_path), parent_id, idx, None)
                    committed = (isinstance(new_id, str) and bool(new_id)
                        and isinstance(drive_md5, str) and drive_md5.lower() == expected_md5.lower())
                    if not committed:
                        raise RuntimeError('Upload commit identity or checksum was not confirmed')
        except Exception as stopped:
            error = stopped
            if committed:
                warning = 'The upload committed, but staging cleanup needs review.'
            logging.error('Upload stopped (%s); committed=%s', type(stopped).__name__, committed)
        self._settle_transfer(action, operation,
            'committed' if committed else 'unknown' if attempted else 'not_started',
            error=error, drive_id=new_id if committed else None, warning=warning, kind=kind, cancelled=cancelled)

    def _delete_local(self, action):
        operation = None
        attempted = False
        committed = False
        cancelled = False
        error = None
        try:
            if self.cancelled:
                cancelled = True
                raise StaleSyncPlan('local deletion was cancelled before staging')
            observed = revalidate_action(getattr(self, 'consent', None), action, self.drive_client)
            local_path = project_path(self.consent.snapshot.project_root, action[1].path)
            operation = self._prepare_transfer(action, 'delete_local', observed)
            operation.preserve_local_backup(local_path, md5=observed.local.md5,
                size=observed.local.size, identity=observed.local.identity)
            operation.verify_recovery_material()
            revalidate_action(self.consent, action, self.drive_client)
            if self.cancelled:
                cancelled = True
                raise StaleSyncPlan('local deletion was cancelled before moving the file')
            operation.record_outcome('unknown', phase='mutation_started')
            attempted = True
            send2trash(local_path)
            committed = True
        except Exception as stopped:
            error = stopped
            logging.error('Local deletion stopped (%s); committed=%s', type(stopped).__name__, committed)
        self._settle_transfer(action, operation,
            'committed' if committed else 'unknown' if attempted else 'not_started',
            error=error, deleted=True, kind='delete_local', cancelled=cancelled)

    def _delete_cloud(self, action):
        operation = None
        attempted = False
        committed = False
        cancelled = False
        error = None
        drive_id = action[1].drive_id
        try:
            if self.cancelled:
                cancelled = True
                raise StaleSyncPlan('cloud deletion was cancelled before staging')
            observed = revalidate_action(getattr(self, 'consent', None), action, self.drive_client)
            operation = self._prepare_transfer(action, 'delete_cloud', observed)
            if self.cancelled:
                cancelled = True
                raise StaleSyncPlan('cloud deletion was cancelled before moving the file')
            operation.record_outcome('unknown', phase='mutation_started')
            attempted = True
            if not self.drive_client.delete_file(drive_id):
                raise RuntimeError('Cloud deletion was not confirmed')
            committed = True
        except Exception as stopped:
            error = stopped
            logging.error('Cloud deletion stopped (%s); committed=%s', type(stopped).__name__, committed)
        self._settle_transfer(action, operation,
            'committed' if committed else 'unknown' if attempted else 'not_started',
            error=error, drive_id=drive_id if committed else None, deleted=True, kind='delete_cloud', cancelled=cancelled)

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
