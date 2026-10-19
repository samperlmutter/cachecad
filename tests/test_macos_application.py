"""Portable checks for the real macOS launch path and honest updater limits."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
from recovered_harness import load_function

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'application/launch.py').is_file():
    raise unittest.SkipTest('Application source absent from isolated scaffold wheel')
spec = importlib.util.spec_from_file_location('macos_launch_contract', ROOT / 'application/launch.py')
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


class MacApplicationContracts(unittest.TestCase):
    def test_native_cocoa_is_accepted_only_on_macos(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(launch.sys, 'platform', 'darwin'):
                args = launch.parse_arguments(['--app-source', temporary, '--startup-check', '--platform', 'cocoa'])
                self.assertEqual(args.platform, 'cocoa')
            with patch.object(launch.sys, 'platform', 'linux'), self.assertRaises(SystemExit):
                launch.parse_arguments(['--app-source', temporary, '--startup-check', '--platform', 'cocoa'])

    def test_mac_update_instructions_do_not_import_or_contact_windows_service(self):
        method = load_function('maintained/app/app_controller.py', 'AppController.on_download_app_update',
                               {'sys': SimpleNamespace(platform='darwin')})
        controller = SimpleNamespace(main_window=SimpleNamespace(show_status_message=Mock()))
        method(controller)
        text = controller.main_window.show_status_message.call_args.args[0]
        self.assertIn('installed manually', text)
        self.assertIn('Quit CacheCAD', text)
        self.assertNotIn('current version', text)


if __name__ == '__main__':
    unittest.main()
