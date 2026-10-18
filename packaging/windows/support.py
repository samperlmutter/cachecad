"""Standard-library helpers shared by Windows packaging and its regression tests."""
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

APP_ID = '{4A03D312-83C5-4EC3-9C46-8CE43DA792EA}'
APP_NAME = 'CacheCAD Offline Development'
EXE_NAME = 'CacheCAD-Offline.exe'
INSTALLER_NAME = 'CacheCAD-Offline-Development-Setup.exe'
INNO_VERSION = '6.7.1'
SOURCE_PACKAGES = ('app', 'core', 'gui', 'utils', 'resources_rc', 'build_resources')
LIVE_PACKAGES = ('google', 'googleapiclient', 'google_auth_oauthlib', 'requests',
                 'httplib2', 'oauthlib', 'requests_oauthlib')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verified_payload(root):
    """Return only manifest-listed data, refusing traversal, links and stale bytecode."""
    root = Path(root).resolve(strict=True)
    manifest_path = root / 'assembly-manifest.json'
    if manifest_path.is_symlink():
        raise ValueError('Symlink application manifest rejected')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    result = {'assembly-manifest.json': manifest_path}
    for entry in manifest['files']:
        name = entry['path']
        relative = PurePosixPath(name)
        if (not name or relative.is_absolute() or '..' in relative.parts or
                '\\' in name or ':' in name or name in result):
            raise ValueError('Unsafe or duplicate application payload path')
        path = root.joinpath(*relative.parts)
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root):
            raise ValueError('Symlink application payload rejected')
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError('Application payload escapes its root')
        if path.suffix in ('.pyc', '.pyo') or sha256(path) != entry['sha256']:
            raise ValueError('Application payload hash/bytecode failure: ' + name)
        result[name] = path
    listed_python = {path for name, path in result.items() if path.suffix == '.py'}
    actual_python = set(root.rglob('*.py'))
    if listed_python != actual_python or list(root.rglob('*.pyc')) or list(root.rglob('*.pyo')):
        raise ValueError('Unmanifested Python or bytecode in application payload')
    for required in ('development/launch.py', 'development/resources_rc.py',
                     'development/build_resources.py', 'src/gui/main_window.py'):
        if required not in result:
            raise ValueError('Missing required offline payload: ' + required)
    return result


def stdlib_hidden_imports(paths):
    """Find stdlib dependencies without importing recovered application modules."""
    imports = set()
    for path in paths:
        if Path(path).suffix != '.py':
            continue
        for node in ast.walk(ast.parse(Path(path).read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names = [node.module]
            else:
                continue
            imports.update(name for name in names if name.partition('.')[0] in sys.stdlib_module_names)
    return sorted(imports)


def inno_version(output):
    """Require the compiler engine's exact release banner from a normal compile."""
    matches = re.findall(r'^Compiler engine version:[ \t]+(?:Inno Setup[ \t]+)?'
                         r'(\d+\.\d+\.\d+)[ \t]*\r?$', output, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError('Cannot identify the installed Inno Setup compiler version')
    if matches[0] != INNO_VERSION:
        raise ValueError(f'Inno Setup {INNO_VERSION} required; found {matches[0]}')
    return matches[0]


def decode_file_version(file_version_ms, file_version_ls):
    """Decode the four WORDs in Windows VS_FIXEDFILEINFO's two DWORDs."""
    for value in (file_version_ms, file_version_ls):
        if type(value) is not int or not 0 <= value <= 0xffffffff:
            raise ValueError('Invalid PE file-version DWORD')
    return (file_version_ms >> 16, file_version_ms & 0xffff,
            file_version_ls >> 16, file_version_ls & 0xffff)


def pe_file_version(path):
    """Read numeric VERSIONINFO without running the executable or trusting /? text."""
    import pefile
    with pefile.PE(str(path), fast_load=True) as executable:
        executable.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']])
        records = getattr(executable, 'VS_FIXEDFILEINFO', [])
        if len(records) != 1 or records[0].Signature != 0xfeef04bd:
            raise ValueError('Missing, ambiguous or invalid PE fixed file-version resource')
        return decode_file_version(records[0].FileVersionMS, records[0].FileVersionLS)


def validate_smoke(stdout, scenario, platform):
    lines = [line.partition('=')[2] for line in stdout.splitlines()
             if line.startswith('CACHECAD_OFFLINE_RESULT=')]
    if len(lines) != 1:
        raise ValueError('Expected exactly one installed-app result record')
    result = json.loads(lines[0])
    expected = {'mode': 'offline-development', 'actual_gui': 'gui.main_window.MainWindow',
                'qt_platform': platform, 'synthetic_remote': True, 'active_threads': 0,
                'frozen': True, 'source_loader': 'verified-source'}
    for key, value in expected.items():
        if result.get(key) != value:
            raise ValueError(f'Unexpected smoke result {key}: {result.get(key)!r}')
    for key in ('errors', 'external_attempts', 'forbidden_modules_loaded',
                'credential_files', 'slot_errors', 'missing_placeholder_resources'):
        if result.get(key) != []:
            raise ValueError('Offline safety/render invariant failed: ' + key)
    size = result.get('rendered_size', [])
    if len(size) != 2 or any(not isinstance(n, int) or n <= 100 for n in size):
        raise ValueError('Actual GUI was not rendered at an expected size')
    outcomes = {
        'success': {'cancelled': [], 'failed': [], 'succeeded': [1]},
        'lifecycle': {'cancelled': [1], 'failed': [2], 'succeeded': [3]},
        'close-pending': {'cancelled': [1], 'failed': [], 'succeeded': []},
    }
    writes = {'success': [1], 'lifecycle': [3], 'close-pending': []}
    if result.get('outcomes') != outcomes[scenario] or result.get('database_write_generations') != writes[scenario]:
        raise ValueError('Scan lifecycle isolation failed')
    if scenario == 'close-pending':
        if not result.get('closed_pending_scan') or result.get('scan_ready', False):
            raise ValueError('Pending scan did not close cleanly')
    elif (result.get('scan_ready') is not True or result.get('planned_paths') != ['part.txt'] or
          result.get('planned_uploads') != 1 or result.get('transfer_button_enabled') is not False):
        raise ValueError('Unexpected synthetic plan or enabled transfer control')
    return result
