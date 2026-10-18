"""Optional YAML parser regression; run with PyYAML installed (not core runtime)."""
from pathlib import Path
import unittest

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(yaml is None, 'Optional PyYAML parser not installed')
class WorkflowYamlTests(unittest.TestCase):
    def test_workflow_parses_with_expected_triggers_and_permissions(self):
        path = ROOT / '.github/workflows/linux-ci.yml'
        if not path.is_file():
            self.skipTest('Workflow not included in isolated wheel test fixture')
        workflow = yaml.safe_load(path.read_text(encoding='utf-8'))
        self.assertEqual(set(workflow['on']), {'push', 'pull_request', 'workflow_dispatch'})
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        steps = workflow['jobs']['offline']['steps']
        self.assertFalse(steps[0]['with']['persist-credentials'])
        run_steps = [step['run'] for step in steps if 'run' in step]
        self.assertTrue(all(isinstance(command, str) for command in run_steps))
        self.assertIn('--only-binary=:all:', run_steps[0])
        self.assertIn('--require-hashes', run_steps[0])

    def test_real_qt_system_libraries_precede_runtime_tests(self):
        path = ROOT / ".github/workflows/linux-ci.yml"
        if not path.is_file():
            self.skipTest("Workflow not included in isolated wheel test fixture")
        steps = yaml.safe_load(path.read_text())["jobs"]["offline-qt"]["steps"]
        commands = [step["run"] for step in steps if "run" in step]
        self.assertIn("apt-get install -y --no-install-recommends libegl1 libgl1 libopengl0", commands[0])
        self.assertIn("--require-hashes", commands[1])
        self.assertIn("test_offline_runtime.py", commands[2])

    def test_unquoted_colon_space_command_is_rejected(self):
        with self.assertRaises(yaml.YAMLError):
            yaml.safe_load('run: python -m pip install --only-binary=:all: --require-hashes\n')
