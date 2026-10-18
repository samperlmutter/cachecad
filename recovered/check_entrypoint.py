"""Compile-only check of original CArchive entrypoint, separate from49PYZmodules."""
import argparse
import hashlib
import json
import marshal
from pathlib import Path
import sys
from compare_bytecode import compare, structural_fingerprint

EXPECTED = 'c8cced824d84a988077bb3da39b0cb0e67a7b0246c4c872e92452b9cb297d5f0'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, help='Optional extracted CArchive main marshal file')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error('Python 3.12 required')
    root = Path(__file__).resolve().parent / 'entrypoint'
    report = json.loads((root / 'comparison.json').read_text())
    source = (root / 'main.py').read_bytes()
    candidate = compile(source, 'main.py', 'exec', dont_inherit=True, optimize=0)
    if report['source_sha256'] != hashlib.sha256(source).hexdigest():
        raise SystemExit('Entrypoint source hash differs')
    if report['original_marshaled_sha256'] != EXPECTED:
        raise SystemExit('Entrypoint original hash differs')
    if report['original_structure_sha256'] != structural_fingerprint(candidate):
        raise SystemExit('Entrypoint compiled structure differs')
    if args.original:
        raw = args.original.read_bytes()
        if hashlib.sha256(raw).hexdigest() != EXPECTED:
            raise SystemExit('Original entrypoint bytes differ from pinned release')
        original = marshal.loads(raw)
        differences, count = compare(original, candidate)
        if differences or count != 2:
            raise SystemExit('Original entrypoint comparison failed')
        print('Entrypoint: original bytes revalidated; 2 code objects match')
    else:
        print('Entrypoint: source-only fingerprint verified (2 code objects)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
