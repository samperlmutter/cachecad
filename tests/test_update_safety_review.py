"""Independent update-boundary regressions with no network or executable effects.

These tests load only maintained functions and pure updater modules. Disposable
paths and fabricated values are never production trust or installer inputs.
"""
import ast
import builtins
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from recovered_harness import deny_external_effects, load_function

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'maintained/core/application_updates.py').is_file():
    raise unittest.SkipTest('Maintained update implementation absent from wheel fixture')

from maintained.core import application_updates as updates


class ControllerDialogSafetyTests(unittest.TestCase):
    """Cancel/close must not permanently disable the controller's update entry."""

    def make_controller(self, *, closed=False, settled=True):
        previous = SimpleNamespace(
            _closed=closed, closed=closed, busy=not settled,
            show=Mock(), raise_=Mock(), shutdown=Mock(return_value=settled),
            deleteLater=Mock())
        replacement = SimpleNamespace(show=Mock())
        create = Mock(return_value=replacement)
        config = SimpleNamespace(publisher_certificate_sha256=('a' * 64,))
        service = Mock()
        modules = {
            'shiboken6': SimpleNamespace(isValid=lambda value: True),
            'core.application_updates': SimpleNamespace(
                UpdateService=service, InstalledRelease=updates.InstalledRelease,
                load_update_config=lambda directory: config),
            'core.windows_update_install': SimpleNamespace(
                SignerPolicy=Mock(), VerifiedInstaller=Mock()),
            'gui.application_updates': SimpleNamespace(ApplicationUpdateDialog=create),
            'app.app_version': SimpleNamespace(
                APP_PRODUCT_ID='cachecad-reconstructed', APP_RELEASE_NUMBER=1,
                APP_BUILD_DATE='2026-10-18'),
        }
        method = load_function('maintained/app/app_controller.py',
            'AppController.on_download_app_update', {
                'Path': Path, 'sys': SimpleNamespace(platform='fixture-only'),
                '__file__': str(ROOT / 'maintained/app/app_controller.py'),
                'APP_VERSION': '0.1.0'})
        def fixture_import(name, *args, **kwargs):
            if name not in modules:
                raise AssertionError('Unexpected import: ' + name)
            return modules[name]
        method.__globals__['__builtins__']['__import__'] = fixture_import
        controller = SimpleNamespace(
            development=None, _update_install_pending=False, _update_dialog=previous,
            main_window=SimpleNamespace(show_status_message=Mock()),
            application=SimpleNamespace(profile_root=Path('/fixture-only/profile')),
            _request_update_install=Mock())
        return method, controller, previous, replacement, create

    def test_open_live_dialog_is_reused_without_starting_a_second_operation(self):
        method, controller, old, new, create = self.make_controller()
        with deny_external_effects():
            method(controller)
        old.show.assert_called_once()
        old.raise_.assert_called_once()
        create.assert_not_called()

    def test_closed_settled_dialog_is_retired_and_recreated(self):
        method, controller, old, new, create = self.make_controller(closed=True)
        with deny_external_effects():
            method(controller)
        old.show.assert_not_called()
        old.shutdown.assert_called_once_with(0)
        old.deleteLater.assert_called_once()
        create.assert_called_once()
        self.assertIs(controller._update_dialog, new)
        new.show.assert_called_once()

    def test_closed_unsettled_dialog_is_not_reopened_or_deleted(self):
        method, controller, old, new, create = self.make_controller(closed=True, settled=False)
        with deny_external_effects():
            method(controller)
        old.show.assert_not_called()
        old.deleteLater.assert_not_called()
        create.assert_not_called()
        controller.main_window.show_status_message.assert_called_once()

    def test_pending_handoff_never_opens_another_update_dialog(self):
        method, controller, old, new, create = self.make_controller()
        controller._update_install_pending = True
        with deny_external_effects():
            method(controller)
        old.show.assert_not_called()
        create.assert_not_called()

    def test_pending_handoff_refuses_new_account_scan_and_transfer_actions(self):
        for name, arguments in (
            ('on_local_signout', ()), ('on_disconnect_clicked', ()),
            ('on_signin_clicked', ()), ('_begin_sync_analysis', ()),
            ('on_execute_sync', ([],)),
        ):
            with self.subTest(handler=name), deny_external_effects():
                # No manager, scan or transfer dependencies are supplied: reaching
                # any action after the exclusion guard would fail this test.
                method = load_function('maintained/app/app_controller.py',
                    'AppController.' + name, {})
                controller = SimpleNamespace(_update_install_pending=True)
                self.assertIsNone(method(controller, *arguments))


