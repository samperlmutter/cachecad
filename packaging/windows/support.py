"""Standard-library helpers shared by Windows packaging and its regression tests."""
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

APP_ID = '{D24539B1-3941-4273-9C44-A62A05A91623}'
APP_NAME = 'CacheCAD'
EXE_NAME = 'CacheCAD.exe'
INSTALLER_NAME = 'CacheCAD-Setup.exe'
INNO_VERSION = '6.7.1'
SOURCE_PACKAGES = ('app', 'core', 'gui', 'utils', 'resources_rc', 'build_resources',
                   'application', 'development', 'launch', 'signin', 'crash_diagnostics')
# First-party code is loaded from the reviewed source snapshot. Real Google SDKs
# are deliberately included: a production sign-in must yield usable credentials.
TYPE_IMPORTS = ('google.oauth2.credentials',)
API_IMPORTS = ('google_auth_oauthlib.flow', 'google.oauth2.id_token',
               'google.auth.transport.requests', 'googleapiclient.discovery',
               'googleapiclient.http', 'googleapiclient.errors',
               'google_auth_httplib2', 'httplib2', 'requests_oauthlib', 'oauthlib.oauth2')
API_DISTRIBUTIONS = ('google-api-python-client', 'google-api-core', 'google-auth',
                     'google-auth-httplib2', 'google-auth-oauthlib', 'httplib2', 'requests',
                     'requests-oauthlib', 'oauthlib', 'urllib3', 'certifi',
                     'charset-normalizer', 'idna', 'cachetools', 'rsa', 'pyasn1',
                     'pyasn1-modules', 'uritemplate', 'pyparsing')
REQUIRED_SOURCE = ('application/launch.py', 'application/resources_rc.py',
                   'application/crash_diagnostics.py',
                   'src/gui/main_window.py', 'src/core/auth_manager.py',
                   'src/core/drive_client.py', 'src/utils/application_mode.py',
                   'src/utils/windows_token_store.py', 'src/utils/profile_lock.py',
                   'src/core/google_session.py', 'src/gui/application_account.py')


def excluded_frozen_module(name):
    """Prevent frozen copies from shadowing first-party verified source."""
    return any(name == prefix or name.startswith(prefix + '.') for prefix in SOURCE_PACKAGES)


def locked_distributions(path):
    """Retain notices for the complete hash-locked runtime dependency closure."""
    result = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or line == '--only-binary :all:':
            continue
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)==[^\s;]+(?:\s+--hash=sha256:[a-f0-9]{64})+', line)
        if match is None or match[1].lower().replace('_', '-') in result:
            raise ValueError('Runtime distribution metadata requires a unique hash-locked package')
        result.append(match[1].lower().replace('_', '-'))
    if not result:
        raise ValueError('Runtime dependency lock is empty')
    return tuple(result)


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
    for name in result:
        if name.startswith(('development/', 'tests/')) or name.endswith(('.pyc', '.pyo')):
            raise ValueError('Development/test code cannot enter the production payload: ' + name)
    if any(entry.get('origin') == 'offline-development' for entry in manifest['files']):
        raise ValueError('Offline fixture artifacts cannot enter the production payload')
    for required in REQUIRED_SOURCE:
        if required not in result:
            raise ValueError('Missing required application payload: ' + required)
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


def validate_application_smoke(stdout, platform):
    """Require a real installed, signed-out app with no authentication effects."""
    lines = [line.partition('=')[2] for line in stdout.splitlines()
             if line.startswith('CACHECAD_APPLICATION_RESULT=')]
    if len(lines) != 1:
        raise ValueError('Expected exactly one installed application result record')
    result = json.loads(lines[0])
    if not isinstance(result, dict):
        raise ValueError('Installed application result must be an object')
    expected = {'mode': 'application', 'actual_gui': 'gui.main_window.MainWindow',
                'qt_platform': platform, 'frozen': True, 'source_loader': 'verified-source',
                'signed_in': False, 'has_credentials': False, 'drive_client_created': False,
                'network_disabled': True, 'active_threads': 0, 'setup_required': True,
                'sdk_imports_verified': True, 'certificate_bundle_present': True,
                'drive_discovery_present': True}
    for key, value in expected.items():
        if type(result.get(key)) is not type(value) or result.get(key) != value:
            raise ValueError(f'Unexpected application smoke result {key}: {result.get(key)!r}')
    for key in ('errors', 'external_attempts', 'development_modules_loaded',
                'forbidden_modules_loaded', 'credential_files'):
        if result.get(key) != []:
            raise ValueError('Signed-out startup invariant failed: ' + key)
    size = result.get('rendered_size', [])
    if not isinstance(size, list) or len(size) != 2 or any(type(n) is not int or n <= 100 for n in size):
        raise ValueError('Actual application GUI was not rendered at an expected size')
    return result


