"""Build a native Linux x86_64 application tarball from reviewed production source."""
import argparse
from datetime import datetime, timezone
import gzip
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile

from linux_support import (APP_NAME, ARCHIVE_NAME, EXECUTABLE, ROOT, audit_native_files,
                           bundle_source, minimum_glibc, sha256, snapshot, validate_archive,
                           verified_payload)

sys.path.insert(0, str(ROOT))
from tooling.version import load_identity  # noqa: E402


def run_logged(command, log, *, env=None, timeout=1200):
    with Path(log).open('w', encoding='utf-8') as stream:
        process = subprocess.run([str(item) for item in command], cwd=ROOT, env=env,
                                 stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
    if process.returncode:
        print('\n'.join(Path(log).read_text(encoding='utf-8', errors='replace').splitlines()[-60:]), file=sys.stderr)
        raise RuntimeError(f'Build step failed ({process.returncode}); see {log}')


def create_archive(bundle, archive):
    """Preserve executable modes and internal symlinks; normalize archive metadata."""
    def metadata(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        info.mtime = 0
        return info
    with Path(archive).open('xb') as output:
        with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0, compresslevel=6) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as stream:
                for path in [Path(bundle), *sorted(Path(bundle).rglob('*'))]:
                    stream.add(path, arcname=str(Path(APP_NAME) / path.relative_to(bundle)),
                               recursive=False, filter=metadata)
    validate_archive(archive)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--version-file', type=Path, default=ROOT / 'version.json')
    parser.add_argument('--system-library-dir', type=Path,
                        help='Explicit reviewed system-library directory for dependency collection')
    args = parser.parse_args()
    if sys.platform != 'linux' or platform.machine().lower() not in ('x86_64', 'amd64') or sys.maxsize <= 2**32:
        parser.error('Build on native x86_64 Linux; cross-platform builds are rejected')
    if sys.version_info[:2] != (3, 12):
        parser.error('CPython 3.12 is required by the reviewed dependency locks')
    identity = load_identity(args.version_file)
    library_dir = None
    if args.system_library_dir is not None:
        library_dir = args.system_library_dir.absolute()
        if (not library_dir.is_dir() or '..' in library_dir.parts
                or any(path.is_symlink() for path in (library_dir, *library_dir.parents))):
            parser.error('The explicit system-library directory must be a real unlinked directory')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    logs = output / 'build-logs'
    logs.mkdir()
    source = output / 'app-source'
    run_logged([sys.executable, '-B', ROOT / 'tooling/assemble_app.py', source, '--production',
                '--version-file', args.version_file.resolve()], logs / 'assemble.log')
    original = verified_payload(source)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('PYTHON', 'QT_', 'QML', 'LD_', 'CACHECAD_'))}
    environment.update(CACHECAD_APP_SOURCE=str(source), PYTHONDONTWRITEBYTECODE='1',
                       PYINSTALLER_CONFIG_DIR=str(output / 'pyinstaller-cache'),
                       XDG_CACHE_HOME=str(output / 'build-cache'))
    if library_dir is not None:
        environment['LD_LIBRARY_PATH'] = str(library_dir)
    run_logged([sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
                '--distpath', output / 'bundle', '--workpath', output / 'pyinstaller-work',
                ROOT / 'packaging/linux/cachecad.spec'], logs / 'pyinstaller.log', env=environment)
    bundle = output / 'bundle' / APP_NAME
    executable = bundle / EXECUTABLE
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError('Built application executable is missing')
    bundled = verified_payload(bundle_source(bundle))
    if {key: sha256(value) for key, value in original.items()} != {key: sha256(value) for key, value in bundled.items()}:
        raise RuntimeError('Bundled source differs from reviewed production assembly')
    for name in ('drive.v3.json', 'libqoffscreen.so', 'libqxcb.so', 'libxcb-shape.so.0'):
        if not list(bundle.rglob(name)):
            raise RuntimeError('Required runtime data/plugin is missing: ' + name)
    if list(bundle.rglob('CacheCADUpdateHelper*')):
        raise RuntimeError('A Windows update helper cannot ship on Linux')
    native = audit_native_files(bundle)
    archive = output / ARCHIVE_NAME
    create_archive(bundle, archive)
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip()
    status = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, text=True, capture_output=True, check=True).stdout.splitlines()
    inputs = ['version.json', 'tooling/version.py', 'tooling/assemble_app.py',
              'application/manifest.json', 'application/launch.py', 'application/crash_diagnostics.py',
              'packaging/windows/support.py', 'requirements/runtime-linux.lock', 'requirements/linux-package.lock']
    inputs += [path.relative_to(ROOT).as_posix() for path in sorted((ROOT / 'packaging/linux').iterdir()) if path.is_file()]
    provenance = {
        'schema_version': 1, 'kind': 'maintained application; Linux x86_64 tar.gz',
        'release_identity': identity, 'created_utc': datetime.now(timezone.utc).isoformat(),
        'platform': platform.platform(), 'python': sys.version, 'architecture': 'x86_64',
        'host_libc': platform.libc_ver(), 'minimum_glibc_required': minimum_glibc(native),
        'explicit_system_library_input': ({'path': str(library_dir), 'files': snapshot(library_dir)}
                                          if library_dir is not None else None),
        'compatibility_note': 'Native runtime validated only on the test host; a GLIBC floor alone is not full distro compatibility.',
        'git_commit': commit, 'git_worktree_status': status,
        'tools': {name: importlib.metadata.version(name) for name in ('pyinstaller', 'pyinstaller-hooks-contrib')},
        'input_sha256': {name: sha256(ROOT / name) for name in inputs},
        'version_input_sha256': sha256(args.version_file),
        'assembly_manifest_sha256': sha256(source / 'assembly-manifest.json'),
        'archive': {'path': ARCHIVE_NAME, 'sha256': sha256(archive), 'bytes': archive.stat().st_size},
        'bundle_files': snapshot(bundle), 'native_binaries': native,
        'verification': {'bundled_source_hashes_match': True, 'development_payload_absent': True,
                         'native_x86_64_audit': True, 'glibc_requirements_audited': True,
                         'extracted_app_runtime': 'pending', 'live_authentication_verified': False,
                         'binary_reproducibility': 'not established'},
    }
    (output / 'build-provenance.json').write_text(json.dumps(provenance, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    (output / 'archive.sha256').write_text(f'{sha256(archive)}  {ARCHIVE_NAME}\n', encoding='ascii')
    print(f'Built {archive}; extracted-app runtime validation remains required')


if __name__ == '__main__':
    main()
