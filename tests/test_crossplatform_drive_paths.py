"""Real maintained path conversion; synthetic metadata only, no app or services."""
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from recovered_harness import ROOT, deny_external_effects, load_function


DRIVE = 'maintained/core/drive_client.py'


def item(name, identifier='drawing', parent='root', *, folder=False):
    return {'id': identifier, 'name': name, 'parents': [parent], 'folder': folder}


class ConvertedItem:
    @staticmethod
    def from_drive_item(record, full_path):
        return SimpleNamespace(path=full_path, id=record['id'], name=record['name'],
                               is_folder=record['folder'])


@unittest.skipUnless((ROOT / DRIVE).is_file(), 'Maintained source absent from wheel fixture')
class CrossPlatformDrivePathTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())

    def convert(self, folders, files, path_type=Path):
        errors = []
        method = load_function(DRIVE, 'DriveClient.build_drive_files', {
            'Path': path_type, 'PureWindowsPath': PureWindowsPath, 're': re,
            'DriveItemInfo': ConvertedItem, 'logging': Mock(),
            'print': lambda *args: None,
        })
        result = method(None, folders, files, 'root', report_error=errors.append)
        return result, errors

    def nested_metadata(self):
        folders = [item('Assembly', 'assembly', folder=True),
                   item('Parts', 'parts', 'assembly', folder=True)]
        files = [item('bracket.step', parent='parts')]
        return folders, files

    def test_nested_paths_match_native_local_keys_on_both_platforms(self):
        for path_type in (PurePosixPath, PureWindowsPath):
            with self.subTest(path_type=path_type.__name__):
                converted, errors = self.convert(*self.nested_metadata(), path_type)
                expected = str(path_type('Assembly', 'Parts', 'bracket.step'))
                self.assertEqual(converted[0].path, expected)
                self.assertEqual(errors, [])
                parsed = path_type(converted[0].path)
                self.assertEqual(parsed.parts, ('Assembly', 'Parts', 'bracket.step'))
                # This is the same native Path split used by SyncAction.
                self.assertEqual(parsed.name, 'bracket.step')
                self.assertEqual(str(parsed.parent), str(path_type('Assembly', 'Parts')))

    def test_native_relative_path_selects_actual_nested_file(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        local = root / 'Assembly' / 'Parts' / 'bracket.step'
        local.parent.mkdir(parents=True)
        local.write_text('disposable CAD fixture', encoding='utf-8')
        converted, _ = self.convert(*self.nested_metadata())
        self.assertEqual(converted[0].path, str(local.relative_to(root)))
        self.assertEqual(root / converted[0].path, local)
        self.assertEqual((root / converted[0].path).read_text(), 'disposable CAD fixture')

    def test_single_component_keeps_existing_path(self):
        for path_type in (PurePosixPath, PureWindowsPath):
            converted, errors = self.convert([], [item('bracket.step')], path_type)
            self.assertEqual([entry.path for entry in converted], ['bracket.step'])
            self.assertEqual(errors, [])

    def test_raw_names_cannot_inject_paths_or_bypass_windows_safe_rules(self):
        unsafe = ('../escape.step', 'nested/escape.step', 'nested\\escape.step',
                  '/absolute.step', 'C:\\absolute.step', 'name:stream', '.', '..',
                  '.private', '~$lock.step', 'CON', 'nul.step', 'LPT9.txt',
                  'trailing.', 'trailing ', ' ', '', 'nul\x00byte', 'control\x01',
                  'pipe|name', 'question?', 'star*', 'quote"name', '<name>')
        for path_type in (PurePosixPath, PureWindowsPath):
            for name in unsafe:
                with self.subTest(path_type=path_type.__name__, name=repr(name)):
                    converted, _ = self.convert([], [item(name)], path_type)
                    self.assertEqual(converted, [])

    def test_unsafe_ancestor_is_not_reinterpreted_or_included_as_folder(self):
        for path_type in (PurePosixPath, PureWindowsPath):
            for name in ('../outside', 'one/two', 'one\\two', '.hidden', 'NUL'):
                with self.subTest(path_type=path_type.__name__, name=name):
                    converted, _ = self.convert([item(name, 'bad', folder=True)],
                        [item('bracket.step', parent='bad')], path_type)
                    self.assertEqual(converted, [])

    def test_duplicate_nested_file_remains_an_incomplete_scan(self):
        folders, files = self.nested_metadata()
        files.append(item('bracket.step', 'duplicate', 'parts'))
        for path_type in (PurePosixPath, PureWindowsPath):
            converted, errors = self.convert(folders, files, path_type)
            self.assertEqual(len([entry for entry in converted if not entry.is_folder]), 1)
            self.assertEqual(errors, ['Drive scan incomplete: duplicate items with the same name.'])


if __name__ == '__main__':
    unittest.main()