def client_config_bytes(path):
    """Accept only public Desktop-client metadata, never credentials or tokens.

    This optional owner input is copied into the installer. The build deliberately
    neither creates a client nor requests an OAuth grant. Unknown metadata is
    rejected so an accidentally selected token/service-account file cannot ship.
    """
    from urllib.parse import urlsplit

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate client configuration field')
            result[key] = value
        return result

    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Owner Desktop client configuration must be a regular file')
    with path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('Owner Desktop client configuration is too large')
    try:
        config = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
        if not isinstance(config, dict) or set(config) != {'installed'}:
            raise ValueError
        installed = config['installed']
        allowed = {'client_id', 'client_secret', 'auth_uri', 'token_uri', 'redirect_uris',
                   'project_id', 'auth_provider_x509_cert_url'}
        if not isinstance(installed, dict) or set(installed) - allowed:
            raise ValueError
        client_id = installed['client_id']
        if not isinstance(client_id, str) or not re.fullmatch(
                r'[A-Za-z0-9_-]{1,256}\.apps\.googleusercontent\.com', client_id):
            raise ValueError
        auth_uri = 'https://accounts.google.com/o/oauth2/v2/auth'
        if installed['auth_uri'] not in (auth_uri, 'https://accounts.google.com/o/oauth2/auth'):
            raise ValueError
        if installed['token_uri'] != 'https://oauth2.googleapis.com/token':
            raise ValueError
        if installed.get('auth_provider_x509_cert_url', 'https://www.googleapis.com/oauth2/v1/certs') != 'https://www.googleapis.com/oauth2/v1/certs':
            raise ValueError
        secret = installed.get('client_secret', '')
        if not isinstance(secret, str) or len(secret) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in secret):
            raise ValueError
        redirects = installed['redirect_uris']
        if not isinstance(redirects, list) or not 1 <= len(redirects) <= 8:
            raise ValueError
        for uri in redirects:
            if not isinstance(uri, str) or len(uri) > 256:
                raise ValueError
            parsed = urlsplit(uri)
            if (parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1') or
                    parsed.username is not None or parsed.password is not None or
                    parsed.path not in ('', '/') or parsed.query or parsed.fragment or
                    (parsed.port is not None and not 1 <= parsed.port <= 65535)):
                raise ValueError
        sanitized = {'installed': {'client_id': client_id, 'client_secret': secret,
                     'auth_uri': auth_uri, 'token_uri': installed['token_uri'],
                     'redirect_uris': redirects}}
        return (json.dumps(sanitized, indent=2, sort_keys=True) + '\n').encode('utf-8')
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ValueError('Invalid owner Desktop client configuration; tokens and service accounts are forbidden') from None


def add_owner_client_config(root, selected_path):
    """Manifest only an explicitly supplied, sanitized Desktop client input."""
    raw = client_config_bytes(selected_path)
    root = Path(root)
    manifest_path = root / 'assembly-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    name = 'config/google-desktop-client.json'
    if any(entry['path'] == name for entry in manifest['files']):
        raise ValueError('Owner client configuration already exists in this assembly')
    target = root / name
    target.parent.mkdir(exist_ok=True)
    with target.open('xb') as stream:
        stream.write(raw)
    digest = hashlib.sha256(raw).hexdigest()
    manifest['files'].append({'path': name, 'sha256': digest, 'origin': 'owner-desktop-client'})
    manifest['files'].sort(key=lambda item: item['path'])
    manifest['owner_desktop_client_configured'] = True
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return digest
