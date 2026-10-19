"""Independent negative review of Linux archive and install boundaries.

All payloads are inert synthetic bytes in disposable directories. No artifact or
archive member is ever executed by these tests.
"""
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'packaging/linux/linux_support.py').is_file():
    raise unittest.SkipTest('Maintained Linux packaging absent from scaffold wheel')
SPEC = importlib.util.spec_from_file_location(
    '_cachecad_linux_distribution_review', ROOT / 'packaging/linux/linux_support.py')
support = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(support)


def member(name, kind=tarfile.REGTYPE, target='', mode=0o644, data=b'fixture'):
    entry = tarfile.TarInfo(name)
    entry.type, entry.linkname, entry.mode = kind, target, mode
    entry.size = len(data) if entry.isfile() else 0
    return entry, data


def archive_fixture(path, extras=(), *, reviewed_source=False):
    entries = [member('CacheCAD', tarfile.DIRTYPE, mode=0o755),
               member('CacheCAD/CacheCAD', mode=0o755)]
    if reviewed_source:
        import hashlib
        raw = b'# Inert review fixture, never executed.\n'
        names = support._shared.REQUIRED_SOURCE
        manifest = {'files': [{'path': name, 'sha256': hashlib.sha256(raw).hexdigest(),
                               'origin': 'maintained'} for name in names]}
        entries += [member('CacheCAD/_internal/app-source/' + name, data=raw) for name in names]
    else:
        manifest = {'files': []}
    entries.append(member('CacheCAD/_internal/app-source/assembly-manifest.json',
                          data=json.dumps(manifest).encode()))
    entries.extend(extras)
    with tarfile.open(path, 'w:gz') as output:
        for entry, raw in entries:
            output.addfile(entry, io.BytesIO(raw) if entry.isfile() else None)
    return path


class LinuxArchiveBoundaryReview(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temporary)

    def assert_rejected(self, extras):
        path = archive_fixture(self.root / 'untrusted.tar.gz', extras)
        with self.assertRaises((ValueError, tarfile.TarError)):
            support.validate_archive(path)

    def test_rejects_traversal_absolute_noncanonical_and_foreign_roots(self):
        for name in ('CacheCAD/../outside', '/CacheCAD/evil', 'Other/file',
                     'CacheCAD//same', 'CacheCAD/./same', 'CacheCAD\\evil', 'CacheCAD/C:evil'):
            with self.subTest(name=name):
                self.assert_rejected([member(name)])

    def test_rejects_duplicates_and_children_of_links_or_regular_files(self):
        for extras in ([member('CacheCAD/CacheCAD')],
                       [member('CacheCAD/parent'), member('CacheCAD/parent/file')],
                       [member('CacheCAD/parent', tarfile.SYMTYPE, 'safe'),
                        member('CacheCAD/parent/file')]):
            with self.subTest(extras=[entry.name for entry, _ in extras]):
                self.assert_rejected(extras)

    def test_rejects_hardlinks_devices_fifos_and_privileged_modes(self):
        for kind in (tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                self.assert_rejected([member('CacheCAD/unsafe', kind, 'CacheCAD/CacheCAD')])
        for mode in (0o4755, 0o2755, 0o1755):
            with self.subTest(mode=mode):
                self.assert_rejected([member('CacheCAD/unsafe', mode=mode)])

    def test_rejects_external_symlinks(self):
        for target in ('/etc/passwd', '../outside', '../../outside', 'C:outside', '..\\outside'):
            with self.subTest(target=target):
                self.assert_rejected([member('CacheCAD/link', tarfile.SYMTYPE, target)])

    def test_rejects_dangling_and_cyclic_symlinks_before_extraction(self):
        for extras in ([member('CacheCAD/link', tarfile.SYMTYPE, 'absent')],
                       [member('CacheCAD/a', tarfile.SYMTYPE, 'b'),
                        member('CacheCAD/b', tarfile.SYMTYPE, 'a')],
                       [member('CacheCAD/link', tarfile.SYMTYPE, '.')]):
            with self.subTest(extras=[entry.linkname for entry, _ in extras]):
                self.assert_rejected(extras)

    def test_accepts_safe_internal_file_symlink(self):
        path = archive_fixture(self.root / 'valid.tar.gz',
                               [member('CacheCAD/link', tarfile.SYMTYPE, 'CacheCAD')])
        self.assertEqual(support.validate_archive(path), 4)

    def test_extraction_rejects_existing_destination_without_modification(self):
        path = archive_fixture(self.root / 'valid.tar.gz', reviewed_source=True)
        destination = self.root / 'existing'
        destination.mkdir()
        sentinel = destination / 'sentinel'
        sentinel.write_bytes(b'keep')
        with self.assertRaises((ValueError, FileExistsError)):
            support.extract_archive(path, destination)
        self.assertEqual(list(destination.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b'keep')

    def test_extraction_rejects_linked_parent_before_writing_outside_target(self):
        path = archive_fixture(self.root / 'valid.tar.gz', reviewed_source=True)
        outside = self.root / 'outside'
        outside.mkdir()
        linked_parent = self.root / 'linked-parent'
        linked_parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            support.extract_archive(path, linked_parent / 'new-install')
        self.assertEqual(list(outside.iterdir()), [])

    def test_inert_reviewed_source_extracts_in_new_directory(self):
        path = archive_fixture(self.root / 'valid.tar.gz', reviewed_source=True)
        bundle = support.extract_archive(path, self.root / 'new-install')
        self.assertEqual(bundle, self.root / 'new-install' / 'CacheCAD')
        self.assertTrue((bundle / 'CacheCAD').stat().st_mode & 0o111)
        self.assertTrue((bundle / '_internal/app-source/application/launch.py').is_file())


if __name__ == '__main__':
    unittest.main()
