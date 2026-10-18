"""Build the unsigned Windows x64 offline-development installer; never download tools."""
import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from support import EXE_NAME, INSTALLER_NAME, inno_version, sha256, verified_payload

ROOT = Path(__file__).resolve().parents[2]


def run_logged(command, log, *, env=None):
    with Path(log).open('w', encoding='utf-8') as stream:
        process = subprocess.run([str(arg) for arg in command], cwd=ROOT, env=env,
                                 stdout=stream, stderr=subprocess.STDOUT, timeout=900)
    if process.returncode:
        raise RuntimeError(f'Build step failed ({process.returncode}); see {log}')


def find_iscc(explicit=None):
    candidates = [explicit, shutil.which('ISCC.exe'),
                  Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Inno Setup 6/ISCC.exe',
                  Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Inno Setup 6/ISCC.exe']
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise FileNotFoundError('Preinstalled official Inno Setup 6.7.1 compiler not found; no download attempted')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--iscc', type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64') or sys.maxsize <= 2**32:
        parser.error('Build on native x64 Windows with 64-bit CPython 3.12')
    if sys.version_info[:2] != (3, 12):
        parser.error('CPython 3.12 is required by the reviewed runtime lock')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    logs = output / 'build-logs'
    logs.mkdir()
    iscc = find_iscc(args.iscc)
    version_probe = subprocess.run([str(iscc), '/?'], capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', timeout=30)
    compiler_output = version_probe.stdout + version_probe.stderr
    (logs / 'inno-version.log').write_text(compiler_output, encoding='utf-8')
    version = inno_version(compiler_output)
    source = output / 'app-source'
    run_logged([sys.executable, '-B', ROOT / 'tooling/assemble_app.py', source], logs / 'assemble.log')
    verified_payload(source)
    environment = dict(os.environ, CACHECAD_APP_SOURCE=str(source), PYTHONDONTWRITEBYTECODE='1')
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    run_logged([sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
                '--distpath', output / 'bundle', '--workpath', output / 'pyinstaller-work',
                ROOT / 'packaging/windows/cachecad.spec'], logs / 'pyinstaller.log', env=environment)
    bundle = output / 'bundle/CacheCAD-Offline'
    if not (bundle / EXE_NAME).is_file():
        raise RuntimeError('Frozen executable is missing')
    original = verified_payload(source)
    bundled = verified_payload(bundle / '_internal/app-source')
    if {key: sha256(value) for key, value in original.items()} != {key: sha256(value) for key, value in bundled.items()}:
        raise RuntimeError('Bundled source differs from reviewed assembly')
    # These DLLs prove both test platforms can be attempted; runtime tests establish loading.
    for plugin in ('qwindows.dll', 'qoffscreen.dll'):
        if not list(bundle.rglob(plugin)):
            raise RuntimeError('Required Qt platform plugin is missing: ' + plugin)
    installer_dir = output / 'installer'
    installer_dir.mkdir()
    run_logged([iscc, '/Qp', f'/DBundleDir={bundle}', f'/DOutputDir={installer_dir}',
                ROOT / 'packaging/windows/cachecad.iss'], logs / 'inno-build.log')
    installer = installer_dir / INSTALLER_NAME
    if not installer.is_file():
        raise RuntimeError('Inno Setup did not produce the expected installer')
    git = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, capture_output=True, check=True)
    status = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, text=True, capture_output=True, check=True)
    inputs = ['requirements/runtime-windows.lock', 'requirements/windows-build.lock',
              'development/manifest.json', 'packaging/windows/cachecad.spec',
              'packaging/windows/cachecad.iss', 'packaging/windows/build.py',
              'packaging/windows/support.py', 'packaging/windows/test_installer.py',
              'packaging/windows/OFFLINE-README.txt']
    provenance = {
        'schema_version': 1, 'kind': 'unsigned offline development installer; personal/noncommercial tryout',
        'created_utc': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(),
        'python': sys.version, 'git_commit': git.stdout.strip(), 'git_worktree_status': status.stdout.splitlines(),
        'runner_image': os.environ.get('ImageVersion'), 'runner_image_os': os.environ.get('ImageOS'),
        'tools': {'inno_setup': version, 'iscc_sha256': sha256(iscc),
                  'pyinstaller': importlib.metadata.version('pyinstaller')},
        'input_sha256': {name: sha256(ROOT / name) for name in inputs},
        'assembly_manifest_sha256': sha256(source / 'assembly-manifest.json'),
        'installer': {'path': 'installer/' + INSTALLER_NAME, 'sha256': sha256(installer), 'bytes': installer.stat().st_size},
        'bundle_files': [{'path': path.relative_to(bundle).as_posix(), 'sha256': sha256(path), 'bytes': path.stat().st_size}
                         for path in sorted(bundle.rglob('*')) if path.is_file()],
        'verification': {'bundled_source_hashes_match': True, 'installed_runtime_test': 'pending',
                         'binary_reproducibility': 'not established', 'code_signed': False},
    }
    (output / 'build-provenance.json').write_text(json.dumps(provenance, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    (output / 'installer.sha256').write_text(f'{sha256(installer)}  {INSTALLER_NAME}\n', encoding='ascii')
    print(f'Built {installer}; installed-runtime validation remains required')


if __name__ == '__main__':
    main()