class LauncherCommitSafetyTests(unittest.TestCase):
    """Execute the launcher's unchanged main body with effects explicitly doubled."""

    def test_windows_run_lease_precedes_profile_and_diagnostics_work(self):
        class StopBeforeProfile(Exception):
            pass
        tree = ast.parse((ROOT / 'application/launch.py').read_text(encoding='utf-8'))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'run_application')
        isolated = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        for gate_blocked in (False, True):
            events = []
            def enter(lease):
                events.append('run-lease')
                if gate_blocked:
                    raise StopBeforeProfile()
            def profile(*args):
                events.append('profile')
                raise StopBeforeProfile()
            modules = {
                'utils.application_mode': SimpleNamespace(
                    configure_application=Mock(), default_profile_root=lambda: '/fixture-only'),
                'core.windows_update_install': SimpleNamespace(ApplicationRunLease=Mock()),
            }
            namespace = {
                '__builtins__': dict(vars(builtins),
                    __import__=lambda name, *args, **kwargs: modules[name]),
                'sys': SimpleNamespace(platform='win32'), 'application_profile': profile,
            }
            diagnostics = SimpleNamespace(activate=Mock(), enable_native=Mock())
            with self.subTest(gate_blocked=gate_blocked), deny_external_effects():
                exec(compile(isolated, str(ROOT / 'application/launch.py'), 'exec'), namespace)
                with self.assertRaises(StopBeforeProfile):
                    namespace['run_application'](
                        SimpleNamespace(platform=None, startup_check=False),
                        Path('/fixture-only'), diagnostics,
                        SimpleNamespace(enter_context=enter), [])
                self.assertEqual(events, ['run-lease'] if gate_blocked else ['run-lease', 'profile'])
                diagnostics.activate.assert_not_called()
                diagnostics.enable_native.assert_not_called()

    def test_preconfiguration_allows_only_reviewed_exclusion_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path_factory = Mock(side_effect=Path)
            path_factory.cwd.return_value = root
            for early_module in ('core.windows_update_install', 'core.application_updates',
                                 'core.windows_update_install.extra', 'app.app_version',
                                 'gui.application_updates', 'utils.paths', 'utils.app_settings'):
                method = load_function('maintained/utils/application_mode.py',
                    'configure_application', {
                        '_configuration': None, 'Path': path_factory,
                        'sys': SimpleNamespace(modules={early_module: object()}),
                        'ApplicationConfig': lambda profile, client, startup: SimpleNamespace(
                            profile_root=profile)})
                def fixture_import(name, *args, **kwargs):
                    if name != 'utils.development_mode':
                        raise AssertionError('Unexpected import: ' + name)
                    return SimpleNamespace(current=lambda: None)
                method.__globals__['__builtins__']['__import__'] = fixture_import
                with self.subTest(early_module=early_module), deny_external_effects():
                    if early_module == 'core.windows_update_install':
                        result = method(root, root / 'missing-client.json')
                        self.assertEqual(result.profile_root, root)
                    else:
                        with self.assertRaises(RuntimeError):
                            method(root, root / 'missing-client.json')

    def test_launcher_registers_only_an_armed_install(self):
        tree = ast.parse((ROOT / 'application/launch.py').read_text(encoding='utf-8'))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'run_application')
        assignments = [node for node in ast.walk(function) if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == 'coordinator'
                               for target in node.targets)]
        conditions = [node for node in ast.walk(function) if isinstance(node, ast.If)
                      and any(isinstance(value, ast.Attribute) and value.attr == 'install_pending'
                              for value in ast.walk(node.test))]
        self.assertEqual(len(assignments), 1)
        self.assertEqual(len(conditions), 1)
        isolated = ast.fix_missing_locations(ast.Module(
            body=[assignments[0], conditions[0]], type_ignores=[]))
        for armed in (False, True):
            with self.subTest(armed=armed), deny_external_effects():
                coordinator = SimpleNamespace(install_pending=armed)
                pending = []
                namespace = {'controller': SimpleNamespace(_update_coordinator=coordinator),
                             'pending_updates': pending}
                exec(compile(isolated, str(ROOT / 'application/launch.py'), 'exec'), namespace)
                self.assertEqual(pending, [coordinator] if armed else [])

    def run_main(self, *, application_exit=0, diagnostics_stopped=True,
                 application_raises=False, diagnostics_raises=False,
                 commit_success=True):
        events = []
        coordinator = SimpleNamespace(
            commit_after_clean_shutdown=Mock(side_effect=lambda: events.append('commit')
                                            or commit_success),
            abort_pending_install=Mock(side_effect=lambda: events.append('abort')))
        close_calls = 0
        def close_diagnostics():
            nonlocal close_calls
            close_calls += 1
            events.append('diagnostics-closed' if diagnostics_stopped else 'diagnostics-unsettled')
            if diagnostics_raises and close_calls == 1:
                raise RuntimeError('Synthetic diagnostics close failure')
            return diagnostics_stopped
        diagnostics = SimpleNamespace(
            close=Mock(side_effect=close_diagnostics), record_exception=Mock())
        module = SimpleNamespace(
            source_catalog=Mock(return_value={}),
            CrashDiagnostics=Mock(return_value=SimpleNamespace(install=lambda: diagnostics)))
        modules = {'crash_diagnostics': module,
                   'build_version': SimpleNamespace(APP_VERSION='0.1.1')}
        def fixture_import(name, *args, **kwargs):
            if name not in modules:
                raise AssertionError('Unexpected import: ' + name)
            return modules[name]
        def application(args, root, diagnostics, lifetime, pending):
            pending.append(coordinator)
            events.append('application-shutdown-return')
            if application_raises:
                raise RuntimeError('Synthetic incomplete application shutdown')
            return application_exit
        lifetime = SimpleNamespace(close=Mock(side_effect=lambda: events.append('lease-closed')))
        tree = ast.parse((ROOT / 'application/launch.py').read_text(encoding='utf-8'))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == 'main']
        self.assertEqual(len(functions), 1)
        with tempfile.TemporaryDirectory() as directory, deny_external_effects():
            root = Path(directory)
            (root / 'assembly-manifest.json').write_bytes(b'{"fixture":true}')
            namespace = {
                '__builtins__': dict(vars(builtins), __import__=fixture_import),
                'parse_arguments': lambda argv: SimpleNamespace(
                    app_source=root, diagnostic_native_helper=False),
                'ExitStack': lambda: lifetime,
                'verify_source': lambda root: {},
                'install_source_importer': lambda root, sources: None,
                'hashlib': hashlib, 'run_application': application,
                'sys': SimpleNamespace(stderr=io.StringIO(), exc_info=sys.exc_info),
            }
            isolated = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
            exec(compile(isolated, str(ROOT / 'application/launch.py'), 'exec'), namespace)
            result = namespace['main']([])
        return result, coordinator, diagnostics, events

    def test_commit_requires_clean_application_and_confirmed_diagnostics_exit(self):
        result, coordinator, diagnostics, events = self.run_main()
        self.assertEqual(result, 0)
        coordinator.commit_after_clean_shutdown.assert_called_once()
        self.assertLess(events.index('application-shutdown-return'), events.index('diagnostics-closed'))
        self.assertLess(events.index('diagnostics-closed'), events.index('commit'))
        self.assertLess(events.index('commit'), events.index('lease-closed'))

    def test_nonzero_application_exit_never_commits(self):
        result, coordinator, diagnostics, events = self.run_main(application_exit=2)
        self.assertEqual(result, 2)
        coordinator.commit_after_clean_shutdown.assert_not_called()
        coordinator.abort_pending_install.assert_called()

    def test_unconfirmed_diagnostics_shutdown_never_commits(self):
        result, coordinator, diagnostics, events = self.run_main(diagnostics_stopped=False)
        coordinator.commit_after_clean_shutdown.assert_not_called()
        coordinator.abort_pending_install.assert_called()

    def test_application_or_diagnostics_exception_never_commits(self):
        for change in ({'application_raises': True}, {'diagnostics_raises': True}):
            with self.subTest(change=change):
                result, coordinator, diagnostics, events = self.run_main(**change)
                self.assertEqual(result, 1)
                coordinator.commit_after_clean_shutdown.assert_not_called()
                coordinator.abort_pending_install.assert_called()

    def test_failed_commit_is_not_success(self):
        result, coordinator, diagnostics, events = self.run_main(commit_success=False)
        self.assertEqual(result, 1)
        coordinator.commit_after_clean_shutdown.assert_called_once()


