# Maintained derivative: isolated, generation-tagged, fail-closed local scans.
import os
import hashlib
import json
from pathlib import Path
from typing import Optional
import logging
import traceback
from threading import RLock

from PySide6.QtCore import QObject, Signal, QRunnable
from PySide6.QtCore import Qt
from core.models import LocalFileInfo
from core.thread_manager import thread_pool
from core.signal_manager import signal_manager
from utils.app_settings import settings


class HashWorkerSignals(QObject):
    finished = Signal(str, str)
    error = Signal(str, str)


class HashWorker(QRunnable):
    def __init__(self, path: Path, rel_path: str, signals: HashWorkerSignals):
        super().__init__()
        self.path = path
        self.rel_path = rel_path
        self.signals = signals

    def run(self):
        try:
            md5 = self._calculate_md5(self.path)
            self.signals.finished.emit(self.rel_path, md5)
            logging.debug(f"Hash success: {self.rel_path} = {md5}")
        except Exception as e:
            tb = traceback.format_exc()
            logging.exception(f"Unexpected error in HashWorker: {e} \n {tb}")
            self.signals.error.emit(self.rel_path, str(e))


    def _calculate_md5(self, path: Path, chunk_size: int = 8192) -> str:
        hash_md5 = hashlib.md5()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()


