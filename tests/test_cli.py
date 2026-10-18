"""Smoke-test only the handwritten offline entrypoint in an isolated process."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def invoke(self, *arguments):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
        with tempfile.TemporaryDirectory() as cwd:
            return subprocess.run([sys.executable, "-m", "cachecad", *arguments],
                                  cwd=cwd, env=env, text=True, capture_output=True,
                                  timeout=30, check=False)

    def test_offline_demo_is_machine_readable_and_converges(self):
        result = self.invoke("--demo", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "offline-demo")
        self.assertIs(payload["network_access"], False)
        self.assertIs(payload["production_ready"], False)
        self.assertEqual(payload["second_analysis_actions"], 0)
        self.assertEqual(len(payload["results"]), 2)

    def test_no_implicit_real_project_execution(self):
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Only --demo is enabled", result.stderr)

    def test_unknown_project_option_rejected(self):
        result = self.invoke("--project", "do-not-touch")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
