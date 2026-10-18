"""Compile recovered source and compare code objects; never execute either program.

Only read the pinned, locally extracted CacheCAD PYZ. Python marshal is not a
safe parser for arbitrary untrusted input; hash verification precedes parsing.
"""
import argparse
import hashlib
import json
import marshal
from pathlib import Path
import struct
import sys
import types
import zlib

FIELDS = ('co_code', 'co_argcount', 'co_posonlyargcount', 'co_kwonlyargcount',
          'co_nlocals', 'co_stacksize', 'co_flags', 'co_names', 'co_varnames',
          'co_freevars', 'co_cellvars', 'co_name', 'co_qualname', 'co_exceptiontable')


def source_path(root, module):
    base = root.joinpath(*module.split('.'))
    module_file = base.with_suffix('.py')
    package_file = base / '__init__.py'
    if module_file.is_file() and package_file.is_file():
        raise ValueError('Ambiguous module and package sources: ' + module)
    return package_file if package_file.is_file() else module_file


def structural_fingerprint(code):
    """Hash every compared field and nested constant, excluding source locations."""
    values = {}
    for field in FIELDS:
        value = getattr(code, field)
        values[field] = value.hex() if isinstance(value, bytes) else value
    values['co_consts'] = [
        {'code': structural_fingerprint(value)} if isinstance(value, types.CodeType)
        else {'marshal': marshal.dumps(value).hex()}
        for value in code.co_consts
    ]
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def compare(original, candidate, path='<module>'):
    differences = []
    count = 1
    for field in FIELDS:
        if getattr(original, field) != getattr(candidate, field):
            differences.append(f'{path}.{field}')
    if len(original.co_consts) != len(candidate.co_consts):
        differences.append(f'{path}.co_consts.length')
    for index, (a, b) in enumerate(zip(original.co_consts, candidate.co_consts)):
        label = f'{path}.co_consts[{index}]'
        if type(a) is not type(b):
            differences.append(label + '.type')
        elif isinstance(a, types.CodeType):
            child_diff, child_count = compare(a, b, a.co_qualname)
            differences.extend(child_diff)
            count += child_count
        elif marshal.dumps(a) != marshal.dumps(b):
            differences.append(label)
    return differences, count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pyz', type=Path, required=True)
    parser.add_argument('modules', nargs='+')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error('Python 3.12 is required')
    data = args.pyz.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    expected = '5482479f417d650d4a3be34def973534b172902baceeed48f9bc877ebb883ea2'
    if digest != expected:
        parser.error('PYZ SHA256 differs from pinned analyzed release')
    toc = dict(marshal.loads(data[struct.unpack('!I', data[8:12])[0]:]))
    root = Path(__file__).resolve().parent
    failed = False
    for module in args.modules:
        source = source_path(root, module)
        _, pos, size = toc[module]
        original = marshal.loads(zlib.decompress(data[pos:pos + size]))
        candidate = compile(source.read_bytes(), original.co_filename, 'exec', dont_inherit=True, optimize=0)
        differences, count = compare(original, candidate)
        report = {
            'module': module, 'method': 'compile-only recursive code-object comparison',
            'python': sys.version, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'pyz_sha256': digest,
            'original_marshaled_module_sha256': hashlib.sha256(zlib.decompress(data[pos:pos + size])).hexdigest(),
            'code_objects_compared': count,
            'compared_fields': list(FIELDS) + ['co_consts recursively, including exact constant types'],
            'excluded_fields': ['co_filename', 'co_firstlineno', 'co_linetable'],
            'original_structure_sha256': structural_fingerprint(original),
            'candidate_structure_sha256': structural_fingerprint(candidate),
            'match': not differences, 'differences': differences,
            'original_executed': False, 'candidate_executed': False,
            'limitation': 'Matching compiler structure does not verify dependencies, runtime integration, or source formatting/comments.'
        }
        (root / 'reports' / (module + '.json')).write_text(json.dumps(report, indent=2) + '\n')
        print(module, 'MATCH' if not differences else 'MISMATCH', count, differences)
        failed |= bool(differences)
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
