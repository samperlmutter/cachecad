import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

spec = importlib.util.spec_from_file_location('recovery', Path(__file__).parents[1] / 'recover.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def archive(entries):
    payload, toc = b'', b''
    for name, data, compressed, kind in entries:
        stored = zlib.compress(data) if compressed else data
        name = name.encode() + b'\0'
        toc += r.ENTRY.pack(r.ENTRY.size + len(name), len(payload), len(stored),
                            len(data), int(compressed), kind) + name
        payload += stored
    return payload + toc + r.COOKIE.pack(r.MAGIC, len(payload) + len(toc) + r.COOKIE.size,
                                        len(payload), len(toc), 312, b'python312.dll')


class RecoveryTests(unittest.TestCase):
    def test_roundtrip_and_determinism(self):
        blob = archive([('folder/module', b'inert bytes', True, b's'), ('data', b'abc', False, b'x')])
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'input'; source.write_bytes(blob)
            a, b = Path(d) / 'one', Path(d) / 'two'
            r.extract(source, a, r.digest(blob)); r.extract(source, b, r.digest(blob))
            self.assertEqual((a / 'manifest.json').read_bytes(), (b / 'manifest.json').read_bytes())
            self.assertEqual((a / 'folder/module').read_bytes(), b'inert bytes')
            self.assertFalse(json.loads((a / 'manifest.json').read_text())['target_execution'])

    def test_traversal_and_windows_names(self):
        for name in ['../escape', '/absolute', 'C:\\evil', 'a/../x', 'a//x', 'a/./x', 'NUL.txt', 'a.']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                r.parse_archive(archive([(name, b'x', False, b'x')]))

    def test_windows_invalid_characters_and_device_aliases(self):
        for name in ['a<b', 'a>b', 'a"b', 'a|b', 'a?b', 'a*b',
                     'a:stream', 'folder/COM¹.txt', 'LPT²', 'COM³.log',
                     'CONIN$', 'conout$.txt', 'CLOCK$', 'CON .txt',
                     'aux...txt', 'folder/NUL.data', 'x\x7fy']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                r.parse_archive(archive([(name, b'x', False, b'x')]))
        for name in ['COM10.txt', 'computer.txt', 'normal-file_1.txt']:
            self.assertEqual(r.safe_name(name), name)

    def test_duplicate_collision_reserved_and_links(self):
        for names in [('a', 'A'), ('a', 'a/b'), ('manifest.json',)]:
            with self.subTest(names=names), self.assertRaises(ValueError):
                r.parse_archive(archive([(n, b'x', False, b'x') for n in names]))
        with self.assertRaises(ValueError):
            r.parse_archive(archive([('link', b'../target', False, b'n')]))

    def test_bad_hash_creates_no_output(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'source'; src.write_bytes(archive([('x', b'x', False, b'x')]))
            out = Path(d) / 'out'
            with self.assertRaises(ValueError): r.extract(src, out, '0' * 64)
            self.assertFalse(out.exists())

    def test_existing_output_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'source'; blob = archive([('x', b'x', False, b'x')]); src.write_bytes(blob)
            out = Path(d) / 'out'; out.mkdir()
            link = Path(d) / 'link'; link.symlink_to(out, target_is_directory=True)
            for path in (out, link):
                with self.assertRaises(FileExistsError): r.extract(src, path, r.digest(blob))
            self.assertEqual(list(out.iterdir()), [])

    def test_corrupt_bounds_and_truncated_cookie(self):
        blob = archive([('x', b'x', False, b'x')])
        with self.assertRaises(ValueError): r.parse_archive(blob[:-1])
        bad = bytearray(blob)
        struct.pack_into('!I', bad, len(blob) - r.COOKIE.size + 12, len(blob) * 2)
        with self.assertRaises(ValueError): r.parse_archive(bytes(bad))

    def test_compression_budget(self):
        blob = bytearray(archive([('x', b'x' * 1000, True, b'x')]))
        cookie = len(blob) - r.COOKIE.size
        _, _, off, _, _, _ = r.COOKIE.unpack_from(blob, cookie)
        struct.pack_into('!I', blob, off + 12, 1)
        with self.assertRaises(ValueError): r.parse_archive(bytes(blob))


if __name__ == '__main__': unittest.main()
