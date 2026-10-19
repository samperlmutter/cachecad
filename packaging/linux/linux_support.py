"""Linux package checks; native inspection never executes a bundled binary."""
import importlib.util
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import struct
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location('_cachecad_linux_shared', ROOT / 'packaging/windows/support.py')
_shared = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_shared)
API_IMPORTS = _shared.API_IMPORTS
TYPE_IMPORTS = _shared.TYPE_IMPORTS
SOURCE_PACKAGES = _shared.SOURCE_PACKAGES + ('cachecad',)
locked_distributions = _shared.locked_distributions
sha256 = _shared.sha256
stdlib_hidden_imports = _shared.stdlib_hidden_imports
validate_application_smoke = _shared.validate_application_smoke
verified_payload = _shared.verified_payload
APP_NAME = 'CacheCAD'
EXECUTABLE = 'CacheCAD'
ARCHIVE_NAME = 'CacheCAD-Linux-x86_64.tar.gz'


def excluded_frozen_module(name):
    return any(name == prefix or name.startswith(prefix + '.') for prefix in SOURCE_PACKAGES)


def elf_architecture(header):
    if header[:4] != b'\x7fELF':
        return None
    if len(header) < 64 or header[4:7] != bytes((2, 1, 1)):
        raise ValueError('A Linux native binary must be a complete little-endian ELF64 header')
    kind, machine, version = struct.unpack_from('<HHI', header, 16)
    if machine != 62 or kind not in (2, 3) or version != 1:
        raise ValueError('A Linux native binary must target x86_64')
    return 'x86_64'


def glibc_requirements(readelf_output):
    """Use only version needs, not versions exported by a bundled library."""
    needed = readelf_output.partition('Version needs section')[2]
    return sorted(set(re.findall(r'\bName: GLIBC_([0-9]+(?:\.[0-9]+)+)\b', needed)),
                  key=lambda value: tuple(map(int, value.split('.'))))


def audit_native_files(root):
    root = Path(root).resolve(strict=True)
    records = []
    foreign = {b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xce', b'\xcf\xfa\xed\xfe',
               b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError('Bundle symlink escapes application')
            continue
        if not path.is_file():
            continue
        with path.open('rb') as stream:
            header = stream.read(64)
        if header.startswith(b'MZ') or header[:4] in foreign:
            raise ValueError('Foreign-platform executable in Linux package')
        architecture = elf_architecture(header)
        if architecture is None:
            continue
        output = subprocess.run(['readelf', '--version-info', str(path)], check=True,
                                capture_output=True, text=True, timeout=30,
                                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}).stdout
        records.append({'path': path.relative_to(root).as_posix(), 'architecture': architecture,
                        'glibc_required': glibc_requirements(output),
                        'sha256': sha256(path), 'bytes': path.stat().st_size})
    if not records:
        raise ValueError('No native ELF application binaries found')
    return records


def minimum_glibc(records):
    versions = [version for item in records for version in item['glibc_required']]
    if not versions:
        raise ValueError('No GLIBC version requirements found')
    return max(versions, key=lambda value: tuple(map(int, value.split('.'))))


def bundle_source(bundle):
    root = Path(bundle).resolve(strict=True)
    source = root / '_internal/app-source'
    if source.is_symlink() or not source.resolve(strict=True).is_relative_to(root):
        raise ValueError('Bundled source escapes application')
    verified_payload(source)
    return source


def snapshot(root):
    result = {}
    for path in sorted(Path(root).rglob('*')):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = {'symlink': os.readlink(path)}
        elif path.is_file():
            result[name] = {'sha256': sha256(path), 'mode': path.stat().st_mode & 0o777}
    return result


def isolated_environment(root, *, display=None, xauthority=None):
    """Use a disposable home without account, Python, Qt, proxy or loader injection."""
    root = Path(root).resolve(strict=True)
    result = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'HOME': str(root),
              'TMPDIR': str(root / 'tmp'), 'XDG_CONFIG_HOME': str(root / 'config'),
              'XDG_CACHE_HOME': str(root / 'cache'), 'XDG_DATA_HOME': str(root / 'data'),
              'XDG_RUNTIME_DIR': str(root / 'run')}
    for name in ('tmp', 'config', 'cache', 'data', 'run'):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    if display is not None:
        if not re.fullmatch(r'(?:127\.0\.0\.1|localhost)?:[0-9]+(?:\.[0-9]+)?', display):
            raise ValueError('Only an explicitly selected local disposable X display is supported')
        result['DISPLAY'] = display
    if xauthority is not None:
        authority = Path(xauthority).absolute()
        if display is None or authority.is_symlink() or not authority.is_file():
            raise ValueError('A regular authority file and local disposable X display are required')
        result['XAUTHORITY'] = str(authority)
    return result


def validate_archive(path):
    """Reject traversal, devices, hard links, link-parent writes and unsafe modes."""
    entries, total = {}, 0
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            name = member.name
            relative = PurePosixPath(name)
            if (not relative.parts or relative.is_absolute() or '..' in relative.parts
                    or str(relative) != name or '\\' in name or ':' in name
                    or relative.parts[0] != APP_NAME or name in entries):
                raise ValueError('Unsafe or duplicate application archive member')
            if not (member.isdir() or member.isfile() or member.issym()) or member.mode & 0o7000:
                raise ValueError('Unsupported archive member type or privileged mode')
            if member.size < 0 or member.size > 512 * 1024 * 1024:
                raise ValueError('Invalid archive member size')
            total += member.size
            if total > 2 * 1024**3 or len(entries) >= 20000:
                raise ValueError('Application archive exceeds extraction limits')
            if member.issym():
                target = member.linkname
                resolved = posixpath.normpath(posixpath.join(str(relative.parent), target))
                if (not target or target.startswith('/') or '\\' in target or ':' in target
                        or not PurePosixPath(resolved).parts or PurePosixPath(resolved).parts[0] != APP_NAME):
                    raise ValueError('Application archive symlink escapes its directory')
            entries[name] = member
    for name, member in entries.items():
        for parent in PurePosixPath(name).parents:
            if str(parent) in entries and not entries[str(parent)].isdir():
                raise ValueError('Archive member has a non-directory parent')
        if member.issym():
            seen, target = {name}, name
            while entries[target].issym():
                link = entries[target]
                target = posixpath.normpath(posixpath.join(str(PurePosixPath(target).parent), link.linkname))
                if target in seen or target not in entries:
                    raise ValueError('Archive symlink is cyclic or dangling')
                seen.add(target)
            if not entries[target].isfile():
                raise ValueError('Archive symlink must resolve to a regular file')
    executable = entries.get(f'{APP_NAME}/{EXECUTABLE}')
    if executable is None or not executable.isfile() or not executable.mode & 0o111:
        raise ValueError('Archive is missing its real executable or executable permissions')
    if f'{APP_NAME}/_internal/app-source/assembly-manifest.json' not in entries:
        raise ValueError('Archive is missing reviewed application source')
    return len(entries)


def extract_archive(archive, destination):
    validate_archive(archive)
    destination = Path(destination).absolute()
    if '..' in destination.parts:
        raise ValueError('Traversal in extraction destination')
    def check_destination():
        if any(path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())
               for path in (destination, *destination.parents)):
            raise ValueError('Linked extraction destination or ancestor rejected')
    check_destination()
    destination.mkdir(parents=True, exist_ok=False)
    check_destination()
    with tarfile.open(archive, 'r:gz') as stream:
        stream.extractall(destination, filter='data')
    bundle = destination / APP_NAME
    bundle_source(bundle)
    return bundle
