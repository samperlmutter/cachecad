"""Pure startup/configuration contracts for the real application entrypoint."""
import ast
from contextlib import chdir
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from recovered_harness import deny_external_effects, load_function

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless((ROOT / 'application/launch.py').is_file(),
                     'Real application source is not included in the experimental wheel')
class ApplicationAuthContracts(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.launch = load_module('_real_application_launch_contract', 'application/launch.py')

    def test_owner_config_missing_is_valid_startup_without_creating_oauth_client(self):
        args = self.launch.parse_arguments(['--app-source', str(self.directory)])
        self.assertEqual(args.client_config, self.directory / 'config/google-desktop-client.json')
        self.assertFalse(args.client_config.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_profile_is_separate_and_never_imports_legacy_credentials(self):
        profile = self.launch.prepare_profile(self.directory / 'profile')
        self.assertEqual(json.loads((profile / 'app_data/settings.json').read_text())['working_directory'],
                         str(profile / 'projects'))
        self.assertFalse((profile / 'projects').exists())
        (profile / 'app_data/token.json').write_text('synthetic legacy token')
        with self.assertRaises(ValueError):
            self.launch.prepare_profile(profile)

    def test_unmarked_existing_directory_and_nonempty_startup_profile_are_rejected(self):
        (self.directory / 'existing.txt').write_text('do not alter')
        for startup in (True, False):
            with self.assertRaises(ValueError):
                self.launch.prepare_profile(self.directory, startup_check=startup)
        self.assertEqual(list(self.directory.iterdir()), [self.directory / 'existing.txt'])

    def test_profile_rejects_symlink_before_writing_settings(self):
        target = self.directory / 'target'
        target.mkdir()
        linked = self.directory / 'linked'
        linked.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.launch.prepare_profile(linked)
        self.assertEqual(list(target.iterdir()), [])

    def test_source_verification_requires_real_session_and_owned_resources(self):
        (self.directory / 'src').mkdir()
        (self.directory / 'assembly-manifest.json').write_text(json.dumps({'files': []}))
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.launch.verify_source(self.directory)

    def test_vendor_services_are_blocked_before_any_loader_fallback(self):
        finder = self.launch.VerifiedSourceFinder(self.directory, {})
        for module in self.launch.BLOCKED_MODULES | {'development', 'development.launch'}:
            with self.subTest(module=module), self.assertRaises(ImportError):
                finder.find_spec(module)

    def test_production_entrypoint_never_contains_mock_authorization_or_fixture_backend(self):
        tree = ast.parse((ROOT / 'application/launch.py').read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or '')
        self.assertFalse(any(name.startswith(('development', 'unittest.mock')) for name in imports))
        self.assertNotIn('FixtureDrive', (ROOT / 'application/launch.py').read_text())
        self.assertNotIn('configure_offline', (ROOT / 'application/launch.py').read_text())

    def test_create_project_requires_current_authenticated_state(self):
        method = load_function('maintained/app/app_controller.py', 'AppController.create_project_from_link', {})
        controller = SimpleNamespace(development=None, signed_in=False, buttons_disabled=False)
        self.assertIn('Sign in', method(controller, 'synthetic-drive-id'))

    def test_session_invalidation_clears_stale_plans_and_requires_restart_during_transfer(self):
        signal_names = ('sync_cancelled', 'page_change')
        signals = SimpleNamespace(**{name: Mock() for name in signal_names})
        method = load_function('maintained/app/app_controller.py', 'AppController.on_session_signed_out',
            {'signal_manager': signals, 'Page': SimpleNamespace(HOME='home')})
        controller = SimpleNamespace(auth_manager=SimpleNamespace(last_invalidation='reauth_required', require_restart=Mock()),
            sync_executing=True, _retire_sync_clients=Mock(), _clear_sync_plan=Mock(), disable_buttons=Mock(),
            main_window=SimpleNamespace(settings_btn=Mock()), creds=object(), user={'email': 'synthetic@example.invalid'},
            project_id='synthetic-id', project_name='synthetic')
        method(controller)
        controller.auth_manager.require_restart.assert_called_once()
        signals.sync_cancelled.emit.assert_called_once()
        self.assertFalse(controller.sync_executing)
        self.assertIsNone(controller.creds)
        self.assertIsNone(controller.project_id)
        controller._clear_sync_plan.assert_called_once()


if __name__ == '__main__':
    unittest.main()
