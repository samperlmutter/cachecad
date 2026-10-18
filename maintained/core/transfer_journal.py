"""Durable, fail-closed evidence for real transfers, with no automatic replay.

Every external mutation must be preceded by a durable ``unknown`` outcome.
Only the caller can establish that a mutation committed. A completed operation
also requires its ordinary sync log to have been written. Recovery is read-only:
this module never removes recovery material or automatically repeats an action.

Files are fsynced and JSON updates are atomic. POSIX directory fsync failures
are fatal. Python's standard library cannot flush Windows directory handles;
Windows therefore has file-fsync/atomic-replace protection, not a promise of
power-loss durability of directory entries. This limitation is in every intent.
Checks reject symlinks, junctions and other Windows reparse points, including
ancestors. They are observed-path checks, not a defense against a privileged
concurrent process swapping directories between a check and a filesystem call.
"""
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import threading
import uuid
from core.file_identity import file_identity, handle_identity
import weakref


class JournalError(RuntimeError):
    """Evidence could not be made durable or safely validated; do not mutate."""


class PendingTransferRecovery(JournalError):
    """A previous operation on this destination needs explicit reconciliation."""


class PublicationCommittedError(JournalError):
    """The no-clobber link succeeded, but its directory flush failed."""
    committed = True


class TransferOutcome(str, Enum):
    NOT_STARTED = 'not_started'
    UNKNOWN = 'unknown'
    COMMITTED = 'committed'
    COMMITTED_RECORDING_FAILED = 'committed_recording_failed'


_OUTCOMES = frozenset(item.value for item in TransferOutcome)
_ACTIONS = frozenset(('upload_new', 'download_new', 'upload_revision',
                     'download_revision', 'upload_unknown', 'download_unknown',
                     'delete_local', 'delete_cloud', 'create_folder'))
_OPERATION_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}\Z')
_LABEL = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_MATERIAL_NAMES = {'backup': 'backup.bin', 'incoming': 'incoming.bin',
                   'publication': 'publication.bin'}
_MAX_JSON = 4 * 1024 * 1024
_PROCESS_LOCKS = weakref.WeakValueDictionary()
_PROCESS_LOCKS_GUARD = threading.Lock()


def _process_lock(path):
    with _PROCESS_LOCKS_GUARD:
        key = os.path.normcase(str(path))
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    # A private deep copy also rejects NaN, unsupported objects and mutable aliases.
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def _encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'),
                       ensure_ascii=True, allow_nan=False) + '\n').encode('utf-8')


def _identity(info):
    return file_identity(info)


def _reject_links(path, *, missing=False):
    path = Path(path).absolute()
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise JournalError('Recovery path is unavailable: ' + str(component)) from None
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400
                or (hasattr(component, 'is_junction') and component.is_junction())):
            raise JournalError('Recovery paths cannot contain links or reparse points')
        if component != path and not stat.S_ISDIR(info.st_mode):
            raise JournalError('Recovery path ancestor is not a directory')
    return path


