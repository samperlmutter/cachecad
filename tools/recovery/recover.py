#!/usr/bin/env python3
"""Static PyInstaller CArchive extraction. Never imports or executes target code."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
import zlib

MAGIC = b'MEI\014\013\012\013\016'
COOKIE = struct.Struct('!8sIIII64s')
ENTRY = struct.Struct('!IIIIBc')
INSTALLER_SHA256 = '3d9da1785b1ba3288a60cf0771f7b0189037957947e25348f4fe2bf783d431bd'
APP_SHA256 = '38ef57dd6dacd33c2e831bd380ca2a07756510248debc32d1722a73df8779ce6'
MAX_INPUT = 256 * 1024 * 1024
MAX_MEMBER = 256 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_ENTRIES = 10000


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    name = name.replace('\\', '/')
    parts = name.split('/')
    if (not name or name.startswith('/') or any(p in ('', '.', '..') for p in parts)
            or any(any(c in '<>:\"|?*' or ord(c) < 32 or ord(c) == 127 for c in p) for p in parts)
            or any(p.endswith((' ', '.')) for p in parts)
            or any(re.fullmatch(r'(?i)(con|prn|aux|nul|clock\$|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])',
                                p.split('.', 1)[0].rstrip(' ')) for p in parts)):
        raise ValueError('Unsafe archive member path: ' + repr(name))
    return str(PurePosixPath(name))


def parse_archive(blob):
    """Validate the complete archive before any output; return inert byte records."""
    if len(blob) > MAX_INPUT:
        raise ValueError('Input exceeds size limit')
    cookie = blob.rfind(MAGIC)
    if cookie < 0 or cookie + COOKIE.size > len(blob):
        raise ValueError('Missing or truncated PyInstaller cookie')
    _, total, offset, length, version, library = COOKIE.unpack_from(blob, cookie)
    base = cookie + COOKIE.size - total
    if base < 0 or total < COOKIE.size or offset + length > cookie - base:
        raise ValueError('Invalid archive bounds')
    pos, end = base + offset, base + offset + length
    rows, names, expanded = [], set(), 0
    while pos < end:
        if len(rows) >= MAX_ENTRIES or end - pos < ENTRY.size:
            raise ValueError('Invalid or excessive table entries')
        size, off, stored, raw, flag, kind = ENTRY.unpack_from(blob, pos)
        if size <= ENTRY.size or pos + size > end or flag not in (0, 1):
            raise ValueError('Invalid table entry')
        name_bytes = blob[pos + ENTRY.size:pos + size]
        if b'\0' not in name_bytes:
            raise ValueError('Unterminated member name')
        name = safe_name(name_bytes.split(b'\0', 1)[0].decode('utf-8'))
        key = name.casefold()
        if key in names or key == 'manifest.json':
            raise ValueError('Duplicate/reserved output name')
        # Reject links and unknown types rather than interpreting their payloads.
        if kind not in (b'b', b's', b'm', b'M', b'o', b'z', b'x', b'Z', b'd'):
            raise ValueError('Unsupported archive member type')
        if raw > MAX_MEMBER or expanded + raw > MAX_TOTAL or off + stored > offset:
            raise ValueError('Member bounds or size limit exceeded')
        data = blob[base + off:base + off + stored]
        if flag:
            decoder = zlib.decompressobj()
            data = decoder.decompress(data, raw + 1)
            if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ValueError('Invalid or over-budget compressed member')
        if len(data) != raw:
            raise ValueError('Member size mismatch')
        names.add(key)
        expanded += raw
        rows.append({'name': name, 'type': kind.decode('ascii'), 'size': raw,
                     'compressed_size': stored, 'offset': base + off,
                     'sha256': digest(data), 'data': data})
        pos += size
    for name in names:
        if any(str(p).casefold() in names for p in PurePosixPath(name).parents if str(p) != '.'):
            raise ValueError('File/directory member collision')
    return {'python_version': version,
            'python_library': library.split(b'\0', 1)[0].decode('utf-8'),
            'items': sorted(rows, key=lambda r: r['name'])}


def extract(source, destination, expected_sha256=APP_SHA256):
    source, destination = Path(source), Path(destination)
    if source.stat().st_size > MAX_INPUT:
        raise ValueError('Input exceeds size limit')
    blob = source.read_bytes()
    if digest(blob) != expected_sha256.lower():
        raise ValueError('SHA256 mismatch; no extraction performed')
    parsed = parse_archive(blob)
    # Caller must supply a new directory under a trusted parent. Never reuse trees
    # that could contain symlinks; mkdir(exist_ok=False) also rejects a root link.
    destination.mkdir(parents=False, exist_ok=False)
    for row in parsed['items']:
        path = destination / row['name']
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as f:
            f.write(row.pop('data'))
    manifest = {'schema_version': 1, 'source_sha256': digest(blob),
                'source_size': len(blob), 'target_execution': False,
                'content_kind': 'Recovered packaged bytes, not original source', **parsed}
    (destination / 'manifest.json').write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    verify = sub.add_parser('verify-installer')
    verify.add_argument('path', type=Path)
    unpack = sub.add_parser('extract-pyinstaller')
    unpack.add_argument('path', type=Path)
    unpack.add_argument('output', type=Path)
    unpack.add_argument('--expected-sha256', default=APP_SHA256)
    args = p.parse_args()
    if args.command == 'verify-installer':
        if args.path.stat().st_size > MAX_INPUT:
            raise ValueError('Input exceeds size limit')
        actual = digest(args.path.read_bytes())
        if actual != INSTALLER_SHA256:
            raise ValueError('Installer SHA256 mismatch')
        print(json.dumps({'sha256': actual, 'verified': True}, sort_keys=True))
    else:
        result = extract(args.path, args.output, args.expected_sha256)
        print(json.dumps({'source_sha256': result['source_sha256'],
                          'entries': len(result['items'])}, sort_keys=True))


if __name__ == '__main__':
    main()
