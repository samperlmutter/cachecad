"""Deterministic source assembly. Does not import or execute application code."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
STAMP = (2026, 1, 1, 0, 0, 0)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_source(root, relative):
    rel = PurePosixPath(relative)
    if rel.is_absolute() or '..' in rel.parts or '\\' in relative:
        raise ValueError('Unsafe source path')
    path = root / relative
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError('Symlink source rejected')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Source escapes repository')
    return path


def assemble(root, output):
    root, output = Path(root).absolute(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('Output must be a new directory')
    if not output.parent.is_dir():
        raise ValueError('Output parent must already exist')
    for part in (output.parent, *output.parent.parents):
        if part.is_symlink():
            raise ValueError('Symlink output parent rejected')
    for directory in ('recovered', 'maintained'):
        base = safe_source(root, directory)
        for path in base.rglob('*'):
            if path.is_symlink():
                raise ValueError('Symlink input rejected')
    recovery = json.loads(safe_source(root, 'recovered/recovery-manifest.json').read_text())
    patches = json.loads(safe_source(root, 'maintained/patch-manifest.json').read_text())
    baseline = patches['baseline_commit']
    if not re.fullmatch('[0-9a-f]{40}', baseline):
        raise ValueError('Invalid baseline commit')
    for entry in recovery['entries']:
        safe_source(root, entry['source'])
        safe_source(root, entry['report'])
    for entry in patches['modules']:
        safe_source(root, entry['baseline'])
        safe_source(root, entry['maintained'])
    for entry in patches.get('new_helpers', []):
        safe_source(root, entry['maintained'])
    # These are reviewed validation tools, never application modules.
    for tool in ('recovered/check_reports.py', 'recovered/check_entrypoint.py', 'tooling/maintained.py'):
        try:
            subprocess.run([sys.executable, str(safe_source(root, tool))], cwd=root, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except subprocess.CalledProcessError as error:
            raise ValueError(f'Assembly validation failed in {tool}:\n{error.stdout}\n{error.stderr}') from error
    overlay = {entry['module']: entry for entry in patches['modules']}
    files = {}
    records = []
    for entry in recovery['entries']:
        module = entry['module']
        original = entry['source']
        raw = safe_source(root, original).read_bytes()
        if sha(raw) != entry['source_sha256']:
            raise ValueError('Baseline source hash differs: ' + module)
        source = original
        origin = 'recovered'
        if module in overlay:
            source = overlay[module]['maintained']
            raw = safe_source(root, source).read_bytes()
            if sha(raw) != overlay[module]['maintained_sha256']:
                raise ValueError('Maintained payload hash differs: ' + module)
            origin = 'maintained'
        elif entry['status'] != 'exact_compiled_structure':
            if entry.get('sanitized_source_status') != 'sanitized_not_matching':
                raise ValueError('Unreviewed non-exact source')
            origin = 'sanitized'
        relative = PurePosixPath(original).relative_to('recovered')
        if relative.parts[0] not in {'app', 'core', 'gui', 'utils'} or relative.suffix != '.py':
            raise ValueError('Unexpected application module path')
        destination = 'src/' + relative.as_posix()
        if destination in files:
            raise ValueError('Duplicate assembly destination')
        files[destination] = raw
        records.append(dict(path=destination, sha256=sha(raw), origin=origin,
                            source=source, baseline_commit=baseline))
    if set(overlay) - {entry['module'] for entry in recovery['entries']}:
        raise ValueError('Overlay module absent from recovery baseline')
    for entry in patches.get('new_helpers', []):
        source = entry['maintained']
        raw = safe_source(root, source).read_bytes()
        if sha(raw) != entry['maintained_sha256']:
            raise ValueError('New helper payload hash differs: ' + entry['module'])
        relative = PurePosixPath(source).relative_to('maintained')
        if relative.parts[0] not in {'app', 'core', 'gui', 'utils'} or relative.suffix != '.py':
            raise ValueError('Unexpected helper module path')
        destination = 'src/' + relative.as_posix()
        if destination in files:
            raise ValueError('Helper collides with assembled source')
        files[destination] = raw
        records.append(dict(path=destination, sha256=sha(raw), origin='new-helper',
                            source=source, baseline_commit=baseline))
    raw = safe_source(root, 'recovered/entrypoint/main.py').read_bytes()
    entrypoint_report = json.loads(safe_source(root, 'recovered/entrypoint/comparison.json').read_text())
    if sha(raw) != entrypoint_report['source_sha256']:
        raise ValueError('Entrypoint payload hash differs')
    files['main.py'] = raw
    records.append(dict(path='main.py', sha256=sha(raw), origin='recovered-entrypoint',
                        source='recovered/entrypoint/main.py', baseline_commit=baseline))
    development_manifest = root / 'development/manifest.json'
    development_files = []
    if development_manifest.is_file():
        development_files = json.loads(safe_source(root, 'development/manifest.json').read_text())['files']
        for entry in development_files:
            source = entry['source']
            destination = entry['destination']
            if not (source.startswith('development/') or source.startswith('requirements/runtime-linux')):
                raise ValueError('Unapproved development artifact source')
            relative = PurePosixPath(destination)
            if relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'development':
                raise ValueError('Unsafe development artifact destination')
            raw = safe_source(root, source).read_bytes()
            if sha(raw) != entry['sha256'] or destination in files:
                raise ValueError('Development artifact hash/collision failure')
            files[destination] = raw
            records.append(dict(path=destination, sha256=sha(raw), origin='offline-development',
                                source=source, baseline_commit=baseline))
    manifest = dict(schema_version=1, artifact_kind='application source with optional offline development launcher; not an installer',
                    runtime_verified=False, baseline_commit=baseline,
                    missing_runtime_inputs=['production resources and branding', 'authorized account configuration', 'installed target runtime'],
                    offline_development=bool(development_files),
                    files=sorted(records, key=lambda record: record['path']))
    files['assembly-manifest.json'] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    output.mkdir()  # Atomic refusal if another process created the target.
    for name, raw in sorted(files.items()):
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(raw)
    # Manifest hashes all payload files; it cannot self-hash or hash its containing ZIP.
    with zipfile.ZipFile(output / 'app-source.zip', 'x', compression=zipfile.ZIP_STORED) as archive:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=STAMP)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    manifest = assemble(ROOT, args.output)
    print(f"Assembled {len(manifest['files'])} source files; runtime unverified: {args.output}")


if __name__ == '__main__':
    main()
