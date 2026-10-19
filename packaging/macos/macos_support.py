"""Portable macOS bundle checks. Reading Mach-O headers never executes code."""
import importlib.util
import os
from pathlib import Path
import plistlib
import struct

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location('_cachecad_packaging_shared', ROOT / 'packaging/windows/support.py')
_shared = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_shared)
# Keep the existing manifest-bounded production payload and SDK checks identical.
API_IMPORTS = _shared.API_IMPORTS
TYPE_IMPORTS = _shared.TYPE_IMPORTS
SOURCE_PACKAGES = _shared.SOURCE_PACKAGES
excluded_frozen_module = _shared.excluded_frozen_module
locked_distributions = _shared.locked_distributions
sha256 = _shared.sha256
stdlib_hidden_imports = _shared.stdlib_hidden_imports
validate_application_smoke = _shared.validate_application_smoke
verified_payload = _shared.verified_payload

APP_NAME = 'CacheCAD.app'
EXECUTABLE = 'Contents/MacOS/CacheCAD'
ZIP_NAME = 'CacheCAD-macOS-arm64.zip'
BUNDLE_ID = 'org.cachecad.reconstructed'
MINIMUM_MACOS = '12.0'
_MACH_MAGIC = {b'\xce\xfa\xed\xfe': ('<', False), b'\xfe\xed\xfa\xce': ('>', False),
               b'\xcf\xfa\xed\xfe': ('<', True), b'\xfe\xed\xfa\xcf': ('>', True)}
_FAT_MAGIC = {b'\xca\xfe\xba\xbe': ('>', False), b'\xbe\xba\xfe\xca': ('<', False),
              b'\xca\xfe\xba\xbf': ('>', True), b'\xbf\xba\xfe\xca': ('<', True)}
_ARCHITECTURES = {0x0100000c: 'arm64', 0x01000007: 'x86_64', 12: 'arm', 7: 'x86'}


def version_tuple(value):
    parts = value.split('.')
    if not 1 <= len(parts) <= 3 or any(not item.isdigit() for item in parts):
        raise ValueError('Invalid macOS version')
    return tuple(map(int, parts)) + (0,) * (3 - len(parts))


def packed_version(value):
    return f'{value >> 16}.{(value >> 8) & 255}.{value & 255}'


def macho_slices(raw):
    """Read per-architecture minimum OS from thin/fat Mach-O load commands."""
    def thin(offset, length):
        magic = raw[offset:offset + 4]
        if magic not in _MACH_MAGIC:
            raise ValueError('Fat Mach-O slice has no valid header')
        endian, bits64 = _MACH_MAGIC[magic]
        header_size = 32 if bits64 else 28
        if length < header_size or offset + length > len(raw):
            raise ValueError('Truncated Mach-O header')
        cpu, subtype, kind, count, command_bytes = struct.unpack_from(endian + 'IIIII', raw, offset + 4)
        if not count or count > 100000 or header_size + command_bytes > length:
            raise ValueError('Invalid Mach-O load-command bounds')
        cursor, end = offset + header_size, offset + header_size + command_bytes
        minima = []
        for _ in range(count):
            if cursor + 8 > end:
                raise ValueError('Truncated Mach-O load command')
            command, size = struct.unpack_from(endian + 'II', raw, cursor)
            if size < 8 or cursor + size > end:
                raise ValueError('Invalid Mach-O load-command size')
            if command == 0x32:  # LC_BUILD_VERSION
                if size < 24:
                    raise ValueError('Truncated LC_BUILD_VERSION')
                platform, minimum = struct.unpack_from(endian + 'II', raw, cursor + 8)
                if platform != 1:
                    raise ValueError('Native binary does not target macOS')
                minima.append(packed_version(minimum))
            elif command == 0x24:  # LC_VERSION_MIN_MACOSX
                if size < 16:
                    raise ValueError('Truncated LC_VERSION_MIN_MACOSX')
                minima.append(packed_version(struct.unpack_from(endian + 'I', raw, cursor + 8)[0]))
            cursor += size
        if cursor != end or len(minima) != 1 or version_tuple(minima[0]) == (0, 0, 0):
            raise ValueError('Missing or ambiguous Mach-O minimum OS')
        return {'architecture': _ARCHITECTURES.get(cpu, f'cpu-{cpu:08x}'),
                'minimum_macos': minima[0]}

    magic = raw[:4]
    if magic in _MACH_MAGIC:
        return [thin(0, len(raw))]
    if magic not in _FAT_MAGIC:
        return []
    endian, fat64 = _FAT_MAGIC[magic]
    if len(raw) < 8:
        raise ValueError('Truncated fat Mach-O header')
    count = struct.unpack_from(endian + 'I', raw, 4)[0]
    entry_size = 32 if fat64 else 20
    if not 1 <= count <= 32 or 8 + count * entry_size > len(raw):
        raise ValueError('Invalid fat Mach-O architecture table')
    result, spans = [], []
    for number in range(count):
        cursor = 8 + number * entry_size
        cpu, subtype = struct.unpack_from(endian + 'II', raw, cursor)
        offset, length = struct.unpack_from(endian + ('QQ' if fat64 else 'II'), raw, cursor + 8)
        if offset < 8 + count * entry_size or any(offset < b and offset + length > a for a, b in spans):
            raise ValueError('Overlapping fat Mach-O slices')
        item = thin(offset, length)
        if item['architecture'] != _ARCHITECTURES.get(cpu, f'cpu-{cpu:08x}'):
            raise ValueError('Fat Mach-O architecture disagrees with slice')
        result.append(item)
        spans.append((offset, offset + length))
    if len({item['architecture'] for item in result}) != len(result):
        raise ValueError('Duplicate Mach-O architecture')
    return result


