from dataclasses import dataclass, field
from pathlib import Path
from enum import Enum
from typing import Optional, Union

from utils.file_size import format_file_size
from utils.timestamps import timestamp_to_string, drive_time_to_unix, current_unix_timestamp

@dataclass
class LocalFileInfo:
    path: str
    full_path: str
    size: int
    modified_time: float
    md5_checksum: Optional[str] = None
    is_folder: bool = False

    @classmethod
    def from_path(cls, path: Path, root: Path, md5_checksum: Optional[str] = None) -> 'LocalFileInfo':
        """
        Creates a LocalFileInfo from a Path object, relative to the root directory.
        If the file is a folder, md5_checksum should be left as None.
        """
        stat = path.stat()
        rel_path = str(path.relative_to(root))
        return cls(
            path=rel_path,
            full_path=str(path.resolve()),
            size=0 if path.is_dir() else stat.st_size,
            modified_time=stat.st_mtime,
            md5_checksum=md5_checksum,
            is_folder=path.is_dir(),
        )

    @classmethod
    def from_db_row(cls, row: dict) -> 'LocalFileInfo':
        return cls(
            path=row['path'],
            full_path=row['full_path'],
            size=row['size'],
            modified_time=row['modified_time'],
            md5_checksum=row['md5_checksum'],
            is_folder=bool(row['is_folder']),
        )

@dataclass
class DriveItemInfo:
    path: str
    id: str
    name: str
    parent: str
    md5_checksum: Optional[str]
    version: Optional[str]
    size: int
    modified_time: float
    drive_id: Optional[str]
    modified_by: Optional[str]
    is_folder: bool

    @classmethod
    def from_drive_item(cls, item: dict, full_path: str):
        user = item.get('lastModifyingUser', {})
        modified_by = user.get('emailAddress') or user.get('displayName') or 'unknown'
        return cls(
            path=full_path,
            id=item['id'],
            name=item['name'],
            parent=item['parents'][0],
            md5_checksum=item.get('md5Checksum'),
            version=item.get('version'),
            is_folder=item.get('capabilities', {}).get('canAddChildren', False),
            modified_time=drive_time_to_unix(item['modifiedTime']),
            size=int(item.get('size', 0)),
            drive_id=item.get('driveId'),
            modified_by=modified_by,
        )

    @classmethod
    def from_db_row(cls, row: dict) -> 'DriveItemInfo':
        return cls(
            path=row['path'],
            id=row['id'],
            name=row['name'],
            parent=row['parent'],
            md5_checksum=row['md5_checksum'],
            version=row['version'],
            size=row['size'],
            modified_time=row['modified_time'],
            drive_id=row['drive_id'],
            modified_by=row['modified_by'],
            is_folder=bool(row['is_folder']),
        )

class SyncActionType(Enum):
    UPLOAD_NEW = 'upload_new'
    DOWNLOAD_NEW = 'download_new'
    UPLOAD_REVISION = 'upload_revision'
    DOWNLOAD_REVISION = 'download_revision'
    DELETE_LOCAL = 'delete_local'
    DELETE_CLOUD = 'delete_cloud'
    UPLOAD_UNKNOWN = 'upload_unknown'
    DOWNLOAD_UNKNOWN = 'download_unknown'

@dataclass
class SyncAction:
    path: str
    action: SyncActionType
    reason: str
    size: int
    md5_checksum: str
    parent_id: str
    modified_by: str
    modified_time: str
    name: str = field(init=False)
    folder: str = field(init=False)
    parent_name: str = field(init=False)
    drive_id: str = field(default=None)

    def __post_init__(self):
        p = Path(self.path)
        self.name = p.name
        self.folder = None if p.parent == Path('.') else str(p.parent)
        self.parent_name = None if p.parent == Path('.') else str(p.parent.name)

    def get_treeview_data(self) -> list[str]:
        return [
            self.name,
            self.folder,
            format_file_size(self.size),
            self.modified_by,
            timestamp_to_string(self.modified_time) if self.modified_time not in ('', 'NA') else self.modified_time,
        ]

    def get_log_tuple(self) -> tuple:
        return (
            self.drive_id,
            self.name,
            self.path,
            self.md5_checksum,
            self.modified_time,
            self.size,
            current_unix_timestamp(),
            str(self.action.value),
        )
