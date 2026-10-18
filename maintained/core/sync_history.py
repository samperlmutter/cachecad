"""Validate the legacy CSV baseline before it can imply destructive sync actions."""
import csv
import io
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat

FIELDS = ('drive_id', 'name', 'path', 'md5', 'modified_time', 'size', 'timestamp', 'action')
KINDS = frozenset(('upload_new', 'download_new', 'upload_revision', 'download_revision',
                   'upload_unknown', 'download_unknown', 'delete_local', 'delete_cloud'))


class SyncHistoryError(RuntimeError):
    def __init__(self):
        super().__init__('Sync history is incomplete or damaged. No changes were planned. '
                         'Preserve .cachecad/sync_log.csv and review recovery records before resetting history.')


def _number(value, *, optional=False):
    if optional and value in ('', 'NA'):
        return
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise SyncHistoryError()


def read_sync_history(path):
    path = Path(path)
    try:
        for part in (path, *path.parents):
            if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
                raise SyncHistoryError()
        if not path.exists():
            return {}
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400
                or info.st_size > 64 * 1024 * 1024):
            raise SyncHistoryError()
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                raise SyncHistoryError()
            raw = stream.read(64 * 1024 * 1024 + 1)
            after = os.fstat(stream.fileno())
            current = path.lstat()
            if (len(raw) > 64 * 1024 * 1024
                    or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or (current.st_dev, current.st_ino, current.st_nlink)
                    != (opened.st_dev, opened.st_ino, 1)):
                raise SyncHistoryError()
        if not raw or not raw.endswith(b'\n'):
            raise SyncHistoryError()
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8'), newline=''), strict=True)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise SyncHistoryError()
        result = {}
        for row in reader:
            if set(row) != set(FIELDS) or any(value is None for value in row.values()):
                raise SyncHistoryError()
            relative = row['path']
            win, posix = PureWindowsPath(relative), PurePosixPath(relative)
            parts = relative.replace('\\', '/').split('/')
            if (not relative or win.drive or win.is_absolute() or posix.is_absolute()
                    or any(part in ('', '.', '..') for part in parts)
                    or parts[0].casefold() == '.cachecad' or row['name'] != parts[-1]
                    or any(ord(c) < 32 for c in relative)):
                raise SyncHistoryError()
            if row['action'] not in KINDS or not re.fullmatch('[0-9a-fA-F]{32}', row['md5']):
                raise SyncHistoryError()
            if not re.fullmatch('[0-9]+', row['size']):
                raise SyncHistoryError()
            _number(row['timestamp'])
            _number(row['modified_time'], optional=True)
            # Repeated paths are legitimate chronological history; last valid row wins.
            result[relative] = row
        return result
    except SyncHistoryError:
        raise
    except (OSError, UnicodeError, csv.Error, ValueError, TypeError):
        raise SyncHistoryError() from None


def baseline_entry(history, path, remote_id=None):
    """Only a successful transfer of the same identity establishes a baseline."""
    row = history.get(path)
    if (not row or row.get('action') not in KINDS - {'delete_local', 'delete_cloud'}
            or not row.get('drive_id')
            or (remote_id is not None and row.get('drive_id') != remote_id)):
        return None
    return row
