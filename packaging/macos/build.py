"""Build an ad-hoc-only Apple Silicon .app ZIP from reviewed production source."""
import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from macos_support import (APP_NAME, EXECUTABLE, MINIMUM_MACOS, ROOT, ZIP_NAME,
                           audit_native_files, bundle_source, sha256, snapshot,
                           verified_payload, verify_bundle_info)

sys.path.insert(0, str(ROOT))
from tooling.version import load_identity  # noqa: E402


def run_logged(command, log, *, env=None, timeout=900):
    with Path(log).open('w', encoding='utf-8') as stream:
        try:
            process = subprocess.run([str(item) for item in command], cwd=ROOT, env=env,
                                     stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f'Build step timed out; see {log}') from error
    if process.returncode:
        print('\n'.join(Path(log).read_text(encoding='utf-8', errors='replace').splitlines()[-60:]), file=sys.stderr)
        raise RuntimeError(f'Build step failed ({process.returncode}); see {log}')


def check_signature(bundle, log):
    run_logged(['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=2', bundle], log)
    process = subprocess.run(['/usr/bin/codesign', '--display', '--verbose=4', str(bundle)],
                             capture_output=True, text=True, check=True, timeout=30)
    output = process.stdout + process.stderr
    Path(log).with_name('signature-identity.log').write_text(output, encoding='utf-8')
    if 'Signature=adhoc' not in output or 'Authority=' in output:
        raise RuntimeError('Only an ad-hoc signature is allowed for this tryout')
    return {'type': 'ad-hoc', 'developer_id': False, 'notarized': False, 'verified': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--version-file', type=Path, default=ROOT / 'version.json')
    args = parser.parse_args()
    if sys.platform != 'darwin' or platform.machine().lower() != 'arm64' or sys.maxsize <= 2**32:
        parser.error('Build on native Apple Silicon macOS with arm64 CPython 3.12; cross builds and Rosetta are rejected')
    if sys.version_info[:2] != (3, 12):
        parser.error('CPython 3.12 is required by the reviewed dependency locks')
    identity = load_identity(args.version_file)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    logs = output / 'build-logs'
    logs.mkdir()
    source = output / 'app-source'
    run_logged([sys.executable, '-B', ROOT / 'tooling/assemble_app.py', source, '--production',
                '--version-file', args.version_file.resolve()], logs / 'assemble.log')
    original = verified_payload(source)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('PYTHON', 'QT_', 'QML', 'DYLD_', 'CACHECAD_'))}
    environment.update(CACHECAD_APP_SOURCE=str(source), PYTHONDONTWRITEBYTECODE='1',
                       CACHECAD_APP_VERSION=identity['version'],
                       CACHECAD_RELEASE_NUMBER=str(identity['release_number']),
                       MACOSX_DEPLOYMENT_TARGET=MINIMUM_MACOS)
    run_logged([sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
                '--distpath', output / 'bundle', '--workpath', output / 'pyinstaller-work',
                ROOT / 'packaging/macos/cachecad.spec'], logs / 'pyinstaller.log', env=environment)
    bundle = output / 'bundle' / APP_NAME
    verify_bundle_info(bundle, identity)
    bundled = verified_payload(bundle_source(bundle))
    if {key: sha256(value) for key, value in original.items()} != {key: sha256(value) for key, value in bundled.items()}:
        raise RuntimeError('Bundled source differs from reviewed production assembly')
    for name in ('drive.v3.json', 'libqcocoa.dylib', 'libqoffscreen.dylib'):
        if not list(bundle.rglob(name)):
            raise RuntimeError('Required runtime data/plugin is missing: ' + name)
    if list(bundle.rglob('CacheCADUpdateHelper*')):
        raise RuntimeError('A Windows update helper must not be shipped on macOS')
    native = audit_native_files(bundle)
    architectures = subprocess.run(['/usr/bin/lipo', '-archs', str(bundle / EXECUTABLE)],
                                   capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    if architectures != 'arm64':
        raise RuntimeError('The final app executable is not native arm64 only')
    signature = check_signature(bundle, logs / 'codesign-verify.log')
    archive = output / ZIP_NAME
    # ditto preserves framework symlinks, executable modes, and .app layout.
    run_logged(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', bundle, archive], logs / 'archive.log')
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip()
    status = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, text=True, capture_output=True, check=True).stdout.splitlines()
    inputs = ['version.json', 'tooling/version.py', 'tooling/assemble_app.py',
              'application/manifest.json', 'application/launch.py', 'application/crash_diagnostics.py',
              'packaging/windows/support.py', 'requirements/runtime-macos.lock', 'requirements/macos-build.lock']
    inputs += [path.relative_to(ROOT).as_posix() for path in sorted((ROOT / 'packaging/macos').iterdir()) if path.is_file()]
    provenance = {
        'schema_version': 1, 'kind': 'maintained application; Apple Silicon macOS ZIP',
        'release_identity': identity, 'created_utc': datetime.now(timezone.utc).isoformat(),
        'platform': platform.platform(), 'python': sys.version, 'architecture': architectures,
        'minimum_macos': MINIMUM_MACOS, 'signature': signature,
        'git_commit': commit, 'git_worktree_status': status,
        'runner_image': os.environ.get('ImageVersion'), 'runner_image_os': os.environ.get('ImageOS'),
        'tools': {name: importlib.metadata.version(name) for name in ('pyinstaller', 'pyinstaller-hooks-contrib', 'macholib')},
        'input_sha256': {name: sha256(ROOT / name) for name in inputs},
        'version_input_sha256': sha256(args.version_file),
        'assembly_manifest_sha256': sha256(source / 'assembly-manifest.json'),
        'archive': {'path': ZIP_NAME, 'sha256': sha256(archive), 'bytes': archive.stat().st_size},
        'bundle_files': snapshot(bundle), 'native_binaries': native,
        'verification': {'bundled_source_hashes_match': True, 'development_payload_absent': True,
                         'native_arm64_audit': True, 'minimum_os_audit': True,
                         'extracted_app_runtime': 'pending', 'required_platforms': ['offscreen', 'cocoa'],
                         'live_authentication_verified': False, 'binary_reproducibility': 'not established'},
    }
    (output / 'build-provenance.json').write_text(json.dumps(provenance, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    (output / 'archive.sha256').write_text(f'{sha256(archive)}  {ZIP_NAME}\n', encoding='ascii')
    print(f'Built {archive}; extracted-app runtime validation remains required')


if __name__ == '__main__':
    main()
