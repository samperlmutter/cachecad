"""Independent sync safety prototype. No CacheCAD code is imported or executed."""
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Callable

@dataclass(frozen=True)
class RemoteSnapshot:
    paths: frozenset[str]
    complete: bool
    errors: tuple[str, ...] = ()

class IncompleteScan(ValueError):
    pass

def deletion_candidates(previously_synced: set[str], unchanged_local: set[str],
                        remote: RemoteSnapshot) -> frozenset[str]:
    """Refuse destructive planning unless the caller certifies full scan success."""
    if not remote.complete or remote.errors:
        raise IncompleteScan('Remote listing is incomplete; deletion planning is blocked')
    return frozenset((previously_synced & unchanged_local) - remote.paths)

class Status(Enum):
    SUCCESS = 'success'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    CONFLICT = 'conflict'

@dataclass(frozen=True)
class Result:
    action_id: str
    status: Status
    detail: str = ''

class Cancelled(Exception):
    pass

class Ledger:
    """Each unique action can be claimed once and receives one terminal result."""
    def __init__(self, action_ids):
        ids = list(action_ids)
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate action IDs')
        self.expected = frozenset(ids)
        self._claimed = set()
        self._results = {}
        self._lock = Lock()

    def run(self, action_id: str, operation: Callable[[], Status]) -> Result:
        with self._lock:
            if action_id not in self.expected:
                raise KeyError(action_id)
            if action_id in self._claimed:
                raise ValueError('Action already claimed')
            self._claimed.add(action_id)
        try:
            status = operation()
            if not isinstance(status, Status):
                raise TypeError('Operation must return a terminal Status')
            result = Result(action_id, status)
        except Cancelled as exc:
            result = Result(action_id, Status.CANCELLED, str(exc))
        except Exception as exc:
            result = Result(action_id, Status.FAILED, f'{type(exc).__name__}: {exc}')
        with self._lock:
            self._results[action_id] = result
        return result

    @property
    def results(self):
        with self._lock:
            return dict(self._results)

    @property
    def complete(self):
        with self._lock:
            return self.expected == self._results.keys()

def delete_cloud(delete: Callable[[], bool], record_success: Callable[[], None]) -> Status:
    """A failed provider call must never produce a success journal entry."""
    if delete() is not True:
        return Status.FAILED
    record_success()
    return Status.SUCCESS

def download_new(destination_exists: Callable[[], bool], transfer: Callable[[], None]) -> Status:
    """Preflight guard only: atomic no-clobber commit is required in integration."""
    if destination_exists():
        return Status.CONFLICT
    transfer()
    return Status.SUCCESS

class ScanCollector:
    """Track every required batch/page; discovering a page adds another obligation.

    Caller must register child page IDs before completing their parent page.
    Snapshot completeness is derived, not inferred from files accumulated so far.
    """
    def __init__(self, initial_pages):
        self._required = set(initial_pages)
        if not self._required:
            raise ValueError('At least one root listing obligation is required')
        self._finished = set()
        self._paths = set()
        self._errors = []
        self._lock = Lock()

    def page_succeeded(self, page_id, paths, next_pages=()):
        with self._lock:
            if page_id not in self._required or page_id in self._finished:
                raise ValueError('Unknown or already completed page')
            self._required.update(next_pages)
            self._paths.update(paths)
            self._finished.add(page_id)

    def page_failed(self, page_id, error):
        with self._lock:
            if page_id not in self._required or page_id in self._finished:
                raise ValueError('Unknown or already completed page')
            self._errors.append(f'{page_id}: {error}')
            self._finished.add(page_id)

    def snapshot(self):
        with self._lock:
            return RemoteSnapshot(frozenset(self._paths),
                                  not self._errors and self._required == self._finished,
                                  tuple(self._errors))
