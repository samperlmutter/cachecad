"""Actual maintained UI path hooks with fake dialogs; no Qt or account access."""
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from cachecad.safety.onedrive_guard import assess_project_path
from recovered_harness import deny_external_effects, load_function

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless((ROOT / 'maintained/utils/path_advisory.py').exists(), 'maintained fixtures absent')
class OneDriveIntegrationTests(unittest.TestCase):
    def advisory(self, env, reply):
        calls = []
        def warning(*args):
            calls.append(args)
            return reply
        namespace = dict(os=SimpleNamespace(environ=env), assess_project_path=assess_project_path,
                         QMessageBox=SimpleNamespace(Yes=1, No=2, warning=warning), _ACKNOWLEDGED=set())
        function = load_function(ROOT / 'maintained/utils/path_advisory.py',
                                 'confirm_project_location', namespace)
        return function, calls

    def test_configured_path_decline_default_no(self):
        function, calls = self.advisory({'OneDrive': r'C:\Users\A\Cloud'}, 2)
        with deny_external_effects():
            self.assertFalse(function(r'C:\Users\A\Cloud\CAD'))
        self.assertEqual(calls[0][-1], 2)
        self.assertIn('not verified sync activity', calls[0][2])

    def test_accept_cached_only_for_same_path_and_configuration(self):
        env = {'OneDrive': r'C:\Cloud'}
        function, calls = self.advisory(env, 1)
        self.assertTrue(function(r'C:\Cloud\CAD'))
        self.assertTrue(function(r'C:\Cloud\CAD'))
        self.assertEqual(len(calls), 1)
        env['OneDriveConsumer'] = r'C:\Other'
        self.assertTrue(function(r'C:\Cloud\CAD'))
        self.assertEqual(len(calls), 2)

    def test_name_hint_warns_but_unknown_and_other_paths_do_not(self):
        function, calls = self.advisory({}, 2)
        self.assertFalse(function(r'C:\OneDrive\CAD'))
        self.assertTrue(function('relative/unknown'))
        self.assertEqual(len(calls), 1)
        function, calls = self.advisory({'OneDrive': r'C:\Cloud'}, 2)
        self.assertTrue(function(r'C:\CAD'))
        self.assertEqual(calls, [])

    def test_only_three_environment_keys_read(self):
        class Env(dict):
            def get(self, key, default=None):
                self.keys_read.append(key)
                return super().get(key, default)
        env = Env()
        env.keys_read = []
        function, _ = self.advisory(env, 2)
        function(r'C:\CAD')
        self.assertEqual(env.keys_read, ['OneDrive', 'OneDriveConsumer', 'OneDriveCommercial'])

    def test_reviewed_pure_helper_matches_tested_helper(self):
        self.assertEqual((ROOT / 'maintained/utils/onedrive_guard.py').read_bytes(),
                         (ROOT / 'src/cachecad/safety/onedrive_guard.py').read_bytes())

    def test_settings_change_decline_prevents_label_and_signal(self):
        for accepted in (False, True):
            events = []
            namespace = dict(Path=Path, settings=SimpleNamespace(get_working_directory=lambda: Path('/old')),
                             QFileDialog=SimpleNamespace(getExistingDirectory=lambda *a: '/new'),
                             confirm_project_location=lambda *a: accepted,
                             signal_manager=SimpleNamespace(change_working_directory=SimpleNamespace(
                                 emit=lambda *a: events.append(('signal', a)))))
            fn = load_function(ROOT / 'maintained/gui/pages/settings_page.py',
                               'SettingsPage.on_change_working_directory_clicked', namespace)
            obj = SimpleNamespace(project_directory_label=SimpleNamespace(
                setText=lambda value: events.append(('label', value))))
            with deny_external_effects():
                fn(obj)
            self.assertEqual(len(events), 2 if accepted else 0)

    def test_initial_selection_decline_prevents_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            for accepted in (False, True):
                events = []
                namespace = dict(Path=Path, logging=logging,
                    settings=SimpleNamespace(get_working_directory=lambda: Path('/old')),
                    QMessageBox=SimpleNamespace(Yes=1, No=2, question=lambda *a: 1),
                    QFileDialog=SimpleNamespace(getExistingDirectory=lambda *a: directory),
                    confirm_project_location=lambda *a: accepted,
                    signal_manager=SimpleNamespace(change_working_directory=SimpleNamespace(
                        emit=lambda *a: events.append(a))))
                fn = load_function(ROOT / 'maintained/gui/initial_settings_dialog.py',
                                   'select_working_directory', namespace)
                with deny_external_effects():
                    fn()
                self.assertEqual(len(events), int(accepted))
