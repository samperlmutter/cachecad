"""Opt-in real Qt tests of assembled application source with disposable fixtures.

Set CACHECAD_RUNTIME_PYTHON to the reviewed, hash-locked runtime's Python binary.
Without it these tests skip; the ordinary source-only gate never launches Qt.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tooling.assemble_app import assemble


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = os.environ.get('CACHECAD_RUNTIME_PYTHON')


@unittest.skipUnless(RUNTIME, 'Set CACHECAD_RUNTIME_PYTHON for opt-in actual Qt runtime tests')
@unittest.skipUnless((ROOT / 'development/manifest.json').is_file(),
                     'Application sources absent in replacement wheel tests')
class OfflineRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.python = Path(RUNTIME).absolute()
        if not cls.python.is_file() or not os.access(cls.python, os.X_OK):
            raise ValueError('CACHECAD_RUNTIME_PYTHON must name an executable reviewed Python binary')
        cls.temporary = tempfile.TemporaryDirectory(prefix='cachecad-runtime-tests-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.application = cls.root / 'application'
        assemble(ROOT, cls.application)
        cls.scratch = cls.root / 'disposable-profiles'
        cls.scratch.mkdir()
        cls.environment = dict(os.environ)
        cls.environment.pop('PYTHONPATH', None)
        cls.environment.pop('PYTHONHOME', None)
        cls.environment.update(PYTHONDONTWRITEBYTECODE='1', QT_QPA_PLATFORM='offscreen',
                               TMPDIR=str(cls.scratch), TMP=str(cls.scratch), TEMP=str(cls.scratch))

    def snapshot_sources(self):
        return {path.relative_to(self.application).as_posix():
                hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.application.rglob('*') if path.is_file()}

    def run_scenario(self, scenario):
        before = self.snapshot_sources()
        completed = subprocess.run(
            [str(self.python), '-B', str(self.application / 'development/launch.py'),
             '--app-source', str(self.application), '--smoke', '--scenario', scenario],
            cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=45)
        diagnostic = completed.stdout + '\n' + completed.stderr
        self.assertEqual(completed.returncode, 0, diagnostic)
        result_lines = [line.removeprefix('CACHECAD_OFFLINE_RESULT=')
                        for line in completed.stdout.splitlines()
                        if line.startswith('CACHECAD_OFFLINE_RESULT=')]
        self.assertEqual(len(result_lines), 1, diagnostic)
        result = json.loads(result_lines[0])
        self.assertEqual(result['mode'], 'offline-development')
        self.assertEqual(result['actual_gui'], 'gui.main_window.MainWindow')
        self.assertEqual(result['qt_platform'], 'offscreen')
        self.assertTrue(result['synthetic_remote'])
        for field in ('errors', 'external_attempts', 'forbidden_modules_loaded',
                      'credential_files', 'slot_errors', 'missing_placeholder_resources'):
            self.assertEqual(result[field], [], (field, diagnostic))
        self.assertEqual(result['active_threads'], 0)
        self.assertEqual(len(result['rendered_size']), 2)
        self.assertTrue(all(dimension > 100 for dimension in result['rendered_size']))
        self.assertEqual(before, self.snapshot_sources(), 'Application source changed during launch')
        self.assertEqual(list(self.scratch.iterdir()), [], 'Disposable profile was not cleaned up')
        return result

    def test_success_uses_actual_gui_and_only_synthetic_project(self):
        result = self.run_scenario('success')
        self.assertTrue(result['scan_ready'])
        self.assertEqual(result['planned_paths'], ['part.txt'])
        self.assertEqual(result['planned_uploads'], 1)
        self.assertEqual(result['outcomes'], {'cancelled': [], 'failed': [], 'succeeded': [1]})
        self.assertEqual(result['database_write_generations'], [1])

    def test_cancel_failure_retry_and_stale_callbacks_are_isolated(self):
        result = self.run_scenario('lifecycle')
        self.assertTrue(result['scan_ready'])
        self.assertEqual(result['planned_paths'], ['part.txt'])
        self.assertEqual(result['planned_uploads'], 1)
        self.assertEqual(result['outcomes'], {'cancelled': [1], 'failed': [2], 'succeeded': [3]})
        self.assertEqual(result['database_write_generations'], [3])

    def test_close_with_pending_scan_cancels_without_database_write(self):
        result = self.run_scenario('close-pending')
        self.assertTrue(result['closed_pending_scan'])
        self.assertFalse(result.get('scan_ready', False))
        self.assertEqual(result['outcomes'], {'cancelled': [1], 'failed': [], 'succeeded': []})
        self.assertEqual(result['database_write_generations'], [])

    def test_placeholder_generation_repeats_and_matches_manifested_payload(self):
        rcc = self.python.parent / 'pyside6-rcc'
        self.assertTrue(rcc.is_file(), 'Reviewed runtime lacks pyside6-rcc')
        first, second = self.root / 'resources-first.py', self.root / 'resources-second.py'
        for output in (first, second):
            completed = subprocess.run(
                [str(self.python), '-B', str(self.application / 'development/build_resources.py'),
                 '--rcc', str(rcc), str(output)], cwd=self.root, env=self.environment,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first.read_bytes(),
                         (self.application / 'development/resources_rc.py').read_bytes())
        self.assertEqual(list(self.scratch.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
