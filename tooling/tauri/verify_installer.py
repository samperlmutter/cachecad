#!/usr/bin/env python3
"""Install the actual CI artifact into a disposable directory and run its WebView.
The installed binary uses an isolated, network-disabled smoke profile. A report is
written only after React and a Three.js model have rendered in the real WebView.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import time


def run_smoke(executable, report, diagnostics):
    stdout = (diagnostics / 'native-stdout.log').open('wb')
    stderr = (diagnostics / 'native-stderr.log').open('wb')
    process = subprocess.Popen([str(executable), '--smoke-output', str(report)], stdout=stdout, stderr=stderr)
    try:
        deadline = time.monotonic() + 75
        while not report.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f'Installed app exited before rendering evidence (code {process.returncode})')
            time.sleep(0.5)
        if not report.exists():
            raise RuntimeError('Installed app did not render its workspace and 3D model within 75 seconds')
        process.wait(timeout=15)
        if process.returncode != 0:
            raise RuntimeError(f'Installed app exited with code {process.returncode}')
        evidence = json.loads(report.read_text())
        assert Path(evidence['installedExecutable']).resolve() == executable.resolve(), evidence
        assert evidence['isolatedProfile'] and evidence['networkServicesDisabled'], evidence
        assert evidence['ui']['workspaceVisible'] and evidence['ui']['modelRendered'], evidence
        assert (evidence['ui'].get('renderer') or 'webgl') in ('webgl', 'software-svg'), evidence
        assert evidence['ui']['canvasWidth'] >= 100 and evidence['ui']['canvasHeight'] >= 100, evidence
        return evidence
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        stdout.close()
        stderr.close()
        if report.exists():
            shutil.copy2(report, diagnostics / 'native-render.json')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    system = platform.system()
    pattern = '*.dmg' if system == 'Darwin' else '*-setup.exe'
    installers = list(args.bundle.rglob(pattern))
    if len(installers) != 1:
        raise RuntimeError(f'Expected exactly one {pattern} installer, found {installers}')
    installer = installers[0].resolve()
    with tempfile.TemporaryDirectory(prefix='permafrost-installer-') as tmp:
        root = Path(tmp)
        installed = root / 'installed'
        report = root / 'native-smoke.json'
        if system == 'Darwin':
            subprocess.run(['hdiutil', 'verify', str(installer)], check=True)
            mount = root / 'volume'
            mount.mkdir()
            subprocess.run(['hdiutil', 'attach', '-nobrowse', '-readonly', '-mountpoint', str(mount), str(installer)], check=True)
            try:
                apps = list(mount.glob('*.app'))
                assert len(apps) == 1, apps
                installed.mkdir()
                destination = installed / apps[0].name
                shutil.copytree(apps[0], destination, symlinks=True)
            finally:
                subprocess.run(['hdiutil', 'detach', str(mount)], check=True)
            executables = list((destination / 'Contents' / 'MacOS').iterdir())
            assert len(executables) == 1, executables
            executable = executables[0]
            subprocess.run(['codesign', '--verify', '--deep', '--strict', str(destination)], check=True)
        elif system == 'Windows':
            subprocess.run([str(installer), '/S', f'/D={installed}'], check=True, timeout=180)
            executables = [p for p in installed.rglob('*.exe') if p.name.lower() == 'permafrost.exe']
            assert len(executables) == 1, executables
            executable = executables[0]
        else:
            raise RuntimeError('Installer smoke runs on native Windows or macOS runners')
        evidence = run_smoke(executable, report, args.output)
        evidence['installer'] = {'name': installer.name, 'sha256': hashlib.sha256(installer.read_bytes()).hexdigest(), 'size': installer.stat().st_size}
        evidence['ciRun'] = os.environ.get('GITHUB_RUN_ID')
        evidence['commit'] = os.environ.get('GITHUB_SHA')
        if system == 'Windows':
            uninstallers = [p for p in installed.glob('*.exe') if p.name.lower().startswith('uninstall')]
            assert len(uninstallers) == 1, uninstallers
            subprocess.run([str(uninstallers[0]), '/S'], check=True, timeout=120)
            deadline = time.monotonic() + 30
            while executable.exists() and time.monotonic() < deadline:
                time.sleep(0.5)
            assert not executable.exists(), 'Uninstaller left the application executable installed'
            evidence['uninstallVerified'] = True
        else:
            shutil.rmtree(destination)
            evidence['uninstallVerified'] = not destination.exists()
        (args.output / f'installer-{system.lower()}-{platform.machine()}.json').write_text(json.dumps(evidence, indent=2))
        print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
