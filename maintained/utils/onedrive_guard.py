"""Non-mutating Windows path advisory; independent of CacheCAD and Windows APIs.

Injected configured_roots should come from a read-only Windows discovery adapter.
Environment values are configuration signals, not proof that sync is running.
No filesystem, registry, network, account, or target-app access occurs here.
"""
from dataclasses import dataclass
import ntpath
import re
from typing import Iterable, Mapping, Optional, Tuple


@dataclass(frozen=True)
class PathAssessment:
    status: str  # configured_root_match, name_hint, no_known_match, unknown
    normalized_path: Optional[str]
    matched_root: Optional[str] = None
    sources: Tuple[str, ...] = ()
    warning: Optional[str] = None
    limitations: Tuple[str, ...] = (
        "Lexical comparison only; junctions, symlinks, short names and mapped drives are not resolved.",
        "Root discovery may be incomplete or stale; no result proves current sync activity or safety.",
    )


def _normalize(value: str) -> Optional[str]:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        return None
    path = value.replace('/', '\\')
    # Accept ordinary extended drive/UNC paths, never device or volume namespaces.
    if path.lower().startswith('\\\\?\\unc\\'):
        path = '\\\\' + path[8:]
    elif path.startswith('\\\\?\\') and re.match(r'^[a-zA-Z]:\\', path[4:]):
        path = path[4:]
    elif path.startswith(('\\\\?\\', '\\\\.\\')):
        return None
    drive, tail = ntpath.splitdrive(path)
    if not drive or (not drive.startswith('\\\\') and not re.fullmatch('[A-Za-z]:', drive)):
        return None
    if drive.startswith('\\\\') and len([x for x in drive.split('\\') if x]) != 2:
        return None
    if not tail.startswith('\\') and not (drive.startswith('\\\\') and not tail):
        return None
    # Avoid silently assigning ordinary Win32 semantics to ambiguous input.
    if any(c in path for c in '<>"|?*%') or ':' in tail:
        return None
    components = path.split('\\')
    if any(x not in ('', '.', '..') and (x.endswith((' ', '.'))) for x in components):
        return None
    return ntpath.normcase(ntpath.normpath(path))


def assess_project_path(
    path: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    configured_roots: Iterable[str] = (),
) -> PathAssessment:
    """Assess a path with injected data; omitted env deliberately means no env.

    A configured_root_match means containment in a supplied/configuration root,
    NOT verified OneDrive sync. no_known_match must never be labelled 'safe'.
    Path names alone only produce name_hint. Unsupported/relative paths and
    absent or unusable discovery evidence produce unknown.
    """
    normalized = _normalize(path)
    if normalized is None:
        return PathAssessment('unknown', None, warning='Could not assess this Windows folder path.')
    roots = {}
    invalid_root = False
    candidates = [(root, 'configured_root') for root in configured_roots]
    for key, value in (env or {}).items():
        if key.lower() in ('onedrive', 'onedriveconsumer', 'onedrivecommercial') and value:
            candidates.append((value, 'environment:' + key))
    for root, source in candidates:
        canonical = _normalize(root)
        if canonical is None:
            invalid_root = True
            continue
        roots.setdefault(canonical, []).append(source)
    # Most specific match is useful when several accounts/roots overlap.
    for root in sorted(roots, key=len, reverse=True):
        if normalized == root or normalized.startswith(root.rstrip('\\') + '\\'):
            return PathAssessment(
                'configured_root_match', normalized, root, tuple(roots[root]),
                'This folder is inside a configured OneDrive location. OneDrive and CacheCAD '
                'may both update the project files, causing conflicts or unavailable files. '
                'Choose a local folder outside OneDrive, or continue with this risk. '
                'No files have been moved or changed.',
            )
    if any(part == 'onedrive' or part.startswith('onedrive - ')
           for part in normalized.split('\\')):
        return PathAssessment(
            'name_hint', normalized,
            warning='This folder name suggests OneDrive, but that could not be confirmed. '
                    'Check whether OneDrive syncs this location before using it for CacheCAD.',
        )
    if not roots or invalid_root:
        return PathAssessment('unknown', normalized,
                              warning='OneDrive location information is missing or incomplete.')
    return PathAssessment('no_known_match', normalized)
