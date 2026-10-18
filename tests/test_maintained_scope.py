"""Synthetic AST patch-scope checks; no recovered code execution."""
from pathlib import Path
import tempfile
import unittest
from tooling.maintained import masked_ast


class PatchScopeTests(unittest.TestCase):
    def compare(self, first, second):
        with tempfile.TemporaryDirectory() as folder:
            a, b = Path(folder) / 'a.py', Path(folder) / 'b.py'
            a.write_text(first)
            b.write_text(second)
            return masked_ast(a, ['C.f']), masked_ast(b, ['C.f'])

    def test_reviewed_body_can_change(self):
        a, b = self.compare('class C:\n def f(self): return 1\n', 'class C:\n def f(self): return 2\n')
        self.assertEqual(a, b)

    def test_signature_cannot_change(self):
        a, b = self.compare('class C:\n def f(self): return 1\n', 'class C:\n def f(self, x): return 2\n')
        self.assertNotEqual(a, b)

    def test_other_method_cannot_change(self):
        a, b = self.compare('class C:\n def f(self): return 1\n def g(self): return 2\n', 'class C:\n def f(self): return 2\n def g(self): return 3\n')
        self.assertNotEqual(a, b)

    def test_imports_cannot_change(self):
        a, b = self.compare('import os\nclass C:\n def f(self): return 1\n', 'import subprocess\nclass C:\n def f(self): return 2\n')
        self.assertNotEqual(a, b)

    def test_missing_function_fails(self):
        with self.assertRaises(ValueError):
            self.compare('class C:\n def f(self): return 1\n', 'class C:\n def g(self): return 2\n')
