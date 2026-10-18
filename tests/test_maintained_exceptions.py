"""Negative checks for explicit reviewed AST exceptions; synthetic source only."""
from pathlib import Path
import tempfile
import unittest
from tooling.maintained import masked_ast


class MaintainedExceptionTests(unittest.TestCase):
    def compare(self, candidate, **exceptions):
        with tempfile.TemporaryDirectory() as temp:
            a = Path(temp) / 'baseline.py'
            b = Path(temp) / 'candidate.py'
            a.write_text('class C:\n def f(self): return 1\n')
            b.write_text(candidate)
            return (masked_ast(a, ['C.f'], **exceptions),
                    masked_ast(b, ['C.f'], candidate=True, **exceptions))

    def test_explicit_signature_change_allowed(self):
        a, b = self.compare('class C:\n def f(self, token=None): return 2\n',
                            changed_signatures=['C.f'])
        self.assertEqual(a, b)

    def test_signature_exception_requires_changed_function(self):
        with self.assertRaisesRegex(ValueError, 'Signature exception'):
            self.compare('class C:\n def f(self): return 2\n', changed_signatures=['C.g'])

    def test_explicit_added_method_and_signal_allowed(self):
        a, b = self.compare('class C:\n ready = Signal(int)\n def f(self): return 2\n def g(self): return 3\n',
                            added_nodes=['C.ready', 'C.g'])
        self.assertEqual(a, b)

    def test_added_method_missing_rejected(self):
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.compare('class C:\n def f(self): return 2\n', added_nodes=['C.g'])

    def test_duplicate_added_method_rejected(self):
        with self.assertRaisesRegex(ValueError, 'duplicated'):
            self.compare('class C:\n def f(self): return 2\n def g(self): pass\n def g(self): pass\n',
                         added_nodes=['C.g'])

    def test_added_class_not_permitted(self):
        with self.assertRaisesRegex(ValueError, 'Only methods'):
            self.compare('class C:\n def f(self): return 2\n class Nested: pass\n',
                         added_nodes=['C.Nested'])

    def test_explicit_import_allowed(self):
        a, b = self.compare('from threading import RLock\nclass C:\n def f(self): return 2\n',
                            added_imports=['from threading import RLock'])
        self.assertEqual(a, b)

    def test_import_exception_cannot_hide_second_statement(self):
        with self.assertRaisesRegex(ValueError, 'exactly one import'):
            self.compare('import os\nclass C:\n def f(self): return 2\n',
                         added_imports=['import os; dangerous()'])

    def test_unreviewed_decorator_still_detected_with_signature_exception(self):
        a, b = self.compare('class C:\n @staticmethod\n def f(token=None): return 2\n',
                            changed_signatures=['C.f'])
        self.assertNotEqual(a, b)


if __name__ == '__main__':
    unittest.main()
