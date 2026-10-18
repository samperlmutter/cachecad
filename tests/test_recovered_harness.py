"""Negative harness tests with synthetic source only; no application execution."""
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import recovered_harness as harness


class RecoveredHarnessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'recovered').mkdir()
        self.source = self.root / 'recovered' / 'fixture.py'
        self.enterContext(patch.object(harness, 'ROOT', self.root))

    def load(self, source, name='Example.run', namespace=None):
        self.source.write_text(source)
        return harness.load_function(self.source, name, namespace or {})

    def test_module_and_class_initializers_never_run(self):
        method = self.load("raise AssertionError('top level')\nclass Example:\n raise AssertionError('class body')\n def run(self, x):\n  return x + 1\n")
        self.assertEqual(method(None, 2), 3)

    def test_import_in_selected_body_denied(self):
        method = self.load('class Example:\n def run(self):\n  import socket\n')
        with self.assertRaisesRegex(AssertionError, 'external effect denied'):
            method(None)

    def test_open_in_selected_body_denied(self):
        method = self.load("class Example:\n def run(self):\n  return open('unexpected.txt', 'w')\n")
        with self.assertRaisesRegex(AssertionError, 'external effect denied'):
            method(None)
        self.assertFalse((self.root / 'unexpected.txt').exists())

    def test_definition_time_import_denied(self):
        with self.assertRaisesRegex(AssertionError, 'external effect denied'):
            self.load("class Example:\n def run(self, value=__import__('os')):\n  return value\n")

    def test_decorated_function_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Decorated'):
            self.load('class Example:\n @staticmethod\n def run():\n  return 1\n')

    def test_ambiguous_function_rejected(self):
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.load('class Example:\n def run(self): pass\n def run(self): pass\n')

    def test_source_outside_reviewed_tree_rejected(self):
        outside = self.root / 'outside.py'
        outside.write_text('def run(): return 1\n')
        with self.assertRaisesRegex(ValueError, 'reviewed'):
            harness.load_function(outside, 'run', {})

    def test_fixture_open_rejects_parent_escape(self):
        fixture = self.root / 'data'
        fixture.mkdir()
        with self.assertRaises(AssertionError):
            harness.fixture_open(fixture)(fixture / '../escaped.txt', 'w')
        self.assertFalse((self.root / 'escaped.txt').exists())

    def test_fixture_open_rejects_symlink_escape(self):
        fixture = self.root / 'data'
        fixture.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        (fixture / 'link').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(AssertionError):
            harness.fixture_open(fixture)(fixture / 'link' / 'escaped.txt', 'w')
        self.assertEqual(list(outside.iterdir()), [])

    def test_network_and_process_tripwires(self):
        with harness.deny_external_effects():
            for operation in (
                lambda: socket.create_connection(('invalid.example', 443)),
                lambda: subprocess.Popen(['must-never-start']),
                lambda: os.system('must-never-start'),
            ):
                with self.subTest(operation=operation), self.assertRaises(AssertionError):
                    operation()


if __name__ == '__main__':
    unittest.main()
