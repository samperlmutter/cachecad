"""Typed reconstruction of sync concepts; not original recovered source."""
from dataclasses import dataclass, field
from enum import Enum

class ActionKind(str, Enum):
    UPLOAD_NEW='upload_new'
    DOWNLOAD_NEW='download_new'
    UPLOAD_REVISION='upload_revision'
    DOWNLOAD_REVISION='download_revision'
    DELETE_LOCAL='delete_local'
    DELETE_CLOUD='delete_cloud'
    CONFLICT='conflict'

class ResultStatus(str, Enum):
    SUCCESS='success'
    FAILED='failed'
    CANCELLED='cancelled'
    CONFLICT='conflict'

@dataclass(frozen=True)
class FileSnapshot:
    path: str
    digest: str
    size: int
    generation: str

@dataclass(frozen=True)
class Scan:
    files: dict[str,FileSnapshot]
    complete: bool=True
    errors: tuple[str,...]=()

@dataclass(frozen=True)
class SyncAction:
    action_id: str
    path: str
    kind: ActionKind
    reason: str
    expected_local: FileSnapshot|None
    expected_remote: FileSnapshot|None

@dataclass(frozen=True)
class ActionResult:
    action_id: str
    status: ResultStatus
    detail: str=''

@dataclass
class ControllerState:
    phase: str='idle'
    actions: list[SyncAction]=field(default_factory=list)
    results: dict[str,ActionResult]=field(default_factory=dict)
    warnings: list[str]=field(default_factory=list)
    project_directory: str=''
