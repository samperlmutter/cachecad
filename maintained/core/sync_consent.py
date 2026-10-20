"""Immutable scan/consent versions and fail-closed destructive sync preconditions.

These are observed-version checks, not compare-and-swap. Drive v3 updates in
this application have no validated conditional-write contract, and filesystem
trash is not conditional on identity. A residual check/write race remains.
No baseline model is modified and no test transport is present in production.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from core.file_identity import file_identity, handle_identity


DESTRUCTIVE_ACTIONS = frozenset((
    'upload_revision', 'download_revision', 'upload_unknown', 'download_unknown',
    'delete_local', 'delete_cloud'))
_ACTION_FIELDS = ('path', 'reason', 'size', 'md5_checksum', 'parent_id',
                  'modified_by', 'modified_time', 'drive_id', 'name', 'folder', 'parent_name')
_REMOTE_FIELDS = 'id,name,parents,md5Checksum,version,size,trashed,mimeType'
_FOLDER_MIME = 'application/vnd.google-apps.folder'


class StaleSyncPlan(RuntimeError):
    def __init__(self, reason):
        super().__init__(f'Sync stopped: {reason}. Rescan the project, review the changed files, and confirm again.')


def action_fingerprint(action):
    kind = getattr(action.action, 'value', action.action)
    values = tuple(getattr(action, field, None) for field in _ACTION_FIELDS)
    if not isinstance(kind, str) or any(type(value) not in (str, int, float, bool, type(None)) for value in values):
        raise StaleSyncPlan('the selected action has invalid metadata')
    return (kind,) + values


@dataclass(frozen=True)
class LocalVersion:
    exists: bool
    identity: tuple = ()
    md5: str = ''
    size: int = 0
    modified_time: float = 0


@dataclass(frozen=True)
class RemoteVersion:
    file_id: str
    name: str
    parents: tuple
    version: str
    md5: str
    size: int


@dataclass(frozen=True)
class RemoteFolder:
    file_id: str
    name: str
    parent_id: str


@dataclass(frozen=True)
class ObservedAction:
    fingerprint: tuple
    local: LocalVersion
    remote: object
    folders: tuple
    # First missing path component beneath the verified existing ancestors.
    absent_name: str = ''


@dataclass(frozen=True)
class ScanSnapshot:
    project_root: str
    project_id: str
    actions: tuple
    project_identity: tuple


@dataclass(frozen=True)
class SyncConsent:
    snapshot: ScanSnapshot
    selection: tuple


def _identity(info):
    return file_identity(info)


def _reject_path_links(path):
    # Path.resolve alone would follow a newly redirected ancestor. Windows
    # junctions are not symlinks, so check those explicitly as well.
    absolute = Path(path).absolute()
    for component in (*reversed(absolute.parents), absolute):
        if component.is_symlink() or (hasattr(component, 'is_junction') and component.is_junction()):
            raise StaleSyncPlan('a project path contains a symlink or junction')


def _project_root(snapshot):
    root = Path(snapshot.project_root)
    _reject_path_links(root)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != snapshot.project_identity:
        raise StaleSyncPlan('the project directory identity changed')
    return root


def project_path(root, relative):
    root = Path(root)
    relative = Path(relative)
    if (relative.is_absolute() or not relative.parts or '..' in relative.parts
            or relative.parts[0].casefold() == '.cachecad'):
        raise StaleSyncPlan('a selected path leaves the project')
    if root.is_symlink() or not root.is_dir():
        raise StaleSyncPlan('the project folder is unavailable or is a link')
    _reject_path_links(root)
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise StaleSyncPlan('a selected path contains a link or junction')
    if not path.resolve().is_relative_to(root.resolve()):
        raise StaleSyncPlan('a selected path resolves outside the project')
    return path


def capture_local_version(path, copy_to=None):
    """Read fresh bytes and stable object identity; never trust the MD5 cache."""
    path = Path(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        if copy_to is not None:
            raise StaleSyncPlan('the upload source disappeared')
        return LocalVersion(False)
    if not stat.S_ISREG(before.st_mode) or not before.st_ino:
        raise StaleSyncPlan('the local object is not an identifiable regular file')
    digest = hashlib.md5()
    with path.open('rb') as source:
        opened = os.fstat(source.fileno())
        target = Path(copy_to).open('xb') if copy_to is not None else None
        try:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
                if target is not None:
                    target.write(chunk)
            after = os.fstat(source.fileno())
            if target is not None:
                target.flush()
                os.fsync(target.fileno())
        finally:
            if target is not None:
                target.close()
    if (_identity(before) != _identity(opened) or handle_identity(opened) != handle_identity(after)
            or _identity(before) != _identity(after)
            or _identity(before) != _identity(path.lstat())):
        raise StaleSyncPlan('the local file changed while its version was read')
    return LocalVersion(True, _identity(before), digest.hexdigest(), before.st_size, before.st_mtime)


def _md5(value):
    if (not isinstance(value, str) or len(value) != 32
            or any(c not in '0123456789abcdefABCDEF' for c in value)):
        raise StaleSyncPlan('a file version has no usable checksum')
    return value.lower()


def _remote_version(item):
    if (not isinstance(item.id, str) or not item.id or not isinstance(item.name, str)
            or not item.name or not isinstance(item.parent, str) or not item.parent
            or item.version is None or not str(item.version).isdigit()
            or type(item.size) is not int or item.size < 0):
        raise StaleSyncPlan('the Drive scan lacks a complete file version')
    return RemoteVersion(item.id, item.name, (item.parent,), str(item.version), _md5(item.md5_checksum), item.size)


def capture_scan_snapshot(actions, local_items, drive_items, working_dir,
                          project_name, project_id, folder_dict):
    """Capture action-relevant versions at successful analysis completion.

    Pure local I/O: one fresh read per affected local path, including revisions.
    Remote identities/versions are copied from the complete tagged Drive scan.
    Missing or contradictory metadata prevents publishing a destructive plan.
    """
    root = Path(working_dir) / project_name
    if root.is_symlink() or not root.is_dir():
        raise StaleSyncPlan('the scanned project folder is unavailable')
    if not isinstance(project_id, str) or not project_id:
        raise StaleSyncPlan('the scanned Drive project has no identity')
    _reject_path_links(root)
    root = root.resolve()
    root_stat = root.stat()
    root_identity = (root_stat.st_dev, root_stat.st_ino)
    if not root_stat.st_ino:
        raise StaleSyncPlan('the project directory has no stable identity')
    local_by_path = {item.path: item for item in local_items if not item.is_folder}
    remote_by_path = {item.path: item for item in drive_items if not item.is_folder}
    folders_by_path = {item.path: item for item in drive_items if item.is_folder}
    versions = {}
    observed = []
    for action in actions:
        key = action_fingerprint(action)
        kind = key[0]
        if kind not in DESTRUCTIVE_ACTIONS | {'upload_new', 'download_new'}:
            raise StaleSyncPlan('an action type is unsupported')
        path = action.path
        if path not in versions:
            version = capture_local_version(project_path(root, path))
            scanned = local_by_path.get(path)
            if scanned is None:
                if version.exists:
                    raise StaleSyncPlan(f'{path} appeared after its scan')
            elif (not version.exists or version.md5 != _md5(scanned.md5_checksum)
                  or version.size != scanned.size or version.modified_time != scanned.modified_time):
                raise StaleSyncPlan(f'{path} changed after its scan')
            versions[path] = version
        local = versions[path]
        scanned_remote = remote_by_path.get(path)
        remote = _remote_version(scanned_remote) if scanned_remote is not None else None
        current_parent = project_id
        folders = []
        absent_name = ''
        parts = Path(path).parts
        for number, part in enumerate(parts[:-1], 1):
            parent_path = str(Path(*parts[:number]))
            folder = folders_by_path.get(parent_path)
            if folder is None:
                absent_name = part
                break
            if (folder.parent != current_parent or folder.name != part
                    or folder_dict.get(parent_path) != folder.id):
                raise StaleSyncPlan(f'{path} has inconsistent Drive folder ancestry')
            folders.append(RemoteFolder(folder.id, folder.name, folder.parent))
            current_parent = folder.id
        if remote is not None:
            if absent_name or remote.parents != (current_parent,) or remote.name != parts[-1]:
                raise StaleSyncPlan(f'{path} has inconsistent Drive location')
        else:
            absent_name = absent_name or parts[-1]
        expected_shape = {
            'upload_new': (True, False), 'download_new': (False, True),
            'upload_revision': (True, True), 'download_revision': (True, True),
            'upload_unknown': (True, True), 'download_unknown': (True, True),
            'delete_local': (True, False), 'delete_cloud': (False, True)}[kind]
        if (local.exists, remote is not None) != expected_shape:
            raise StaleSyncPlan(f'{path} no longer matches the proposed operation')
        source = local if kind.startswith('upload') or kind == 'delete_local' else remote
        if _md5(action.md5_checksum) != source.md5 or action.size != source.size:
            raise StaleSyncPlan(f'{path} action bytes differ from the scan')
        if remote is not None and action.drive_id != remote.file_id:
            raise StaleSyncPlan(f'{path} action targets a different Drive file')
        observed.append(ObservedAction(key, local, remote, tuple(folders), absent_name))
    return ScanSnapshot(str(root), project_id, tuple(observed), root_identity)


def approve_sync_actions(selection, snapshot):
    """Bind consent to exact index/action fields and both immutable versions.

    No I/O or Qt event processing. The UI calls only after explicit destructive
    confirmation, or its ordinary Sync click when there are no destructive rows.
    """
    if not isinstance(snapshot, ScanSnapshot):
        raise StaleSyncPlan('there is no complete observed scan')
    available = {item.fingerprint: item for item in snapshot.actions}
    frozen = tuple((index, action_fingerprint(action)) for index, action in selection)
    if not frozen or len({index for index, _ in frozen}) != len(frozen):
        raise StaleSyncPlan('the selection is empty or contains duplicate rows')
    paths = [fingerprint[1].casefold() for _, fingerprint in frozen]
    if len(set(paths)) != len(paths):
        raise StaleSyncPlan('more than one action targets the same path')
    if any(fingerprint not in available for _, fingerprint in frozen):
        raise StaleSyncPlan('the selection differs from the observed scan')
    return SyncConsent(snapshot, frozen)


def validate_selection(consent, selection, working_dir, project_name, project_id):
    if not isinstance(consent, SyncConsent):
        raise StaleSyncPlan('destructive actions require fresh explicit confirmation')
    _project_root(consent.snapshot)
    root = Path(working_dir) / project_name
    _reject_path_links(root)
    if (root.is_symlink() or str(root.resolve()) != consent.snapshot.project_root
            or project_id != consent.snapshot.project_id
            or tuple((index, action_fingerprint(action)) for index, action in selection) != consent.selection):
        raise StaleSyncPlan('the approved project or selection changed')


def approved_action(consent, action):
    if not isinstance(consent, SyncConsent):
        raise StaleSyncPlan('destructive actions require fresh explicit confirmation')
    key = (action[0], action_fingerprint(action[1]))
    if key not in consent.selection:
        raise StaleSyncPlan('the action is not in the confirmed selection')
    for observed in consent.snapshot.actions:
        if observed.fingerprint == key[1]:
            return observed
    raise StaleSyncPlan('the action has no observed source and destination versions')


def _get(files, file_id):
    response = files.get(fileId=file_id, fields=_REMOTE_FIELDS, supportsAllDrives=True).execute()
    if not isinstance(response, dict) or response.get('id') != file_id or type(response.get('trashed')) is not bool:
        raise StaleSyncPlan('Drive returned incomplete identity or trash metadata')
    if response['trashed']:
        raise StaleSyncPlan('a Drive object was moved to trash')
    return response


def _verify_remote(consent, observed, drive_client):
    service = drive_client._new_service()
    try:
        _verify_remote_files(consent, observed, service.files())
    finally:
        service.close()


def _verify_remote_files(consent, observed, files):
    root = _get(files, consent.snapshot.project_id)
    if root.get('mimeType') != _FOLDER_MIME:
        raise StaleSyncPlan('the Drive project is no longer a folder')
    parent_id = consent.snapshot.project_id
    for folder in observed.folders:
        now = _get(files, folder.file_id)
        if (now.get('name') != folder.name or now.get('parents') != [folder.parent_id]
                or now.get('mimeType') != _FOLDER_MIME):
            raise StaleSyncPlan('a Drive parent folder moved or changed identity')
        parent_id = folder.file_id
    remote = observed.remote
    if remote is not None:
        now = _get(files, remote.file_id)
        try:
            current = RemoteVersion(now['id'], now['name'], tuple(now['parents']),
                                    str(now['version']), _md5(now['md5Checksum']), int(now['size']))
        except (KeyError, TypeError, ValueError):
            raise StaleSyncPlan('Drive returned an incomplete file version') from None
        if current != remote:
            raise StaleSyncPlan('the Drive file version, name, or location changed')
    else:
        def quote(value):
            return value.replace('\\', '\\\\').replace("'", "\\'")
        query = f"'{quote(parent_id)}' in parents and name = '{quote(observed.absent_name)}' and trashed = false"
        token = None
        seen_tokens = set()
        while True:
            result = files.list(q=query, pageToken=token, pageSize=1000,
                                supportsAllDrives=True, includeItemsFromAllDrives=True,
                                fields='nextPageToken,incompleteSearch,files(id)').execute()
            if (not isinstance(result, dict) or result.get('incompleteSearch') is True
                    or not isinstance(result.get('files'), list)):
                raise StaleSyncPlan('Drive could not establish complete absence')
            if result['files']:
                raise StaleSyncPlan('a Drive file or folder appeared at the scanned missing path')
            token = result.get('nextPageToken')
            if not token:
                break
            if token in seen_tokens:
                raise StaleSyncPlan('Drive absence pagination did not complete')
            seen_tokens.add(token)


def _with_created_folders(consent, observed, created_folders):
    if not created_folders or observed.fingerprint[0] != 'upload_new':
        return observed
    parts = Path(observed.fingerprint[1]).parts
    folders = list(observed.folders)
    parent_id = folders[-1].file_id if folders else consent.snapshot.project_id
    for number in range(len(folders) + 1, len(parts)):
        created_id = created_folders.get(str(Path(*parts[:number])))
        if not created_id:
            return replace(observed, folders=tuple(folders), absent_name=parts[number - 1])
        folders.append(RemoteFolder(created_id, parts[number - 1], parent_id))
        parent_id = created_id
    return replace(observed, folders=tuple(folders), absent_name=parts[-1])


def verify_folder_creation(consent, folder, drive_client, created_folders):
    """Only create a still-missing ancestor of an approved new upload."""
    if not isinstance(consent, SyncConsent):
        raise StaleSyncPlan('folder creation has no approved scan')
    folder = Path(folder)
    selected = {fingerprint for _, fingerprint in consent.selection}
    source = next((item for item in consent.snapshot.actions
                   if item.fingerprint in selected and item.fingerprint[0] == 'upload_new'
                   and folder in Path(item.fingerprint[1]).parents), None)
    if source is None or folder == Path('.'):
        raise StaleSyncPlan('folder creation was not part of the confirmed upload')
    source = _with_created_folders(consent, source, created_folders)
    depth = len(folder.parts)
    if len(source.folders) < depth - 1:
        raise StaleSyncPlan('a new Drive folder has no verified parent')
    expected = replace(source, folders=source.folders[:depth - 1], absent_name=folder.name)
    _project_root(consent.snapshot)
    _verify_remote(consent, expected, drive_client)
    _project_root(consent.snapshot)
    return expected.folders[-1].file_id if expected.folders else consent.snapshot.project_id


def revalidate_action(consent, action, drive_client, *, created_folders=None):
    """Fresh read-only checks immediately before mutation; failures are stale."""
    try:
        observed = approved_action(consent, action)
        observed = _with_created_folders(consent, observed, created_folders)
        _project_root(consent.snapshot)
        _verify_remote(consent, observed, drive_client)
        path = project_path(_project_root(consent.snapshot), action[1].path)
        if capture_local_version(path) != observed.local:
            raise StaleSyncPlan('the local file version or presence changed')
        return observed
    except StaleSyncPlan:
        raise
    except Exception as error:
        raise StaleSyncPlan(f'the file versions could not be verified ({type(error).__name__})') from error


@contextmanager
def staged_revision_upload(consent, action):
    observed = approved_action(consent, action)
    path = project_path(_project_root(consent.snapshot), action[1].path)
    with tempfile.TemporaryDirectory(prefix='cachecad-revision-upload-') as folder:
        # System temporary roots may have a platform-provided alias.
        target = Path(folder).resolve(strict=True) / path.name
        if capture_local_version(path, copy_to=target) != observed.local:
            raise StaleSyncPlan('the upload source changed before staging')
        staged = capture_local_version(target)
        if not staged.exists or staged.md5 != observed.local.md5 or staged.size != observed.local.size:
            raise StaleSyncPlan('the staged upload bytes failed verification')
        target.chmod(stat.S_IRUSR)
        try:
            yield target
        finally:
            # Windows requires clearing read-only before TemporaryDirectory cleanup.
            if target.exists():
                target.chmod(stat.S_IRUSR | stat.S_IWUSR)


def verify_downloaded_revision(returned, staging, expected):
    if returned is None:
        raise StaleSyncPlan('the download did not produce a complete staged file')
    path = Path(returned)
    if path.is_symlink() or path.parent.resolve() != Path(staging).resolve():
        raise StaleSyncPlan('the download returned an unowned temporary file')
    version = capture_local_version(path)
    if not version.exists or version.md5 != expected.md5 or version.size != expected.size:
        raise StaleSyncPlan('the downloaded bytes differ from the confirmed Drive version')
    return path
