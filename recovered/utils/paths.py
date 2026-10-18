import sys, logging, traceback
from pathlib import Path
import re

def get_base_dir():
    return Path.cwd()

BASE_DIR = get_base_dir()
APP_DATA_DIR = BASE_DIR / 'app_data'
RESOURCES_DIR = BASE_DIR / 'resources'
APP_LOG_DIR = BASE_DIR / 'logs'
APP_DATA_DIR.mkdir(exist_ok=True)
APP_LOG_DIR.mkdir(exist_ok=True)

def get_project_app_folder(working_dir: str, project_name: str) -> Path:
    if isinstance(working_dir, Path):
        working_dir_path = working_dir
    else:
        working_dir_path = Path(working_dir)
    project_folder = working_dir_path / project_name
    if not project_folder.exists():
        project_folder.mkdir(parents=True, exist_ok=True)
        msg = f'Project folder created: {project_folder}'
        try:
            from utils import signal_manager
            signal_manager.notify_user.emit(msg)
            logging.info(msg)
        except ImportError:
            logging.warning('signal_manager could not be imported to emit notification.')
    cache_dir = project_folder / '.cachecad'
    try:
        cache_dir.mkdir(exist_ok=True)
    except PermissionError as e:
        tb = traceback.format_exc()
        logging.error(f'Permission denied when creating cache directory: {cache_dir} \n {tb}')
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logging.error(f'Error creating cache directory {cache_dir}: {e} \n {tb}')
        signal_manager.error_occurred.emit('')
        raise
    hide_folder(cache_dir)
    return cache_dir

class CaseInsensitivePathDict:
    def __init__(self, init_dict=None):
        self._store = {}
        if init_dict:
            for k, v in init_dict.items():
                self[k] = v

    def _normalize_key(self, key):
        if isinstance(key, Path):
            key_str = key.as_posix()
        else:
            key_str = Path(key).as_posix()
        if sys.platform.startswith('win') or sys.platform == 'darwin':
            key_str = key_str.lower()
        return key_str

    def __setitem__(self, key, value):
        norm_key = self._normalize_key(key)
        self._store[norm_key] = (key, value)

    def __getitem__(self, key):
        norm_key = self._normalize_key(key)
        return self._store[norm_key][1]

    def __delitem__(self, key):
        norm_key = self._normalize_key(key)
        del self._store[norm_key]

    def __contains__(self, key):
        return self._normalize_key(key) in self._store

    def get(self, key, default=None):
        return self._store.get(self._normalize_key(key), (None, default))[1]

    def items(self):
        return ((orig, val) for orig, val in self._store.values())

    def keys(self):
        return (orig for orig, _ in self._store.values())

    def values(self):
        return (val for _, val in self._store.values())

    def __iter__(self):
        return self.keys()

    def __len__(self):
        return len(self._store)

    def __repr__(self):
        return f'{self.__class__.__name__}({dict(self.items())})'

def normalize_path(path):
    '\n    Normalize a path to a consistent format.\n    On Windows, convert to lowercase and use forward slashes.\n    '
    if isinstance(path, Path):
        path = path.as_posix()
    elif isinstance(path, str):
        path = Path(path).as_posix()
    return path

def hide_folder(path):
    '\n    Hide a folder at the given path.\n    On Windows, set the hidden attribute.\n    On macOS, prefix with a dot.\n    On Linux, prefix with a dot.\n    '
    p = Path(path)
    if p.exists() and p.is_dir():
        if sys.platform.startswith('win'):
            import ctypes
            FILE_ATTRIBUTE_HIDDEN = 2
            ret = ctypes.windll.kernel32.SetFileAttributesW(str(p), FILE_ATTRIBUTE_HIDDEN)
            if not ret:
                logging.error(f'Failed to hide folder: {p}')

def hide_file(path: str | Path) -> bool:
    p = Path(path)
    if not p.exists():
        logging.warning(f'File not found: {p}')
        return False
    try:
        if sys.platform.startswith('win'):
            import ctypes
            FILE_ATTRIBUTE_HIDDEN = 2
            ret = ctypes.windll.kernel32.SetFileAttributesW(str(p), FILE_ATTRIBUTE_HIDDEN)
            if not ret:
                logging.error(f'Failed to hide file: {p}')
                return False
        else:
            if not p.name.startswith('.'):
                hidden_path = p.parent / ('.' + p.name)
                p.rename(hidden_path)
                p = hidden_path
        return True
    except Exception as e:
        return False

def is_valid_windows_path(path: str) -> bool:
    if not path or not isinstance(path, str):
        return False
    if len(path) > 260:
        return False
    if re.match('^[a-zA-Z]:', path):
        drive, rest = path[0:2], path[2:]
    else:
        drive, rest = '', path
    invalid_chars = '<>:"/\\|?*'
    if any(ch in rest for ch in invalid_chars):
        return False
    for part in rest.split('\\'):
        if part.endswith(' ') or part.endswith('.'):
            return False
    return True
