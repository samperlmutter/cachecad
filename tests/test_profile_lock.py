"""Disposable synthetic profile locks; subprocesses never use accounts/network."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'maintained/utils/profile_lock.py').is_file():
    raise unittest.SkipTest('Maintained profile lock is absent from wheel fixture')

from maintained.utils.profile_lock import ProfileLock, ProfileLockError

CHILD = '''
import sys
from maintained.utils.profile_lock import ProfileLock, ProfileLockError
try:
    lock = ProfileLock(sys.argv[1]).acquire()
except ProfileLockError as error:
    print(error.code, flush=True)
    raise SystemExit(13)
print('acquired', flush=True)
if len(sys.argv) > 2:
    sys.stdin.readline()
lock.close()
'''


class ProfileLockTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        # macOS exposes its trusted temporary directory through /var aliases.
        # Canonicalize the fixture root, retaining explicit symlink rejection tests.
        self.root = Path(self.folder.name).resolve(strict=True) / 'synthetic-profile'

    def child(self):
        return subprocess.run([sys.executable, '-c', CHILD, str(self.root)], cwd=ROOT,
                              capture_output=True, text=True, timeout=10)

    def test_same_process_and_subprocess_exclusion_then_release(self):
        with ProfileLock(self.root) as lock:
            self.assertTrue(lock.acquired)
            self.assertFalse(os.get_inheritable(lock._fd))
            with self.assertRaises(ProfileLockError) as error:
                ProfileLock(self.root).acquire()
            self.assertEqual(error.exception.code, 'busy')
            self.assertIsNone(error.exception.__context__)
            result = self.child()
            self.assertEqual(result.returncode, 13, result.stderr)
            self.assertEqual(result.stdout.strip(), 'busy')
        self.assertFalse(lock.acquired)
        self.assertTrue(lock.path.is_file())
        self.assertEqual(lock.path.read_bytes(), b'\x00')
        result = self.child()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'acquired')

    def test_child_process_exit_releases_lock(self):
        process = subprocess.Popen([sys.executable, '-c', CHILD, str(self.root), 'wait'],
                                   cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.assertEqual(process.stdout.readline().strip(), 'acquired')
        with self.assertRaises(ProfileLockError) as error:
            ProfileLock(self.root).acquire()
        self.assertEqual(error.exception.code, 'busy')
        process.communicate('\n', timeout=10)
        self.assertEqual(process.returncode, 0)
        with ProfileLock(self.root):
            pass

    def test_abrupt_process_exit_releases_lock(self):
        process = subprocess.Popen([sys.executable, '-c', CHILD, str(self.root), 'wait'],
                                   cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.assertEqual(process.stdout.readline().strip(), 'acquired')
        process.kill()
        process.communicate(timeout=10)
        with ProfileLock(self.root):
            pass

    def test_independent_profiles_and_exception_cleanup(self):
        with ProfileLock(self.root), ProfileLock(self.root.parent / 'second'):
            pass
        with self.assertRaises(RuntimeError):
            with ProfileLock(self.root):
                raise RuntimeError('synthetic')
        with ProfileLock(self.root):
            pass

    @unittest.skipIf(os.name == 'nt', 'Windows symlink permission varies')
    def test_symlink_root_or_lockfile_is_never_followed(self):
        actual = Path(self.folder.name) / 'actual'
        actual.mkdir()
        self.root.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(ProfileLockError) as error:
            ProfileLock(self.root).acquire()
        self.assertEqual(error.exception.code, 'path')
        self.assertEqual(list(actual.iterdir()), [])
        self.root.unlink()
        self.root.mkdir()
        destination = actual / 'unrelated'
        destination.write_text('UNRELATED')
        (self.root / '.cachecad-profile.lock').symlink_to(destination)
        with self.assertRaises(ProfileLockError):
            ProfileLock(self.root).acquire()
        self.assertEqual(destination.read_text(), 'UNRELATED')

    def test_lock_directory_rejected(self):
        (self.root / '.cachecad-profile.lock').mkdir(parents=True)
        with self.assertRaises(ProfileLockError):
            ProfileLock(self.root).acquire()


if __name__ == '__main__':
    unittest.main()
