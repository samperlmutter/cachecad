"""Integrity regressions operate solely on disposable local files."""
import tempfile
import unittest
from pathlib import Path

from tooling.integrity import create, safe_path, verify


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "sample.txt").write_text("evidence", encoding="utf-8")
        self.manifest = create(self.root, ["sample.txt"], "static-evidence", self.root / "manifest.json")

    def test_roundtrip(self):
        self.assertEqual(verify(self.root, self.manifest), [])

    def test_same_size_tamper_detected(self):
        (self.root / "sample.txt").write_text("modified", encoding="utf-8")
        self.assertEqual(verify(self.root, self.manifest), ["Changed: sample.txt"])

    def test_missing_detected(self):
        (self.root / "sample.txt").unlink()
        self.assertEqual(verify(self.root, self.manifest), ["Missing: sample.txt"])

    def test_parent_escape_rejected(self):
        with self.assertRaises(ValueError):
            safe_path(self.root, "../outside")

    def test_absolute_path_rejected(self):
        with self.assertRaises(ValueError):
            safe_path(self.root, str(self.root / "sample.txt"))

    def test_duplicate_rejected(self):
        self.manifest["files"] *= 2
        with self.assertRaises(ValueError):
            verify(self.root, self.manifest)

    def test_symlink_rejected(self):
        link = self.root / "link.txt"
        try:
            link.symlink_to(self.root / "sample.txt")
        except OSError:
            self.skipTest("Symlinks unavailable on this platform")
        with self.assertRaises(ValueError):
            create(self.root, ["link.txt"], "static-evidence", self.root / "manifest.json")

    def test_empty_rejected(self):
        (self.root / "empty").mkdir()
        with self.assertRaises(ValueError):
            create(self.root, ["empty"], "static-evidence", self.root / "manifest.json")


if __name__ == "__main__":
    unittest.main()
