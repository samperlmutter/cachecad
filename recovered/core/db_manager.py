import sqlite3
from typing import Dict, List
from core.models import LocalFileInfo, DriveItemInfo
from core.signal_manager import signal_manager
import threading
import logging
from pathlib import Path
from utils.paths import CaseInsensitivePathDict, normalize_path

logger = logging.getLogger(__name__)

class DBManager:
    def __init__(self, db_path=':memory:'):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.local_table = 'local_files'
        self.drive_table = 'drive_files'
        self._create_tables()
        self.lock = threading.Lock()

    def reset(self):
        with self.lock:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(f'DELETE FROM {self.local_table}')
                cursor.execute(f'DELETE FROM {self.drive_table}')

    def _create_tables(self):
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute('DROP TABLE IF EXISTS local_files')
            cursor.execute('DROP TABLE IF EXISTS drive_files')
            cursor.execute('DROP TABLE IF EXISTS logged_actions')
            cursor.execute('''
                CREATE TABLE local_files (
                    path TEXT PRIMARY KEY,
                    full_path TEXT,
                    size INTEGER,
                    modified_time REAL,
                    md5_checksum TEXT,
                    is_folder INTEGER
                )
            ''')
            cursor.execute('''
                CREATE TABLE drive_files (
                    path TEXT,
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    parent TEXT,
                    md5_checksum TEXT,
                    version TEXT,
                    size INTEGER,
                    modified_time REAL,
                    drive_id TEXT,
                    modified_by TEXT,
                    is_folder INTEGER
                )
            ''')

    def write_local_items(self, local_files: List[LocalFileInfo]):
        if local_files is not None:
            with self.lock:
                rows = [
                    (
                        info.path,
                        info.full_path,
                        info.size,
                        info.modified_time,
                        info.md5_checksum,
                        int(info.is_folder)
                    )
                    for info in local_files
                ]
                with self.conn:
                    self.conn.executemany(f'''
                        INSERT INTO {self.local_table} (path, full_path, size, modified_time, md5_checksum, is_folder)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', rows)

    def write_drive_items(self, drive_files: List[DriveItemInfo]):
        if drive_files is not None:
            with self.lock:
                rows = [
                    (
                        info.path,
                        info.id,
                        info.name,
                        info.parent,
                        info.md5_checksum,
                        info.version,
                        info.size,
                        info.modified_time,
                        info.drive_id,
                        info.modified_by,
                        int(info.is_folder)
                    )
                    for info in drive_files
                ]
                folder_count = sum(1 for info in drive_files if info.is_folder)
                logging.debug(f'Writing {len(drive_files)} drive items, including {folder_count} folders')
                with self.conn:
                    self.conn.executemany(f'''
                        INSERT INTO {self.drive_table} (
                            path, id, name, parent, md5_checksum, version,
                            size, modified_time, drive_id, modified_by, is_folder
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', rows)

    def compare_local_and_drive(self) -> tuple[
        List[LocalFileInfo],
        List[DriveItemInfo],
        List[tuple[DriveItemInfo, LocalFileInfo]]
    ]:
        with self.lock:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(f'''
                    SELECT * FROM {self.local_table} WHERE path IN (
                    SELECT path FROM {self.local_table} WHERE is_folder = 0
                    EXCEPT SELECT path FROM {self.drive_table} WHERE is_folder = 0
                    );
                ''')
                local_not_drive_rows = cursor.fetchall()
                local_not_drive = [LocalFileInfo.from_db_row(row) for row in local_not_drive_rows]
                cursor.execute(f'''
                    SELECT * FROM {self.drive_table} WHERE path IN (
                    SELECT path FROM {self.drive_table} WHERE is_folder = 0
                    EXCEPT SELECT path FROM {self.local_table} WHERE is_folder = 0
                    );
                ''')
                drive_not_local_rows = cursor.fetchall()
                drive_not_local = [DriveItemInfo.from_db_row(row) for row in drive_not_local_rows]
                cursor.execute('''
                    SELECT
                        drive.path AS drive_path,
                        drive.id AS drive_id,
                        drive.name AS drive_name,
                        drive.parent AS drive_parent,
                        drive.md5_checksum AS drive_md5,
                        drive.version AS drive_version,
                        drive.size AS drive_size,
                        drive.modified_time AS drive_mtime,
                        drive.drive_id AS drive_drive_id,
                        drive.modified_by AS drive_modified_by,
                        drive.is_folder AS drive_folder,

                        local.path AS local_path,
                        local.full_path AS local_full_path,
                        local.size AS local_size,
                        local.modified_time AS local_mtime,
                        local.md5_checksum AS local_md5,
                        local.is_folder AS local_folder
                    FROM drive_files AS drive
                    JOIN local_files AS local ON drive.path = local.path
                    WHERE drive.md5_checksum <> local.md5_checksum
                ''')
                mismatch_rows = cursor.fetchall()
                cursor.execute(f'''
                    SELECT path, id FROM {self.drive_table} WHERE is_folder = 1;
                ''')
                drive_folders = cursor.fetchall()
                folder_dict = CaseInsensitivePathDict({
                    normalize_path(row['path']): row['id']
                    for row in drive_folders
                })
                drive_folders_set = set(folder_dict.keys())
                cursor.execute(f'''
                    SELECT path FROM {self.local_table} WHERE is_folder = 1;
                ''')
                local_folders = cursor.fetchall()
                local_folders_set = {
                    Path(row['path']).as_posix()
                    for row in local_folders
                }
                needs_drive_folder = {
                    p for p in local_folders_set
                    if p.lower() not in {k.lower() for k in drive_folders_set}
                }
                logging.debug('Folders needing drive folder: %d', len(needs_drive_folder))
                for path in needs_drive_folder:
                    logging.debug('   - %s', path)
                for path in needs_drive_folder:
                    folder_dict[normalize_path(path)] = None
        md5_mismatch = []
        for row in mismatch_rows:
            drive_item = DriveItemInfo.from_db_row({
                'path': row['drive_path'],
                'id': row['drive_id'],
                'name': row['drive_name'],
                'parent': row['drive_parent'],
                'md5_checksum': row['drive_md5'],
                'version': row['drive_version'],
                'size': row['drive_size'],
                'modified_time': row['drive_mtime'],
                'drive_id': row['drive_drive_id'],
                'modified_by': row['drive_modified_by'],
                'is_folder': row['drive_folder']
            })
            local_item = LocalFileInfo.from_db_row({
                'path': row['local_path'],
                'full_path': row['local_full_path'],
                'size': row['local_size'],
                'modified_time': row['local_mtime'],
                'md5_checksum': row['local_md5'],
                'is_folder': row['local_folder']
            })
            md5_mismatch.append((drive_item, local_item))
        return local_not_drive, drive_not_local, md5_mismatch, folder_dict

    def reset(self):
        self._create_tables()
