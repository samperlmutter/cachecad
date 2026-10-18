"""Offline switch and actual controller guards without application-module imports.

Only the new pure configuration helper and selected maintained function ASTs
execute here. Full Qt integration is the separate reviewed launcher smoke test.
"""
import ast
from contextlib import chdir
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from recovered_harness import load_function, deny_external_effects

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = 'maintained/app/app_controller.py'


@unittest.skipUnless((ROOT / 'maintained/utils/development_mode.py').is_file(),
                     'Maintained development fixtures absent in replacement wheel tests')
class OfflineDevelopmentContractTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())
        self.profile = Path(self.enterContext(tempfile.TemporaryDirectory()))
        name = '_cachecad_offline_contract_helper'
        spec = importlib.util.spec_from_file_location(name, ROOT / 'maintained/utils/development_mode.py')
        self.helper = importlib.util.module_from_spec(spec)
        self.enterContext(patch.dict(sys.modules, {name: self.helper}))
        spec.loader.exec_module(self.helper)

    def test_import_defaults_to_disabled_without_side_effects(self):
        self.assertIsNone(self.helper.current())
        self.assertFalse(self.helper.is_enabled())
        self.assertEqual(list(self.profile.iterdir()), [])

    def test_environment_variable_cannot_enable_offline_mode(self):
        with patch.dict('os.environ', {'CACHECAD_OFFLINE': '1', 'CACHECAD_DEVELOPMENT': '1'}):
            self.assertFalse(self.helper.is_enabled())

    def test_configuration_requires_isolated_current_directory(self):
        with self.assertRaises(ValueError):
            self.helper.configure_offline(self.profile)
        self.assertIsNone(self.helper.current())

    def test_configuration_is_explicit_frozen_and_once_per_process(self):
        factory = Mock()
        with chdir(self.profile):
            config = self.helper.configure_offline(self.profile, factory)
            self.assertTrue(self.helper.is_enabled())
            self.assertIs(self.helper.current(), config)
            self.assertIs(config.drive_client_factory, factory)
            factory.assert_not_called()
            with self.assertRaises(AttributeError):
                config.profile_root = Path('/')
            with self.assertRaises(RuntimeError):
                self.helper.configure_offline(self.profile)

    def test_activation_after_application_import_is_rejected(self):
        for name in ('app.app_controller', 'core.auth_manager', 'gui.main_window',
                     'utils.paths', 'utils.app_settings', 'utils.cloud_logging',
                     'utils.oauth_credential_import'):
            with self.subTest(module=name), chdir(self.profile), patch.dict(sys.modules, {name: object()}):
                with self.assertRaises(RuntimeError):
                    self.helper.configure_offline(self.profile)
            self.assertFalse(self.helper.is_enabled())

    def test_invalid_profile_is_not_created_or_configured(self):
        missing = self.profile / 'missing'
        with chdir(self.profile), self.assertRaises(FileNotFoundError):
            self.helper.configure_offline(missing)
        self.assertFalse(missing.exists())
        file = self.profile / 'ordinary-file'
        file.write_text('fixture')
        with chdir(self.profile), self.assertRaises(ValueError):
            self.helper.configure_offline(file)
        self.assertFalse(self.helper.is_enabled())

    def test_configuration_rejects_non_callable_fixture(self):
        with chdir(self.profile), self.assertRaises(TypeError):
            self.helper.configure_offline(self.profile, object())

    def test_profile_paths_reject_parent_and_symlink_escapes(self):
        with chdir(self.profile):
            config = self.helper.configure_offline(self.profile)
            self.assertEqual(config.require_profile_path(self.profile / 'fixture'), self.profile / 'fixture')
            with self.assertRaises(ValueError):
                config.require_profile_path(self.profile / '..' / 'outside')
            (self.profile / 'escape').symlink_to(self.profile.parent, target_is_directory=True)
            with self.assertRaises(ValueError):
                config.require_profile_path(self.profile / 'escape' / 'outside')

    def test_offline_slots_return_before_import_io_or_gui_access(self):
        guarded = {
            'on_signin_clicked': (), 'user_clicked_sign_in': (),
            'on_auth_success': (object(),), 'on_user_profile_loaded': ({}, None),
            'on_bug_clicked': (), 'on_bug_report': (0, 'fixture report'),
            'on_change_working_directory': (Path('/outside'), Path('/old')),
            'on_new_installation': (), 'prompt_for_usage_data': (),
            'on_download_app_update': (), 'on_execute_sync': ([object()],),
        }
        controller = SimpleNamespace(development=object())
        for name, arguments in guarded.items():
            with self.subTest(method=name):
                method = load_function(CONTROLLER, 'AppController.' + name, {'Path': Path})
                self.assertIsNone(method(controller, *arguments))
        method = load_function(CONTROLLER, 'AppController.create_project_from_link', {})
        self.assertIn('disabled', method(controller, 'https://drive.google.com/real-project'))

    def test_offline_controller_constructor_skips_auth_and_ipc(self):
        with chdir(self.profile):
            config = self.helper.configure_offline(self.profile)
        settings = Mock()
        settings.get_working_directory.return_value = self.profile / 'projects'
        signal_names = ('prompt_for_usage_data', 'new_install', 'change_working_directory',
                        'project_list_changed', 'user_validation_data_fetched', 'project_read_only')
        signals = SimpleNamespace(**{name: Mock() for name in signal_names})
        project_manager = Mock()
        ipc = Mock(side_effect=AssertionError('IPC must be skipped offline'))
        namespace = dict(current_development_mode=lambda: config, settings=settings,
                         CaseInsensitivePathDict=dict, signal_manager=signals,
                         ProjectManager=project_manager, SingleInstance=ipc,
                         logging=Mock(), traceback=Mock())
        method = load_function(CONTROLLER, 'AppController.__init__', namespace)
        callbacks = ('prompt_for_usage_data', 'on_new_installation', 'on_change_working_directory',
                     'on_project_list_changed', 'on_user_validation_data_fetched', 'debug_slot')
        controller = SimpleNamespace(**{name: Mock() for name in callbacks})
        method(controller)
        self.assertIsNone(controller.auth_manager)
        self.assertIsNone(controller.single)
        self.assertIsNone(controller.creds)
        self.assertFalse(controller.has_internet)
        self.assertFalse(controller.signed_in)
        ipc.assert_not_called()
        settings.load.assert_called_once_with()
        project_manager.assert_called_once_with(self.profile / 'projects')

    def test_fixture_client_requires_explicit_marker(self):
        method = load_function(CONTROLLER, 'AppController._configure_offline_drive', {})
        with self.assertRaises(RuntimeError):
            method(SimpleNamespace(development=None), SimpleNamespace(is_offline_fixture=True))
        for adapter in (object(), SimpleNamespace(is_offline_fixture='true')):
            with self.assertRaises(TypeError):
                method(SimpleNamespace(development=object()), adapter)

    def test_offline_executor_completion_skips_identity_and_cloud_imports(self):
        signal = Mock()
        method = load_function('maintained/core/sync_executor.py',
                               'SyncExecutor.check_if_all_actions_finished',
                               {'logging': Mock(), 'signal_manager': SimpleNamespace(sync_execution_finished=signal)})
        imports = []
        def import_only_offline_switch(name, *args, **kwargs):
            imports.append(name)
            if name != 'utils.development_mode':
                raise AssertionError('Unexpected service import')
            return SimpleNamespace(is_enabled=lambda: True)
        method.__globals__['__builtins__']['__import__'] = import_only_offline_switch
        identity = Mock(side_effect=AssertionError('Identity gathering must be skipped offline'))
        executor = SimpleNamespace(failed_action_count=0, complete_action_count=1,
                                   cancelled_action_count=0, action_tuple=[object()],
                                   drive_client=SimpleNamespace(get_user_info_for_cloud_logging=identity),
                                   deleteLater=Mock())
        method(executor)
        identity.assert_not_called()
        self.assertEqual(imports, [])
        signal.emit.assert_called_once_with()
        executor.deleteLater.assert_called_once_with()

    def test_startup_sources_do_not_eagerly_import_live_modules(self):
        forbidden = {'core.auth_manager', 'core.bug_report', 'core.drive_client',
                     'core.github_updater', 'core.user_validation',
                     'utils.cloud_logging', 'utils.oauth_credential_import',
                     'core.service_credential_manager'}
        for filename in (CONTROLLER, 'maintained/core/sync_executor.py'):
            tree = ast.parse((ROOT / filename).read_text())
            imports = {node.module for node in tree.body if isinstance(node, ast.ImportFrom)}
            self.assertFalse(imports & forbidden, filename)


if __name__ == '__main__':
    unittest.main()