class PolicySafetyTests(unittest.TestCase):
    def test_noncanonical_urls_cannot_cross_the_transport_boundary(self):
        refused = (
            'http://updates.example.invalid/releases/',
            'https://updates.example.invalid@attacker.example/releases/',
            'https://updates.example.invalid:444/releases/',
            'https://127.0.0.1/releases/',
            'https://localhost/releases/',
            'https://updates.example.invalid/releases/../installer.exe',
            'https://updates.example.invalid/releases/%2e%2e/installer.exe',
            'https://updates.example.invalid/releases/installer.exe?override=1',
            'https://updates.example.invalid/releases/installer.exe#override',
            'https://updates.example.invalid/releases/installer.exe\n',
            'https://updates.example.invalid\\attacker.example/installer.exe',
        )
        for url in refused:
            with self.subTest(url=url), self.assertRaises(ValueError):
                updates._url(url)

    def test_disabled_policy_with_extra_values_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as directory, deny_external_effects():
            Path(directory, 'update-policy.json').write_text(json.dumps({
                'schema': 1, 'configured': False,
                'metadata_base_url': 'https://attacker.example/metadata/'}))
            with self.assertRaises(updates.UpdateError) as error:
                updates.load_update_config(directory)
            self.assertEqual(error.exception.code, 'configuration')

    def test_policy_and_ancestor_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory, deny_external_effects():
            root = Path(directory)
            data = root / 'policy.json'
            data.write_text('{"schema":1,"configured":false}')
            try:
                (root / 'update-policy.json').symlink_to(data)
            except OSError:
                self.skipTest('Symlink creation unavailable on this test platform')
            with self.assertRaises(updates.UpdateError) as error:
                updates.load_update_config(root)
            self.assertEqual(error.exception.code, 'configuration')

    def test_equal_build_cannot_change_authenticated_version_or_digest(self):
        with tempfile.TemporaryDirectory() as directory, deny_external_effects():
            service = updates.UpdateService(None, directory, updates.InstalledRelease(
                'cachecad-reconstructed', '0.1.0', 1))
            offer = updates.ReleaseOffer(
                product_id='cachecad-reconstructed', channel='stable', version='0.1.1',
                build=2, architecture='x86_64', minimum_windows_build=19041,
                maximum_windows_build=99999, minimum_updater_version=1,
                profile_read_min=1, profile_read_max=1, profile_write_min=1,
                profile_write_max=1,
                installer_url='https://updates.example.invalid/releases/0.1.1.exe',
                release_notes_url='https://updates.example.invalid/notes/0.1.1',
                length=100, sha256='a' * 64,
                target_path='channels/stable/x86_64/installer.exe')
            service._high_water(offer)
            before = Path(directory, 'release-state.json').read_bytes()
            for changed in (replace(offer, sha256='b' * 64),
                            replace(offer, version='0.1.2'),
                            replace(offer, build=3),
                            replace(offer, version='0.1.0', build=3)):
                with self.subTest(changed=changed), self.assertRaises(updates.UpdateError) as error:
                    service._high_water(changed)
                self.assertEqual(error.exception.code, 'rollback')
                self.assertEqual(Path(directory, 'release-state.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
