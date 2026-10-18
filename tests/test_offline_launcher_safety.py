"""Synthetic launcher/AST guard tests. No recovered app or native Qt is loaded."""
from contextlib import chdir
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import webbrowser

from tooling.maintained import masked_ast


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless((ROOT / 'development/launch.py').is_file(),
                     'Application development sources absent in replacement wheel tests')
class OfflineLauncherSafetyTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('_cachecad_launcher_test', ROOT / 'development/launch.py')
        self.launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.launcher)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.profile = self.root / 'profile'
        self.profile.mkdir()
        self.outside = self.root / 'outside'
        self.outside.mkdir()

    def install_tripwires(self):
        # Capture the hook rather than permanently altering the unittest process.
        # Preserve every Python-level function the reviewed launcher replaces.
        targets = ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                   (socket.socket, 'bind'), (socket.socket, 'sendto'),
                   (socket, 'create_connection'), (socket, 'getaddrinfo'),
                   (socket, 'gethostbyname'), (socket, 'gethostbyname_ex'),
                   (subprocess, 'Popen'), (os, 'system'), (os, 'open'),
                   (webbrowser, 'open'), (webbrowser, 'open_new'),
                   (webbrowser, 'open_new_tab'))
        for owner, name in targets:
            self.enterContext(patch.object(owner, name, getattr(owner, name)))
        if hasattr(os, 'startfile'):
            self.enterContext(patch.object(os, 'startfile', os.startfile))
        hooks = []
        self.enterContext(patch.object(sys, 'meta_path', list(sys.meta_path)))
        self.enterContext(patch.object(sys, 'addaudithook', side_effect=hooks.append))
        self.enterContext(chdir(self.profile))
        attempts, deny = self.launcher.install_tripwires(self.profile)
        self.assertEqual(len(hooks), 1)
        return hooks[0], attempts, deny

    def test_python_network_process_and_browser_functions_are_denied(self):
        _, attempts, deny = self.install_tripwires()
        for operation in (socket.create_connection, socket.getaddrinfo,
                          socket.gethostbyname, socket.gethostbyname_ex,
                          socket.socket.connect, socket.socket.connect_ex,
                          socket.socket.bind, socket.socket.sendto,
                          subprocess.Popen, os.system, webbrowser.open,
                          webbrowser.open_new, webbrowser.open_new_tab):
            with self.subTest(operation=operation):
                self.assertIs(operation, deny)
                with self.assertRaisesRegex(RuntimeError, 'disabled'):
                    operation()
        self.assertEqual(len(attempts), 13)

    def test_network_and_process_audit_events_are_denied(self):
        audit, _, _ = self.install_tripwires()
        for event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo',
                      'socket.gethostbyname', 'socket.sendto', 'subprocess.Popen',
                      'os.system', 'os.posix_spawn', 'os.exec', 'os.fork'):
            with self.subTest(event=event), self.assertRaisesRegex(RuntimeError, 'disabled'):
                audit(event, ())

    def test_write_audit_denies_outside_but_permits_profile(self):
        audit, _, _ = self.install_tripwires()
        for path in (self.outside / 'sentinel', '../outside/sentinel'):
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, 'outside'):
                audit('open', (path, 'w', os.O_WRONLY | os.O_CREAT))
        audit('open', (self.profile / 'fixture.txt', 'w', os.O_WRONLY | os.O_CREAT))
        audit('open', (self.outside / 'read-only-source', 'r', os.O_RDONLY))

    def test_mutation_audit_denies_outside_paths(self):
        audit, _, _ = self.install_tripwires()
        outside = str(self.outside / 'sentinel')
        inside = str(self.profile / 'fixture')
        events = (('os.remove', (outside, -1)), ('os.rmdir', (outside, -1)),
                  ('os.mkdir', (outside, 0o700, -1)),
                  ('os.rename', (inside, outside, -1, -1)),
                  ('os.link', (inside, outside, -1, -1)))
        for event, arguments in events:
            with self.subTest(event=event), self.assertRaisesRegex(RuntimeError, 'outside'):
                audit(event, arguments)

    @unittest.skipUnless(Path('/proc/self/fd').is_dir(), 'Linux descriptor path test')
    def test_relative_mutation_resolves_actual_directory_descriptor(self):
        audit, _, _ = self.install_tripwires()
        inside_fd = os.open(self.profile, os.O_RDONLY)
        outside_fd = os.open(self.outside, os.O_RDONLY)
        try:
            audit('os.remove', ('fixture', inside_fd))
            with self.assertRaisesRegex(RuntimeError, 'outside'):
                audit('os.remove', ('sentinel', outside_fd))
            with self.assertRaisesRegex(RuntimeError, 'outside'):
                audit('os.rename', ('fixture', 'sentinel', inside_fd, outside_fd))
        finally:
            os.close(inside_fd)
            os.close(outside_fd)

    @unittest.skipUnless(os.open in os.supports_dir_fd, 'Directory descriptors unavailable')
    def test_relative_open_cannot_write_through_outside_descriptor(self):
        self.install_tripwires()
        outside_fd = os.open(self.outside, os.O_RDONLY)
        try:
            with self.assertRaises(RuntimeError):
                descriptor = os.open('escape.txt', os.O_WRONLY | os.O_CREAT,
                                     0o600, dir_fd=outside_fd)
                os.close(descriptor)
            self.assertFalse((self.outside / 'escape.txt').exists())
        finally:
            os.close(outside_fd)

    def test_symlink_creation_is_denied(self):
        audit, _, _ = self.install_tripwires()
        with self.assertRaises(RuntimeError):
            audit('os.symlink', ('fixture', str(self.profile / 'link'), -1))

    def test_live_import_finder_blocks_every_declared_module(self):
        _, attempts, _ = self.install_tripwires()
        finder = sys.meta_path[0]
        self.assertEqual(len(self.launcher.FORBIDDEN_MODULES), 8)
        for name in self.launcher.FORBIDDEN_MODULES:
            with self.subTest(module=name), self.assertRaisesRegex(RuntimeError, 'Live account'):
                finder.find_spec(name)
        self.assertEqual(attempts, ['live-module:' + name for name in self.launcher.FORBIDDEN_MODULES])
        self.assertIsNone(finder.find_spec('core.local_client'))
        self.assertIsNone(finder.find_spec('gui.main_window'))

    def test_live_import_is_rejected_before_synthetic_module_body_executes(self):
        package = self.profile / 'core'
        package.mkdir()
        (package / '__init__.py').write_text('', encoding='utf-8')
        (package / 'auth_manager.py').write_text(
            "raise AssertionError('synthetic live module must never execute')\n", encoding='utf-8')
        self.enterContext(patch.object(sys, 'path', [str(self.profile), *sys.path]))
        self.enterContext(patch.dict(sys.modules))
        for name in ('core', 'core.auth_manager'):
            sys.modules.pop(name, None)
        _, attempts, _ = self.install_tripwires()
        with self.assertRaisesRegex(RuntimeError, 'Live account'):
            importlib.import_module('core.auth_manager')
        self.assertNotIn('core.auth_manager', sys.modules)
        self.assertEqual(attempts, ['live-module:core.auth_manager'])

    def assembly(self, body='value = 1\n'):
        app = self.root / 'assembled'
        source = app / 'src'
        source.mkdir(parents=True)
        module = source / 'fixture.py'
        module.write_text(body, encoding='utf-8')
        manifest = {'files': [{'path': 'src/fixture.py',
                               'sha256': hashlib.sha256(module.read_bytes()).hexdigest()}]}
        (app / 'assembly-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        return app, module, manifest

    def test_source_verification_never_executes_manifested_modules(self):
        app, _, _ = self.assembly("raise AssertionError('application must not execute')\n")
        self.launcher.verify_source(app)

    def test_changed_source_rejected(self):
        app, module, _ = self.assembly()
        module.write_text('value = 2\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.launcher.verify_source(app)

    def test_unmanifested_python_source_rejected(self):
        app, _, _ = self.assembly()
        (app / 'src/extra.py').write_text('value = 2\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Unmanifested'):
            self.launcher.verify_source(app)

    def test_manifest_parent_path_rejected(self):
        app, _, manifest = self.assembly()
        manifest['files'][0]['path'] = '../outside/fixture.py'
        (app / 'assembly-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            self.launcher.verify_source(app)

    def test_manifest_symlink_rejected_even_with_matching_content(self):
        app, module, _ = self.assembly()
        target = self.outside / 'fixture.py'
        target.write_bytes(module.read_bytes())
        module.unlink()
        try:
            module.symlink_to(target)
        except OSError as error:
            self.skipTest('Symlink creation unavailable: ' + str(error))
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            self.launcher.verify_source(app)


class RemovedImportScopeTests(unittest.TestCase):
    def sources(self, baseline, candidate):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        a, b = root / 'baseline.py', root / 'candidate.py'
        a.write_text(baseline + '\nclass C:\n def f(self): return 1\n', encoding='utf-8')
        b.write_text(candidate + '\nclass C:\n def f(self): return 2\n', encoding='utf-8')
        return a, b

    def test_exact_removed_import_is_allowed(self):
        a, b = self.sources('from dangerous import Service', '')
        options = {'removed_imports': ['from dangerous import Service']}
        self.assertEqual(masked_ast(a, ['C.f'], **options),
                         masked_ast(b, ['C.f'], candidate=True, **options))

    def test_removed_import_cannot_remain_in_candidate(self):
        a, b = self.sources('import dangerous', 'import dangerous')
        with self.assertRaisesRegex(ValueError, 'remains'):
            masked_ast(b, ['C.f'], candidate=True, removed_imports=['import dangerous'])

    def test_removed_import_must_exist_in_baseline(self):
        a, _ = self.sources('', '')
        with self.assertRaisesRegex(ValueError, 'absent'):
            masked_ast(a, ['C.f'], removed_imports=['import dangerous'])

    def test_duplicate_baseline_removed_import_rejected(self):
        a, _ = self.sources('import dangerous\nimport dangerous', '')
        with self.assertRaisesRegex(ValueError, 'duplicated'):
            masked_ast(a, ['C.f'], removed_imports=['import dangerous'])

    def test_removed_exception_cannot_hide_extra_statement(self):
        a, _ = self.sources('import dangerous', '')
        with self.assertRaisesRegex(ValueError, 'exactly one import'):
            masked_ast(a, ['C.f'], removed_imports=['import dangerous; run()'])

    def test_import_cannot_be_both_added_and_removed(self):
        a, _ = self.sources('import dangerous', '')
        with self.assertRaisesRegex(ValueError, 'contradictory'):
            masked_ast(a, ['C.f'], added_imports=['import dangerous'],
                       removed_imports=['import dangerous'])

    def test_unrelated_import_change_is_not_masked(self):
        a, b = self.sources('import dangerous\nimport original', 'import changed')
        options = {'removed_imports': ['import dangerous']}
        self.assertNotEqual(masked_ast(a, ['C.f'], **options),
                            masked_ast(b, ['C.f'], candidate=True, **options))


if __name__ == '__main__':
    unittest.main()
