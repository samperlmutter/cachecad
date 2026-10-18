"""Compile-only source gate against the complete reviewed recovery manifest.

No recovered module imports or runtime dependencies. Full evidentiary revalidation
uses compare_bytecode.py with the pinned original PYZ, kept outside this repo.
"""
import hashlib
import json
from pathlib import Path
import sys
from compare_bytecode import structural_fingerprint, source_path


def validate(root):
    manifest = json.loads((root / 'recovery-manifest.json').read_text())
    entries = manifest['entries']
    if len(entries) != 49 or len({e['module'] for e in entries}) != 49:
        raise ValueError('Manifest must cover all 49 distinct original modules')
    expected = {e['module']: e for e in entries if e['status'] == 'exact_compiled_structure'}
    if not expected:
        raise ValueError('Manifest contains no exact recovered modules')
    reports = {}
    for path in sorted((root / 'reports').glob('*.json')):
        report = json.loads(path.read_text())
        if report.get('match'):
            module = report['module']
            if module in reports:
                raise ValueError('Duplicate exact module report: ' + module)
            reports[module] = (path, report)
    if set(reports) != set(expected):
        raise ValueError('Exact report set differs from reviewed manifest')
    failures = []
    for module, entry in expected.items():
        path, report = reports[module]
        source = source_path(root, module)
        raw = source.read_bytes()
        code = compile(raw, str(source), 'exec', dont_inherit=True, optimize=0)
        if hashlib.sha256(raw).hexdigest() != entry['source_sha256'] or entry['source_sha256'] != report['source_sha256']:
            failures.append(module + ': source hash changed')
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry['report_sha256']:
            failures.append(module + ': report hash changed')
        original = entry['original_structure_sha256']
        if structural_fingerprint(code) != original or original != report['original_structure_sha256']:
            failures.append(module + ': compiled structure differs')
        if report['code_objects_compared'] != entry['original_code_objects']:
            failures.append(module + ': code-object coverage differs')
    return len(expected), failures


def main():
    if sys.version_info[:2] != (3, 12):
        raise SystemExit('Python 3.12 required')
    try:
        count, failures = validate(Path(__file__).resolve().parent)
    except (OSError, ValueError, KeyError) as error:
        print('Recovery gate failed:', error)
        return 1
    print(f'Verified {count} recovered modules against reviewed original structure hashes')
    for failure in failures:
        print(failure)
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