def _regular(path):
    path = _reject_links(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not info.st_ino:
        raise JournalError('Recovery material is not an identifiable regular file')
    return info


def fsync_directory(path):
    """Flush a directory on POSIX; return False on Windows (documented limit)."""
    path = _reject_links(path)
    if not path.is_dir():
        raise JournalError('Directory flush target is not a directory')
    if os.name == 'nt':
        return False
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
                 | getattr(os, 'O_NOFOLLOW', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def _mkdir(path):
    _reject_links(path, missing=True)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        _reject_links(path)
        if not path.is_dir():
            raise JournalError('Recovery storage is not a directory') from None
    else:
        fsync_directory(path.parent)


def _read_json(path):
    before = _regular(path)
    if before.st_size > _MAX_JSON:
        raise JournalError('Recovery record is too large')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(_MAX_JSON + 1)
        after = os.fstat(stream.fileno())
    if (_identity(before) != _identity(opened)
            or handle_identity(opened) != handle_identity(after)
            or _identity(before) != _identity(after)
            or _identity(before) != _identity(_regular(path))):
        raise JournalError('Recovery record changed while being read')
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs,
                           parse_constant=lambda value: _invalid_constant(value))
    except (UnicodeError, ValueError, TypeError) as error:
        raise JournalError('Recovery record is not valid JSON') from error
    if not isinstance(value, dict):
        raise JournalError('Recovery record is not a JSON object')
    return value


def _invalid_constant(value):
    raise ValueError('Non-finite JSON number: ' + value)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _atomic_json(path, value, *, exclusive=False):
    _reject_links(path, missing=True)
    raw = _encoded(value)
    if len(raw) > _MAX_JSON:
        raise JournalError('Recovery record would exceed the size limit')
    temporary = path.parent / ('.write-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    _reject_links(path, missing=True)
    if exclusive:
        os.link(temporary, path, follow_symlinks=False)
    else:
        if os.path.lexists(path):
            _regular(path)
        os.replace(temporary, path)
    fsync_directory(path.parent)
    if exclusive:
        temporary.unlink()
        fsync_directory(path.parent)
    # A failed write deliberately retains its private temporary evidence.


def _hash(path):
    before = _regular(path)
    digest = hashlib.md5()
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (_identity(before) != _identity(opened)
            or handle_identity(opened) != handle_identity(after)
            or _identity(before) != _identity(after)
            or _identity(before) != _identity(_regular(path))):
        raise JournalError('Recovery source changed while being verified')
    return digest.hexdigest(), before.st_size, _identity(before)


def _checksum(md5, size):
    if (not isinstance(md5, str) or re.fullmatch(r'[a-fA-F0-9]{32}', md5) is None
            or type(size) is not int or size < 0):
        raise JournalError('Recovery material requires an approved MD5 and size')
    return md5.lower(), size


def _copy_verified(source, target, md5, size, identity=None, *, readonly=True):
    md5, size = _checksum(md5, size)
    before = _regular(source)
    if identity is not None and tuple(identity) != _identity(before):
        raise JournalError('Backup source no longer has its approved identity')
    _reject_links(target, missing=True)
    source_fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(source_fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                            | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(target_fd, 'wb') as copy:
            digest = hashlib.md5()
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
                copy.write(chunk)
            copy.flush()
            if readonly:
                # Independent evidence is read-only as an accident guard, not a
                # security boundary against the owner or privileged processes.
                target.chmod(stat.S_IRUSR)
            os.fsync(copy.fileno())
        after = os.fstat(stream.fileno())
    if (_identity(before) != _identity(opened)
            or handle_identity(opened) != handle_identity(after)
            or _identity(before) != _identity(after)
            or _identity(before) != _identity(_regular(source))
            or digest.hexdigest() != md5 or before.st_size != size):
        raise JournalError('Copied source no longer matches its approved version')
    actual_md5, actual_size, _ = _hash(target)
    if (actual_md5, actual_size) != (md5, size):
        raise JournalError('Recovery copy did not preserve approved bytes')
    fsync_directory(target.parent)
    return {'name': target.name, 'md5': md5, 'size': size}


def _relative(value):
    if not isinstance(value, (str, Path)):
        raise JournalError('Transfer path must be a relative project path')
    text = str(value)
    windows = PureWindowsPath(text)
    path = Path(text)
    components = text.replace('\\', '/').split('/')
    # Backslashes are path separators on Windows even when a journal is inspected
    # on another platform. Reject ambiguous/alternate-stream names everywhere.
    if (not text or path.is_absolute() or windows.root or windows.drive or windows.is_reserved()
            or '\\' in text and os.name != 'nt' or ':' in text
            or any(ord(character) < 32 for character in text)
            or any(part in ('', '.', '..') or part.endswith((' ', '.')) for part in components)
            or path.parts[0].casefold() == '.cachecad'):
        raise JournalError('Transfer path leaves the project or names reserved recovery storage')
    return path.as_posix()


def _version(value, *, remote=False):
    if value is None:
        return None
    value = _json(value)
    if not isinstance(value, dict):
        raise JournalError('Approved version must be a record')
    if remote:
        required = ('file_id', 'name', 'parents', 'version', 'md5', 'size')
        if any(key not in value for key in required):
            raise JournalError('Approved remote version is incomplete')
        if (not isinstance(value['file_id'], str) or not value['file_id']
                or not isinstance(value['version'], str) or not value['version'].isdigit()
                or not isinstance(value['parents'], list)
                or any(not isinstance(item, str) or not item for item in value['parents'])):
            raise JournalError('Approved remote identity or version is invalid')
        _checksum(value['md5'], value['size'])
    else:
        if type(value.get('exists')) is not bool:
            raise JournalError('Approved local version must specify existence')
        if value['exists']:
            _checksum(value.get('md5'), value.get('size'))
            identity = value.get('identity')
            if (not isinstance(identity, list) or len(identity) != 5
                    or any(type(item) is not int for item in identity) or not identity[1]):
                raise JournalError('Approved local version has no stable identity')
    return value


def _blocked(record):
    return (record['state']['outcome'] != TransferOutcome.NOT_STARTED.value
            and not (record['state']['outcome'] == TransferOutcome.COMMITTED.value
                     and record['state']['phase'] == 'completed'))


class TransferJournal:
    def __init__(self, project_root, project_id, *, create=True):
        if not isinstance(project_id, str) or not project_id:
            raise JournalError('Recovery journal requires a project ID')
        self.project_root = _reject_links(project_root).resolve(strict=True)
        _reject_links(self.project_root)
        info = self.project_root.stat()
        if not stat.S_ISDIR(info.st_mode) or not info.st_ino:
            raise JournalError('Project is not an identifiable directory')
        self.project_identity = (info.st_dev, info.st_ino)
        self.project_id = project_id
        self.directory = self.project_root / '.cachecad' / 'recovery'
        self._lock = _process_lock(self.directory)
        if create:
            _mkdir(self.directory.parent)
            _mkdir(self.directory)
        else:
            _reject_links(self.directory, missing=True)
            if self.directory.exists() and not self.directory.is_dir():
                raise JournalError('Recovery storage is not a directory')

    def _validate_root(self):
        _reject_links(self.project_root)
        info = self.project_root.stat()
        if (info.st_dev, info.st_ino) != self.project_identity:
            raise JournalError('Project identity changed')
        _reject_links(self.directory)
        if not self.directory.is_dir():
            raise JournalError('Recovery storage is not a directory')

    @contextmanager
    def _exclusive(self):
        with self._lock:
            self._validate_root()
            path = self.directory / '.journal.lock'
            _reject_links(path, missing=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            locked = False
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or _identity(info) != _identity(_regular(path))):
                    raise JournalError('Recovery lock identity changed')
                if os.name == 'nt':
                    import msvcrt
                    if info.st_size == 0:
                        os.write(fd, b'\0')
                        os.fsync(fd)
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                yield
            finally:
                if locked:
                    if os.name == 'nt':
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def prepare_intent(self, *, action_kind, path, approved_local=None,
                       approved_remote=None, expected_payload=None, operation_id=None):
        action_kind = getattr(action_kind, 'value', action_kind)
        if action_kind not in _ACTIONS:
            raise JournalError('Unsupported transfer action')
        path = _relative(path)
        operation_id = uuid.uuid4().hex if operation_id is None else operation_id
        if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
            raise JournalError('Invalid recovery operation ID')
        local = _version(approved_local)
        remote = _version(approved_remote, remote=True)
        payload = _json(expected_payload)
        if payload is not None:
            if (action_kind not in ('upload_new', 'download_new')
                    or not isinstance(payload, dict) or set(payload) != {'md5', 'size'}):
                raise JournalError('Selected payload fallback is only allowed for new files')
            _checksum(payload.get('md5'), payload.get('size'))
        if action_kind in ('download_revision', 'download_unknown', 'upload_revision', 'upload_unknown') and (
                local is None or not local['exists'] or remote is None):
            raise JournalError('Revision recovery requires both approved file versions')
        if action_kind == 'delete_local' and (local is None or not local['exists']):
            raise JournalError('Local deletion recovery requires its approved version')
        if action_kind == 'delete_cloud' and remote is None:
            raise JournalError('Cloud deletion recovery requires its approved version')
        if action_kind == 'upload_new' and payload is None and (local is None or not local['exists']):
            raise JournalError('A new upload requires its approved payload')
        if action_kind == 'download_new' and payload is None and remote is None:
            raise JournalError('A new download requires its approved payload')
        intent = {'schema_version': 1, 'operation_id': operation_id,
                  'project_root': str(self.project_root),
                  'project_identity': list(self.project_identity), 'project_id': self.project_id,
                  'action_kind': action_kind, 'path': path,
                  'approved_local': local, 'approved_remote': remote,
                  'expected_payload': payload, 'created_at': _now(),
                  'durability': ('file_fsync_atomic_replace; directory_fsync_unavailable_on_windows'
                                 if os.name == 'nt' else 'file_fsync_atomic_replace_directory_fsync')}
        with self._exclusive():
            self._check_pending_path(path)
            directory = self.directory / operation_id
            _reject_links(directory, missing=True)
            directory.mkdir(mode=0o700)  # Never reuse or overwrite an operation ID.
            fsync_directory(self.directory)
            _atomic_json(directory / 'intent.json', intent, exclusive=True)
            state = {'schema_version': 1, 'operation_id': operation_id,
                     'intent_sha256': hashlib.sha256(_encoded(intent)).hexdigest(),
                     'sequence': 0, 'phase': 'intent', 'outcome': 'not_started',
                     'details': {}, 'materials': {}, 'history': []}
            _atomic_json(directory / 'state.json', state, exclusive=True)
        return TransferOperation(self, intent)

    def _check_pending_path(self, path, exclude=None):
        for record in self.list_pending():
            if (record['intent']['operation_id'] != exclude
                    and record['intent']['path'].casefold() == path.casefold() and _blocked(record)):
                raise PendingTransferRecovery('An unfinished transfer needs recovery review: ' + path)

    def read(self, operation_id):
        if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
            raise JournalError('Invalid recovery operation ID')
        self._validate_root()
        directory = _reject_links(self.directory / operation_id)
        intent = _read_json(directory / 'intent.json')
        state = _read_json(directory / 'state.json')
        if (intent.get('schema_version') != 1 or intent.get('operation_id') != operation_id
                or intent.get('project_root') != str(self.project_root)
                or intent.get('project_identity') != list(self.project_identity)
                or intent.get('project_id') != self.project_id
                or intent.get('action_kind') not in _ACTIONS
                or intent.get('path') != _relative(intent.get('path'))):
            raise JournalError('Recovery intent does not match this project or operation')
        _version(intent.get('approved_local'))
        _version(intent.get('approved_remote'), remote=True)
        payload = intent.get('expected_payload')
        if payload is not None:
            if (intent['action_kind'] not in ('upload_new', 'download_new')
                    or not isinstance(payload, dict) or set(payload) != {'md5', 'size'}):
                raise JournalError('Recovery payload fallback is invalid')
            _checksum(payload.get('md5'), payload.get('size'))
        if (state.get('schema_version') != 1 or state.get('operation_id') != operation_id
                or state.get('intent_sha256') != hashlib.sha256(_encoded(intent)).hexdigest()
                or type(state.get('sequence')) is not int or state['sequence'] < 0
                or not isinstance(state.get('phase'), str) or not _LABEL.fullmatch(state['phase'])
                or state.get('outcome') not in _OUTCOMES
                or not isinstance(state.get('details'), dict)
                or not isinstance(state.get('history'), list)
                or state['sequence'] != len(state['history'])
                or not isinstance(state.get('materials'), dict)):
            raise JournalError('Recovery state is invalid or is bound to another intent')
        for sequence, event in enumerate(state['history'], 1):
            if (not isinstance(event, dict) or event.get('sequence') != sequence
                    or event.get('outcome') not in _OUTCOMES
                    or not isinstance(event.get('phase'), str)
                    or not _LABEL.fullmatch(event['phase'])
                    or not isinstance(event.get('details'), dict)
                    or not isinstance(event.get('at'), str)):
                raise JournalError('Recovery history is invalid')
        if state['history']:
            latest = state['history'][-1]
            if any(latest[key] != state[key] for key in ('phase', 'outcome', 'details')):
                raise JournalError('Recovery state contradicts its history')
        elif state['phase'] != 'intent' or state['outcome'] != 'not_started':
            raise JournalError('Recovery state has no supporting history')
        if (state['phase'] in ('mutation_started', 'published', 'completed')
                and state['outcome'] == 'not_started'):
            raise JournalError('Recovery outcome contradicts its mutation phase')
        if state['phase'] == 'completed' and state['outcome'] != 'committed':
            raise JournalError('Recovery completion has no confirmed commit')
        for kind, material in state['materials'].items():
            if (kind not in _MATERIAL_NAMES or not isinstance(material, dict)
                    or material.get('name') != _MATERIAL_NAMES[kind]):
                raise JournalError('Recovery material path is invalid')
            _checksum(material.get('md5'), material.get('size'))
            _regular(directory / material['name'])
        return {'intent': intent, 'state': state}

    def list_pending(self):
        _reject_links(self.directory, missing=True)
        if not self.directory.exists():
            return ()
        self._validate_root()
        pending = []
        for entry in sorted(self.directory.iterdir()):
            if entry.name == '.journal.lock':
                _regular(entry)
                continue
            if not entry.is_dir() or not _OPERATION_ID.fullmatch(entry.name):
                raise JournalError('Unrecognized recovery storage entry')
            record = self.read(entry.name)
            if not (record['state']['outcome'] == 'committed' and record['state']['phase'] == 'completed'):
                pending.append(record)
        return tuple(pending)

    def open_operation(self, operation_id):
        return TransferOperation(self, self.read(operation_id)['intent'])


class TransferOperation:
    def __init__(self, journal, intent):
        self.journal = journal
        self._intent = _json(intent)
        self.operation_id = intent['operation_id']
        self.directory = journal.directory / self.operation_id
        self.backup_path = self.directory / 'backup.bin'
        self.incoming_path = self.directory / 'incoming.bin'
        self.publication_path = self.directory / 'publication.bin'

    @property
    def intent(self):
        return _json(self._intent)

    def _read(self):
        record = self.journal.read(self.operation_id)
        if record['intent'] != self._intent:
            raise JournalError('An immutable transfer intent changed')
        return record

    def _record(self, phase=None, outcome=None, details=None, materials=None):
        with self.journal._exclusive():
            state = self._read()['state']
            next_phase = state['phase'] if phase is None else phase
            next_outcome = state['outcome'] if outcome is None else getattr(outcome, 'value', outcome)
            if (not isinstance(next_phase, str) or not _LABEL.fullmatch(next_phase)
                    or next_outcome not in _OUTCOMES):
                raise JournalError('Invalid transfer phase or outcome')
            if state['outcome'] in ('committed', 'committed_recording_failed') and next_outcome not in (
                    'committed', 'committed_recording_failed'):
                raise JournalError('A committed outcome cannot be downgraded')
            if state['outcome'] == 'unknown' and next_outcome == 'not_started':
                raise JournalError('An uncertain mutation cannot be silently retried')
            if next_phase in ('mutation_started', 'published', 'completed') and next_outcome == 'not_started':
                raise JournalError('Mutation phases require a durable uncertain or committed outcome')
            if next_outcome != 'not_started':
                self.journal._check_pending_path(self._intent['path'], exclude=self.operation_id)
            if next_phase == 'completed' and next_outcome != 'committed':
                raise JournalError('Only a confirmed, recorded transfer may complete')
            state['sequence'] += 1
            state['phase'] = next_phase
            state['outcome'] = next_outcome
            state['details'] = _json(details or {})
            if materials is not None:
                state['materials'].update(_json(materials))
            state['history'].append({'sequence': state['sequence'], 'at': _now(),
                                     'phase': next_phase, 'outcome': next_outcome,
                                     'details': state['details']})
            _atomic_json(self.directory / 'state.json', state)
            return _json(state)

    def record_phase(self, phase, **details):
        return self._record(phase=phase, details=details)

    def record_outcome(self, outcome, *, phase=None, **details):
        return self._record(phase=phase, outcome=outcome, details=details)

    def _approved_material(self, kind, md5, size):
        local = kind == 'backup' or self._intent['action_kind'].startswith('upload_')
        version = self._intent['approved_local' if local else 'approved_remote']
        if version is None and kind == 'incoming':
            version = self._intent.get('expected_payload')
        if version is None or _checksum(md5, size) != _checksum(version.get('md5'), version.get('size')):
            raise JournalError('Recovery bytes do not match immutable approval')

    def preserve_local_backup(self, source, *, md5, size, identity=None):
        self._read()
        self._approved_material('backup', md5, size)
        expected_source = self.journal.project_root / self._intent['path']
        source = _reject_links(source).resolve(strict=True)
        _reject_links(source)
        if source != expected_source:
            raise JournalError('Backup source is not the approved project path')
        approved_identity = self._intent['approved_local'].get('identity')
        if identity is not None and list(identity) != approved_identity:
            raise JournalError('Backup identity differs from immutable approval')
        material = _copy_verified(source, self.backup_path, md5, size, approved_identity)
        self._record(materials={'backup': material})
        return self.backup_path

    def preserve_incoming(self, source, *, md5, size):
        self._read()
        self._approved_material('incoming', md5, size)
        incoming = _copy_verified(source, self.incoming_path, md5, size)
        # The eventual destination must never share an inode with the retained
        # incoming evidence. Prepare this second copy before trashing anything.
        materials = {'incoming': incoming}
        if self._intent['action_kind'].startswith('download_'):
            materials['publication'] = _copy_verified(
                self.incoming_path, self.publication_path, md5, size, readonly=False)
        self._record(materials=materials)
        return self.incoming_path

    def verify_recovery_material(self):
        state = self._read()['state']
        action_kind = self._intent['action_kind']
        required = []
        if action_kind.startswith(('upload_', 'download_')):
            required.append('incoming')
        if action_kind.startswith('download_'):
            required.append('publication')
        if action_kind in ('download_revision', 'download_unknown', 'delete_local'):
            required.append('backup')
        identities = set()
        for kind in required:
            material = state['materials'].get(kind)
            if material is None:
                raise JournalError('Durable recovery material is incomplete: ' + kind)
            path = self.directory / material['name']
            before = _regular(path)
            identity = (before.st_dev, before.st_ino)
            # Hash equality does not prove independent recovery copies. An
            # existing hardlink alias would let an ordinary later CAD save
            # change retained evidence through the published destination.
            if before.st_nlink != 1 or identity in identities:
                raise JournalError('Recovery material must have independent, single-link storage')
            identities.add(identity)
            md5, size, hashed_identity = _hash(path)
            after = _regular(path)
            if (_identity(before) != hashed_identity or _identity(after) != hashed_identity
                    or after.st_nlink != 1):
                raise JournalError('Recovery material identity changed during verification')
            if (md5, size) != (material['md5'], material['size']):
                raise JournalError('Durable recovery material no longer matches its checksum')
        return True

    def publish_incoming(self):
        if self._intent['action_kind'] not in ('download_new', 'download_revision', 'download_unknown'):
            raise JournalError('This operation cannot publish a local download')
        if self._read()['state']['outcome'] != 'unknown':
            raise JournalError('Publication requires a durable mutation-started outcome')
        self.verify_recovery_material()
        destination = self.journal.project_root / self._intent['path']
        _reject_links(destination, missing=True)
        if os.path.lexists(destination):
            raise FileExistsError('Publication destination already exists')
        os.link(self.publication_path, destination, follow_symlinks=False)
        try:
            fsync_directory(destination.parent)
        except Exception as error:
            raise PublicationCommittedError('Download published, but its directory flush failed') from error
        return destination
