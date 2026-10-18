import os
import hashlib
import json
from pathlib import Path
from typing import Optional
import logging
import traceback

from PySide6.QtCore import QObject, Signal, QRunnable
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



    def get_local_items_from_path(self, project_name: str, working_dir: str):

        logging.info("Fetching Local Items from " + project_name)
        print("Fetching Local Items from " + project_name)
        self.project_name = project_name
        self.working_dir = Path(working_dir).resolve()
        project_folder = self.working_dir / self.project_name

        self._folder_dict = {}
        self._file_dict = {}
        self._active_signals = []
        files_to_hash = []

        for path in project_folder.rglob("*"):
            rel_path = path.relative_to(project_folder)
            rel_path_str = str(rel_path)

            if path.is_dir():
                if any(part.startswith(".") for part in rel_path.parts):
                    continue
                info = LocalFileInfo.from_path(path, project_folder)
                self._folder_dict[rel_path_str] = info

            elif path.is_file():
                cache_key = str(path.resolve())
                stat = path.stat()
                size = stat.st_size
                mtime = stat.st_mtime

                cached = self.md5_cache.get(cache_key) if self.md5_cache else None
                md5 = None
                if cached and cached["size"] == size and cached["modified_time"] == mtime:
                    md5 = cached["md5"]


                if path.name.startswith(".") or path.name.startswith("~$") or path.parent.name == ".cachecad":
                    continue

                info = LocalFileInfo.from_path(path, project_folder, md5_checksum=md5)
                self._file_dict[rel_path_str] = info

                if md5 is None:
                    files_to_hash.append((path, rel_path_str))

        self._pending_md5 = len(files_to_hash)

        if self._pending_md5 == 0:
            self._emit_complete()
            return

        logging.info("Calculating Hashes for " + str(len(files_to_hash)) + " files")
        for path, rel_path_str in files_to_hash:
            signals = HashWorkerSignals()
            self._active_signals.append(signals)
            signals.finished.connect(self._on_hash_result)
            signals.error.connect(self._on_hash_error)
            worker = HashWorker(path, rel_path_str, signals)
            thread_pool.start(worker)


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
