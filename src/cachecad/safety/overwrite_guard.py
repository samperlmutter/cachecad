"""Independent, fail-closed stale-plan policy prototype; not a CacheCAD patch.

Adapters must capture stable snapshots (including a content digest and identity /
generation), resolve paths inside the project, reject aliases and symlinks, and
provide a COMPLETE authoritative remote listing. Unknown is never absence.
Run this check before staging and again immediately before commit. Passing it is
NOT an atomic commit authorization: adapters still need a conditional remote
write or exclusive ownership protocol, local race protection, recoverable
backups, and a per-project/path lock. No API capability is assumed here.
"""

from dataclasses import dataclass
from enum import Enum


class Operation(str, Enum):
    UPLOAD_NEW = "upload_new"
    UPLOAD_REVISION = "upload_revision"
    DOWNLOAD_NEW = "download_new"
    DOWNLOAD_REVISION = "download_revision"
    DELETE_LOCAL = "delete_local"
    DELETE_REMOTE = "delete_remote"


class GuardConflict(ValueError):
    """Stop this action and reanalyze; do not auto-select an overwrite winner."""


@dataclass(frozen=True)
class Snapshot:
    exists: bool
    object_id: str | None = None
    generation: str | None = None
    digest: str | None = None
    size: int | None = None

    def validate(self) -> None:
        if type(self.exists) is not bool:
            raise GuardConflict("Existence must be known explicitly")
        if not self.exists:
            if any(v is not None for v in
                   (self.object_id, self.generation, self.digest, self.size)):
                raise GuardConflict("Absent snapshots must not carry old metadata")
            return
        if not all(isinstance(v, str) and v.strip() for v in
                   (self.object_id, self.generation, self.digest)):
            raise GuardConflict("Existing file lacks identity, generation or digest")
        if type(self.size) is not int or self.size < 0:
            raise GuardConflict("Existing file lacks a valid size")


@dataclass(frozen=True)
class Plan:
    operation: Operation
    local: Snapshot
    remote: Snapshot
    scan_complete: bool
    unresolved_conflict: bool = False


def check_preconditions(plan: Plan, *, local_now: Snapshot,
                        remote_now: Snapshot, scan_complete_now: bool,
                        cancelled: bool = False) -> None:
    """Raise GuardConflict on stale/incomplete/ambiguous state; never mutate data.

Snapshot metadata must use the same algorithm/encoding at plan and commit time.
For a local file, generation should include nanosecond mtime/ctime and identity;
for a remote file use its authoritative change/version token. Capturing these
fields alone does not make reading a changing file a stable snapshot.
"""
    if cancelled:
        raise GuardConflict("Action cancelled")
    if not isinstance(plan.operation, Operation):
        raise GuardConflict("Unsupported or unresolved action")
    if plan.scan_complete is not True or scan_complete_now is not True:
        raise GuardConflict("Incomplete scan: absence and safe writes are unproven")
    if plan.unresolved_conflict:
        raise GuardConflict("Resolve both-changed conflict and build a fresh plan")
    for snapshot in (plan.local, plan.remote, local_now, remote_now):
        snapshot.validate()
    if local_now != plan.local:
        raise GuardConflict("Local file changed after analysis")
    if remote_now != plan.remote:
        raise GuardConflict("Remote file changed after analysis")

    required = {
        Operation.UPLOAD_NEW: (True, False),
        Operation.UPLOAD_REVISION: (True, True),
        Operation.DOWNLOAD_NEW: (False, True),
        Operation.DOWNLOAD_REVISION: (True, True),
        Operation.DELETE_LOCAL: (True, False),
        Operation.DELETE_REMOTE: (False, True),
    }[plan.operation]
    if (local_now.exists, remote_now.exists) != required:
        raise GuardConflict("Action does not match current source/target existence")
