#!/usr/bin/env python3
"""Build two isolated versions, reject tampering, install a signed native upgrade.

Uses a throwaway signing key and application identifier. The loopback HTTP option
and native automation hook are compiled only into these disposable QA builds.
The production checkout, distributor credentials and signing key are never used.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time


def run(command, cwd, env):
    subprocess.run(command, cwd=cwd, env=env, check=True)


def wait_for(path, timeout=150):
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    if not path.exists():
        raise RuntimeError(f'Timed out waiting for {path.name}')
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--debug', action='store_true', help='Use faster local debug builds')
    args = parser.parse_args()
    system = platform.system()
    if system not in ('Darwin', 'Windows'):
        raise RuntimeError('Run upgrade verification on native macOS or Windows')
    source = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    node = shutil.which('node')
    cli = source / 'node_modules/@tauri-apps/cli/tauri.js'
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PERMAFROST_', 'TAURI_SIGNING_', 'APPLE_'))}
    # CI's production installer uses release/, while QA uses debug/. Reuse its
    # tested dependencies there. Local runs keep the user's debug app untouched.
    env['CARGO_TARGET_DIR'] = str(source / ('target' if os.environ.get('CI') else 'target/upgrade-qa'))
    with tempfile.TemporaryDirectory(prefix='permafrost-upgrade-') as tmp:
        root = Path(tmp).resolve()
        checkout = root / 'source'
        checkout.mkdir()
        for name in ('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'package.json', 'package-lock.json'):
            shutil.copy2(source / name, checkout / name)
        for name in ('src-tauri', 'crates', 'desktop'):
            shutil.copytree(source / name, checkout / name, ignore=shutil.ignore_patterns('node_modules', 'dist', 'gen', 'occt'))
        # Junctions work on unprivileged Windows CI; local Unix builds use a symlink.
        if system == 'Windows':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(checkout / 'node_modules'), str(source / 'node_modules')], check=True)
        else:
            (checkout / 'node_modules').symlink_to(source / 'node_modules', target_is_directory=True)
        key = root / 'qa.key'
        # Tauri prints generated keys; keep that output in the ephemeral directory.
        with (root / 'key-generation.log').open('w') as log:
            subprocess.run([node, str(cli), 'signer', 'generate', '--ci', '-w', str(key)], cwd=checkout, env=env, check=True, stdout=log, stderr=log)
        env['TAURI_SIGNING_PRIVATE_KEY'] = str(key)
        env['TAURI_SIGNING_PRIVATE_KEY_PASSWORD'] = ''
        config_path = checkout / 'src-tauri/tauri.conf.json'
        config = json.loads(config_path.read_text())
        config['productName'] = 'Permafrost Upgrade QA'
        config['identifier'] = 'com.permafrost.upgradeqa'
        config['bundle']['createUpdaterArtifacts'] = True
        config['plugins']['updater'] = {
            'pubkey': key.with_suffix('.key.pub').read_text().strip(),
            'endpoints': ['http://127.0.0.1:1/latest.json'],
            'dangerousInsecureTransportProtocol': True,
            'requireSignedVersion': True,
            'windows': {'installMode': 'quiet'},
        }
        manifest = (checkout / 'Cargo.toml').read_text()
        bundles = 'app' if system == 'Darwin' else 'nsis'
        profile = 'debug' if args.debug else 'release'
        bundle = Path(env['CARGO_TARGET_DIR']) / profile / 'bundle'
        installed = root / 'installed'
        installed.mkdir()
        executable = None
        old_hash = None
        for version in ('0.0.1', '0.0.2'):
            config['version'] = version
            config_path.write_text(json.dumps(config, indent=2))
            (checkout / 'Cargo.toml').write_text(re.sub(r'version = "[^"]+"', f'version = "{version}"', manifest, count=1))
            command = [node, str(cli), 'build', '--features', 'updater-qa', '--bundles', bundles, '--ci']
            if args.debug:
                command.append('--debug')
            run(command, checkout, env)
            if version == '0.0.1':
                if system == 'Darwin':
                    app = next((bundle / 'macos').glob('*.app'))
                    destination = installed / app.name
                    shutil.copytree(app, destination, symlinks=True)
                    executable = destination / 'Contents/MacOS/permafrost'
                else:
                    installer = next((bundle / 'nsis').glob('*0.0.1*-setup.exe'))
                    run([str(installer), '/S', f'/D={installed}'], checkout, env)
                    executable = installed / 'permafrost.exe'
                old_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
        pattern = '*.app.tar.gz' if system == 'Darwin' else '*0.0.2*-setup.exe'
        updates = list(bundle.rglob(pattern))
        if len(updates) != 1:
            raise RuntimeError(f'Expected one upgrade package, found {updates}')
        artifact = updates[0].read_bytes()
        signature = Path(str(updates[0]) + '.sig').read_text().strip()
        requests = []
        download_count = 0

        class Server(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                nonlocal download_count
                requests.append(self.path)
                if self.path == '/latest.json':
                    body = json.dumps({'version': '0.0.2', 'url': f'http://127.0.0.1:{self.server.server_port}/update', 'signature': signature}).encode()
                elif self.path == '/update':
                    download_count += 1
                    body = artifact if download_count > 1 else artifact[:-1] + bytes([artifact[-1] ^ 1])
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        report = root / 'upgraded-render.json'
        receipt = root / 'upgrade-receipt.json'
        qa = root / 'qa.json'
        qa.write_text(json.dumps({'endpoint': f'http://127.0.0.1:{server.server_port}/latest.json', 'targetVersion': '0.0.2', 'receipt': str(receipt)}))
        process = subprocess.Popen([str(executable), '--smoke-output', str(report), '--update-qa', str(qa)], env=env)
        try:
            evidence = wait_for(receipt)
            if 'error' in evidence:
                raise RuntimeError(evidence['error'])
            rendered = wait_for(report)
            process.wait(timeout=20)
            assert process.returncode == 0, process.returncode
            assert rendered['version'] == '0.0.2', rendered
            assert rendered['ui']['workspaceVisible'] and rendered['ui']['modelRendered'], rendered
            assert Path(rendered['installedExecutable']).resolve() == executable.resolve(), rendered
            new_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
            assert old_hash != new_hash, 'The installed executable was not replaced'
            assert evidence['tamperedPackageRejected'] and evidence['validSignatureVerified'], evidence
            assert download_count == 2, requests
            if system == 'Windows':
                uninstaller = next(installed.glob('uninstall*.exe'))
                run([str(uninstaller), '/S'], checkout, env)
                deadline = time.monotonic() + 30
                while executable.exists() and time.monotonic() < deadline:
                    time.sleep(0.5)
                assert not executable.exists(), 'Uninstaller left upgraded app installed'
            else:
                subprocess.run(['codesign', '--verify', '--deep', '--strict', str(destination)], check=True)
                shutil.rmtree(destination)
            evidence.update({'schema': 1, 'platform': system, 'arch': platform.machine(), 'oldExecutableSha256': old_hash, 'newExecutableSha256': new_hash, 'restartedNativeApp': rendered, 'uninstallVerified': not executable.exists(), 'ciRun': os.environ.get('GITHUB_RUN_ID'), 'commit': os.environ.get('GITHUB_SHA')})
            (output / f'upgrade-{system.lower()}-{platform.machine()}.json').write_text(json.dumps(evidence, indent=2))
            print(json.dumps(evidence, indent=2))
        finally:
            server.shutdown()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)


if __name__ == '__main__':
    main()