def audit_native_files(root, minimum_macos=MINIMUM_MACOS, *, require_thin=True):
    """Every shipped Mach-O must contain arm64 and fit the declared OS floor."""
    root = Path(root).resolve(strict=True)
    records = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError('Bundle symlink escapes application')
            continue
        if not path.is_file():
            continue
        with path.open('rb') as stream:
            magic = stream.read(4)
            if magic.startswith(b'MZ') or magic == b'\x7fELF':
                raise ValueError('Foreign-platform executable in macOS bundle')
            if magic not in _MACH_MAGIC and magic not in _FAT_MAGIC:
                continue
            stream.seek(0)
            slices = macho_slices(stream.read())
        arm = [item for item in slices if item['architecture'] == 'arm64']
        if len(arm) != 1 or (require_thin and len(slices) != 1):
            raise ValueError('Native binary must be arm64' + (' only' if require_thin else ' compatible'))
        if version_tuple(arm[0]['minimum_macos']) > version_tuple(minimum_macos):
            raise ValueError(f'Native minimum OS exceeds {minimum_macos}: {path.relative_to(root)}')
        records.append({'path': path.relative_to(root).as_posix(), 'slices': slices,
                        'sha256': sha256(path), 'bytes': path.stat().st_size})
    if not records:
        raise ValueError('No native Mach-O binaries found')
    return records


def bundle_source(bundle):
    # PyInstaller routes data to Resources and supplies Frameworks symlinks.
    root = Path(bundle).resolve(strict=True)
    source = (root / 'Contents/Resources/app-source').resolve(strict=True)
    if not source.is_relative_to(root):
        raise ValueError('Bundled source escapes application')
    verified_payload(source)
    return source


def verify_bundle_info(bundle, identity=None):
    root = Path(bundle)
    with (root / 'Contents/Info.plist').open('rb') as stream:
        info = plistlib.load(stream)
    expected = {'CFBundleExecutable': 'CacheCAD', 'CFBundleIdentifier': BUNDLE_ID,
                'CFBundlePackageType': 'APPL', 'LSMinimumSystemVersion': MINIMUM_MACOS,
                'NSHighResolutionCapable': True, 'LSBackgroundOnly': False}
    if identity is not None:
        expected.update(CFBundleShortVersionString=identity['version'],
                        CFBundleVersion=str(identity['release_number']))
    if any(type(info.get(key)) is not type(value) or info.get(key) != value for key, value in expected.items()):
        raise ValueError('Application bundle identity or minimum OS is incorrect')
    if not (root / EXECUTABLE).is_file() or not os.access(root / EXECUTABLE, os.X_OK):
        raise ValueError('Application executable is missing or not executable')
    return info


def isolated_environment(root):
    """Use a disposable home, no Python/Qt injection, account variables or proxies."""
    root = Path(root).resolve(strict=True)
    allowed = {'LANG', 'LC_ALL', 'LC_CTYPE', '__CF_USER_TEXT_ENCODING'}
    result = {key: value for key, value in os.environ.items() if key in allowed}
    result.update(PATH='/usr/bin:/bin:/usr/sbin:/sbin', HOME=str(root),
                  TMPDIR=str(root / 'tmp'), XDG_CONFIG_HOME=str(root / 'config'),
                  XDG_CACHE_HOME=str(root / 'cache'), XDG_DATA_HOME=str(root / 'data'))
    for directory in ('tmp', 'config', 'cache', 'data'):
        (root / directory).mkdir(exist_ok=True, mode=0o700)
    return result


def snapshot(root):
    result = {}
    for path in sorted(Path(root).rglob('*')):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = {'symlink': os.readlink(path)}
        elif path.is_file():
            result[name] = {'sha256': sha256(path), 'mode': path.stat().st_mode & 0o777}
    return result
