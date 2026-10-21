#!/usr/bin/env python3
"""Stage a flat, clearly named beta download without renaming updater assets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil


PLATFORMS = {
    'windows-x64': ('Windows-x64', '*-setup.exe', 'Setup.exe'),
    'macos-arm64': ('macOS-Apple-Silicon', '*.dmg', 'Install.dmg'),
    'macos-x64': ('macOS-Intel', '*.dmg', 'Install.dmg'),
}


def prepare(bundle, output, platform, config):
    version = config['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?', version):
        raise ValueError('Expected a safe application version')
    label, pattern, suffix = PLATFORMS[platform]
    installers = list(bundle.rglob(pattern))
    if len(installers) != 1:
        raise RuntimeError(f'Expected one {pattern} installer, found {installers}')
    output.mkdir(parents=True, exist_ok=False)
    name = f'Permafrost-{version}-{label}'
    destination = output / f'{name}-{suffix}'
    shutil.copy2(installers[0], destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    (output / 'SHA256SUMS.txt').write_text(f'{digest}  {destination.name}\n', encoding='utf-8')
    if platform == 'windows-x64':
        instructions = (
            f'1. Double-click {destination.name} and complete setup.\n'
            '2. Leave Run Permafrost and Create desktop shortcut selected on the final page.\n'
            '3. Open Start and search for Permafrost, or use the Permafrost desktop shortcut.\n'
            'If you chose a custom install directory, the shortcuts still open that installation.\n'
            'The default program is %LOCALAPPDATA%\\Permafrost\\permafrost.exe.\n'
            'If you closed the window, Permafrost may still be in the system tray;\n'
            'open its tray menu or use the Start shortcut to show the window again.\n'
        )
    else:
        instructions = (
            f'1. Open {destination.name}.\n'
            '2. Drag Permafrost into Applications.\n'
            '3. Open Applications and launch Permafrost.\n'
        )
    (output / 'START-HERE.txt').write_text(
        f'Permafrost {version} — {label}\n\n'
        'This is the current Permafrost desktop beta.\n'
        'Artifacts labeled Legacy-CacheCAD are the older Python/Qt application.\n\n'
        + instructions
        + '\nThis development build is not production code-signed/notarized.\n'
        + 'Keep your operating system security settings enabled.\n'
        + 'SHA256SUMS.txt contains the checksum of the installer in this download.\n',
        encoding='utf-8',
    )
    return f'{name}-Installer'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--platform', choices=PLATFORMS, required=True)
    args = parser.parse_args()
    config = json.loads(Path('src-tauri/tauri.conf.json').read_text())
    name = prepare(args.bundle, args.output, args.platform, config)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write(f'artifact_name={name}\n')
    print(name)


if __name__ == '__main__':
    main()