class LocalClient(QObject):

    def __init__(self):
        super().__init__()
        self.project_name = None
        self.working_dir = settings.get_working_directory()
        self.cache_path = None
        self.md5_cache = self._load_md5_cache() if self.cache_path else {}
        self._pending_md5 = 0
        self._file_dict = {}
        self._active_signals = []
        self._scan_states = {}



    def get_local_items_from_path(self, project_name: str, working_dir: str, *,
                                  scan_id=None, on_success=None, on_error=None):
        """Keep every async hash and terminal outcome tied to this invocation."""
        tagged = scan_id is not None or on_success is not None or on_error is not None
        if tagged and (scan_id is None or not callable(on_success) or not callable(on_error)):
            raise ValueError('Tagged scans require scan_id, on_success, and on_error')
        token = object()
        state = {'signals': [], 'pending': set(), 'settled': False}
        self._scan_states[token] = state
        state_lock = RLock()
        folders = {}
        files = {}
        cache_updates = {}

        def release_if_finished():
            if not state['pending']:
                self._scan_states.pop(token, None)
                state['signals'].clear()

        def fail(message):
            with state_lock:
                if state['settled']:
                    return
                state['settled'] = True
                try:
                    logging.error(message)
                    if tagged:
                        on_error(scan_id, message)
                    else:
                        signal_manager.error_occurred.emit(message)
                finally:
                    release_if_finished()

        def complete():
            with state_lock:
                if state['settled'] or state['pending']:
                    return
                items = list(folders.values()) + list(files.values())
                # Only the analyzer can accept a tagged generation. Never let an
                # obsolete tagged scan commit shared dictionaries or MD5 cache.
                if not tagged:
                    self._folder_dict = folders
                    self._file_dict = files
                    self.md5_cache.update(cache_updates)
                    self._save_md5_cache()
                state['settled'] = True
                try:
                    if tagged:
                        on_success(scan_id, items)
                    else:
                        signal_manager.local_file_list_acquired.emit(items)
                finally:
                    release_if_finished()

        def hash_result(rel_path, md5):
            with state_lock:
                if rel_path not in state['pending']:
                    return
                state['pending'].remove(rel_path)
                if state['settled']:
                    release_if_finished()
                    return
                try:
                    if not isinstance(md5, str) or not md5:
                        raise ValueError('Missing checksum for ' + rel_path)
                    info = files[rel_path]
                    info.md5_checksum = md5
                    cache_updates[info.full_path] = {
                        'size': info.size, 'modified_time': info.modified_time, 'md5': md5}
                    complete()
                except Exception as error:
                    fail('Local scan incomplete: ' + str(error))

        def hash_error(rel_path, error_msg):
            with state_lock:
                if rel_path not in state['pending']:
                    return
                state['pending'].remove(rel_path)
                fail('Local scan incomplete: failed to hash ' + rel_path + ': ' + str(error_msg))
                release_if_finished()

        def walk_error(error):
            raise error

        try:
            logging.info('Fetching Local Items from ' + project_name)
            project_folder = Path(working_dir).resolve() / project_name
            if project_folder.is_symlink():
                raise OSError('Symlink project roots are not supported: ' + str(project_folder))
            if not project_folder.is_dir():
                raise FileNotFoundError('Project directory is unavailable: ' + str(project_folder))
            cache = dict(self.md5_cache)
            files_to_hash = []
            # pathlib.rglob can suppress traversal errors. An explicit onerror
            # ensures an unreadable subtree cannot masquerade as local deletion.
            for root, dirnames, filenames in os.walk(project_folder, onerror=walk_error):
                # Recovery material and journals are private app state, never CAD
                # inputs. Prune the reserved root before os.walk descends into it.
                if Path(root) == project_folder:
                    for name in tuple(dirnames):
                        if name.casefold() == '.cachecad':
                            internal = Path(root) / name
                            if internal.is_symlink() or (hasattr(internal, 'is_junction') and internal.is_junction()):
                                raise OSError('Linked internal project state is not supported')
                            dirnames.remove(name)
                for name in dirnames + filenames:
                    path = Path(root) / name
                    if path.is_symlink():
                        raise OSError('Symlink entries are not supported: ' + str(path))
                    rel_path = path.relative_to(project_folder)
                    rel_path_str = str(rel_path)
                    if path.is_dir():
                        if any(part.startswith('.') for part in rel_path.parts):
                            continue
                        folders[rel_path_str] = LocalFileInfo.from_path(path, project_folder)
                    elif path.is_file():
                        stat = path.stat()
                        cached = cache.get(str(path.resolve()))
                        md5 = None
                        if cached and cached['size'] == stat.st_size and cached['modified_time'] == stat.st_mtime:
                            md5 = cached['md5']
                        if path.name.startswith('.') or path.name.startswith('~$') or path.parent.name == '.cachecad':
                            continue
                        files[rel_path_str] = LocalFileInfo.from_path(path, project_folder, md5_checksum=md5)
                        if md5 is None:
                            files_to_hash.append((path, rel_path_str))
                    else:
                        raise OSError('Scanned path became unavailable: ' + str(path))
            state['pending'] = {rel_path for _, rel_path in files_to_hash}
            if not state['pending']:
                complete()
                return
            for path, rel_path in files_to_hash:
                with state_lock:
                    if state['settled']:
                        state['pending'].discard(rel_path)
                        release_if_finished()
                        continue
                try:
                    signals = HashWorkerSignals()
                    state['signals'].append(signals)
                    # These callbacks only touch lock-protected scan state;
                    # analyzer outcomes are separately queued to its QObject.
                    signals.finished.connect(hash_result, Qt.ConnectionType.DirectConnection)
                    signals.error.connect(hash_error, Qt.ConnectionType.DirectConnection)
                    worker = HashWorker(path, rel_path, signals)
                    thread_pool.start(worker)
                except Exception as error:
                    with state_lock:
                        state['pending'].discard(rel_path)
                        fail('Local scan incomplete: ' + str(error))
                        release_if_finished()
        except Exception as error:
            fail('Local scan incomplete: ' + str(error))


    def _on_hash_result(self, rel_path, md5):
        if rel_path in self._file_dict:
            info = self._file_dict[rel_path]
            info.md5_checksum = md5
            if self.cache_path:
                self.md5_cache[info.full_path] = {
                    "size": info.size,
                    "modified_time": info.modified_time,
                    "md5": md5
                }
        self._check_scan_complete()

    def _on_hash_error(self, rel_path, error_msg):
        print(f"Failed to hash {rel_path}: {error_msg}")
        logging.error(f"Failed to hash {rel_path}: {error_msg}")
        self._check_scan_complete()

    def _check_scan_complete(self):
        self._pending_md5 -= 1

        if self._pending_md5 == 0:
            self._emit_complete()

    def _emit_complete(self):
        self._save_md5_cache()
        logging.info("Local Files Found: " + str(len(self._file_dict)))
        logging.info("Local Folders Found: " + str(len(self._folder_dict)))
        local_items = list(self._folder_dict.values()) + list(self._file_dict.values())
        signal_manager.local_file_list_acquired.emit(local_items)
        self._active_signals.clear()


    def _load_md5_cache(self):
        if not self.cache_path or not self.cache_path.exists():
            return {}
        try:
            with self.cache_path.open("r") as f:
                return json.load(f)
        except Exception as e:
            tb = traceback.format_exc()
            logging.warning(f"Warning: Failed to load MD5 cache: {e} \n {tb}")
            return {}

    def _save_md5_cache(self):
        if not self.cache_path:
            return
        try:
            with self.cache_path.open("w") as f:
                json.dump(self.md5_cache, f)
        except Exception as e:
            tb = traceback.format_exc()
            logging.warning(f"Warning: Failed to save MD5 cache: {e} \n {tb}")

    def calculate_md5(self, path: Path) -> Optional[str]:
        try:
            return HashWorker._calculate_md5(self, path)
        except Exception as e:
            tb = traceback.format_exc()
            print(f"Error calculating MD5 for {path}: {e} \n {tb}")
            signal_manager.error_occurred.emit("")
            return None

    def is_file_open(self, path):
        if not os.path.exists(path):
            return False

        try:

            with open(path, "r+"):
                return False
        except (OSError, PermissionError):
            return True
