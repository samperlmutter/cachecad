"""UI worker/generation regression tests using inspected function bodies only."""
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from recovered_harness import load_function, deny_external_effects

ROOT = Path(__file__).resolve().parents[1]
GUI = 'maintained/gui/desktop_signin_dialog.py'


@unittest.skipUnless((ROOT / GUI).is_file(), 'Maintained sign-in GUI absent in wheel tests')
class SignInGuiWorkerTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(deny_external_effects())
        name = '_signin_gui_core_contract'
        spec = importlib.util.spec_from_file_location(name, ROOT / 'maintained/core/desktop_signin.py')
        self.core = importlib.util.module_from_spec(spec)
        self.enterContext(patch.dict(sys.modules, {name: self.core}))
        spec.loader.exec_module(self.core)
        self.completion = SimpleNamespace(ready=Mock())
        self.event = threading.Event()
        self.worker = load_function(GUI, '_run_backend', {
            'DesktopSignInError': self.core.DesktopSignInError,
            'SignInIdentity': self.core.SignInIdentity,
            'SAFE_CODES': frozenset(self.core._ERRORS),
        })

    def run_worker(self, *, value=None, error=None):
        backend = SimpleNamespace(run=Mock(return_value=value, side_effect=error))
        factory = Mock(return_value=backend)
        self.worker(factory, Path('synthetic-client.json'), self.event, 17, 42, self.completion)
        factory.assert_called_once_with(Path('synthetic-client.json'))
        backend.run.assert_called_once_with(cancel_event=self.event, timeout=17)
        return self.completion.ready.emit.call_args.args

    def test_only_typed_identity_reaches_ui(self):
        identity = self.core.SignInIdentity('synthetic@example.invalid', 'fixture')
        self.assertEqual(self.run_worker(value=identity), (42, identity, ''))

    def test_arbitrary_return_is_not_presented_as_verified(self):
        self.assertEqual(self.run_worker(value={'email': 'unverified@example.invalid'}), (42, None, 'identity'))

    def test_provider_exception_is_generic_and_does_not_leak(self):
        message = 'fake access_token=DO_NOT_LOG_TOKEN&code=DO_NOT_LOG_CODE'
        self.assertEqual(self.run_worker(error=RuntimeError(message)), (42, None, 'failed'))

    def test_unrecognized_mutated_error_code_is_not_reemitted(self):
        error = self.core.DesktopSignInError('denied')
        error.code = 'access_token=DO_NOT_LOG_TOKEN'
        self.assertEqual(self.run_worker(error=error), (42, None, 'failed'))

    def test_safe_errors_preserve_only_fixed_code(self):
        self.assertEqual(self.run_worker(error=self.core.SignInTimeout()), (42, None, 'timeout'))

    def test_cancellation_discards_late_verified_identity(self):
        identity = self.core.SignInIdentity('stale@example.invalid', 'fixture')
        self.event.set()
        self.assertEqual(self.run_worker(value=identity), (42, None, 'cancelled'))

    def test_worker_tolerates_deleted_qt_signal_receiver(self):
        self.completion.ready.emit.side_effect = RuntimeError('C++ signal receiver deleted')
        self.assertEqual(self.run_worker(error=ValueError('private')), (42, None, 'failed'))

    def test_closed_and_stale_callbacks_never_settle_or_retain_identity(self):
        method = load_function(GUI, 'DesktopSignInDialog._receive', {})
        identity = self.core.SignInIdentity('stale@example.invalid', 'fixture')
        for closed, generation in ((True, 42), (False, 43)):
            with self.subTest(closed=closed, generation=generation):
                dialog = SimpleNamespace(_closed=closed, _generation=generation,
                                         _pending_result=None, _settle=Mock())
                method(dialog, 42, identity, '')
                self.assertIsNone(dialog._pending_result)
                dialog._settle.assert_not_called()

    def test_shutdown_invalidates_and_clears_account_before_bounded_join(self):
        method = load_function(GUI, 'DesktopSignInDialog.shutdown', {})
        dialog = SimpleNamespace(_closed=False, _generation=3, _pending_result=object(),
                                 _identity=object(), _config_path=Path('synthetic-client.json'),
                                 identity_label=Mock(), config_label=Mock(), _poll=Mock(),
                                 _cancel_event=threading.Event(), _update_controls=Mock(), _thread=Mock())
        def check_join(timeout):
            self.assertTrue(dialog._closed)
            self.assertEqual(dialog._generation, 4)
            self.assertIsNone(dialog._identity)
            self.assertIsNone(dialog._pending_result)
            self.assertIsNone(dialog._config_path)
            self.assertTrue(dialog._cancel_event.is_set())
            self.assertLessEqual(timeout, 1.5)
        dialog._thread.join.side_effect = check_join
        dialog._thread.is_alive.return_value = True
        self.assertFalse(method(dialog, timeout=99))
        dialog._thread.join.assert_called_once_with(1.5)
        dialog.identity_label.clear.assert_called_once()
        dialog._thread.is_alive.return_value = False
        self.assertTrue(method(dialog, timeout=0))
        self.assertIsNone(dialog._thread)
        self.assertEqual(dialog._generation, 4)


if __name__ == '__main__':
    unittest.main()
