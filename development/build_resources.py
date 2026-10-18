"""Generate neutral development resources; no original asset bytes are used."""
import argparse
import binascii
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import zlib

# Public interface names referenced by recovered GUI code, not original artwork.
ALIASES = ('window.png', 'alert.png', 'bug.png', 'settings.png', 'folder.png',
           'drive.png', 'local.png', 'signin.png', 'internet.png', 'sponsor.png',
           'synced.png', 'refresh.png', 'options.png', 'loading.gif', 'loading2.gif',
           'plus.png', 'minus.png', 'revise.png', 'mismatch.png', 'nosync.png')


def png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', binascii.crc32(kind + data) & 0xffffffff)
    # Neutral checkerboard, authored specifically for this development fallback.
    rows = b''.join(b'\0' + b''.join(bytes((100, 140, 180, 255) if (x // 4 + y // 4) % 2 else (50, 60, 70, 255))
                                   for x in range(24)) for y in range(24))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 24, 24, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b''))


def build(rcc, output):
    output = Path(output)
    if output.exists():
        raise ValueError('Resource output must not already exist')
    with tempfile.TemporaryDirectory(prefix='cachecad-dev-resources-') as directory:
        root = Path(directory)
        (root / 'placeholder.png').write_bytes(png())
        # One-frame, two-color GIF authored directly from the GIF89a encoding.
        (root / 'placeholder.gif').write_bytes(bytes.fromhex(
            '47494638396101000100800000648cb4323c462c00000000010001000002024401003b'))
        entries = ''.join(f'<file alias="{name}">placeholder.{"gif" if name.endswith(".gif") else "png"}</file>' for name in ALIASES)
        (root / 'development.qrc').write_text('<RCC><qresource prefix="/assets">' + entries + '</qresource></RCC>')
        for resource in root.iterdir():
            os.utime(resource, (946684800, 946684800))
        subprocess.run([str(rcc), '-g', 'python', 'development.qrc', '-o', 'resources_rc.py'], cwd=root, check=True)
        raw = (root / 'resources_rc.py').read_bytes()
    output.write_bytes(raw)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rcc', required=True, type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    build(args.rcc, args.output)
