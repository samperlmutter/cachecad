"""Synthetic tests of the static comparator; no original module execution."""
import unittest
from compare_bytecode import compare


class CompareTests(unittest.TestCase):
    def test_same_code(self):
        a = compile('def f(x):\n return x + 1\n', 'a.py', 'exec')
        b = compile('\n\ndef f(x):\n return x + 1\n', 'b.py', 'exec')
        self.assertEqual(compare(a, b), ([], 2))

    def test_changed_constant(self):
        a = compile('x = 1', 'a', 'exec')
        b = compile('x = 2', 'a', 'exec')
        self.assertTrue(compare(a, b)[0])

    def test_changed_constant_type(self):
        a = compile('x = 1', 'a', 'exec')
        b = compile('x = True', 'a', 'exec')
        self.assertTrue(compare(a, b)[0])

    def test_nested_constant_types(self):
        a = compile('x = (1,)', 'a', 'exec')
        b = compile('x = (True,)', 'a', 'exec')
        self.assertTrue(compare(a, b)[0])

    def test_nested_logic(self):
        a = compile('def f(x):\n return x + 1\n', 'a', 'exec')
        b = compile('def f(x):\n return x - 1\n', 'a', 'exec')
        self.assertTrue(compare(a, b)[0])

    def test_arg_name(self):
        a = compile('def f(x):\n return x\n', 'a', 'exec')
        b = compile('def f(y):\n return y\n', 'a', 'exec')
        self.assertTrue(compare(a, b)[0])


if __name__ == '__main__':
    unittest.main()
