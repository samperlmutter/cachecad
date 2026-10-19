"""Actual window methods: native Quit may synchronously dispatch closeEvent."""
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock
from recovered_harness import load_function

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless((ROOT / 'maintained/gui/main_window.py').is_file(), 'Actual window source absent')
class WindowQuitLifecycleTests(unittest.TestCase):
    def window(self, layer, tray=False):
        native = SimpleNamespace(quit=Mock())
        env = {'QApplication': native, 'settings': SimpleNamespace(get_use_tray_icon=lambda: tray),
               'QIcon': lambda value: value}
        window = SimpleNamespace(setVisible=Mock(), tray_icon=Mock(), tray_icon_notifier=True)
        for name in ('closeEvent', 'exit_app'):
            setattr(window, name, MethodType(load_function(f'{layer}/gui/main_window.py',
                                                          'MainWindow.' + name, env), window))
        return window, native

    def test_original_close_reenters_native_quit(self):
        window, native = self.window('recovered')
        event = Mock()
        native.quit.side_effect = RuntimeError('synthetic recursive native termination')
        with self.assertRaises(RuntimeError):
            window.closeEvent(event)
        native.quit.assert_called_once_with()

    def test_ordinary_close_accepts_without_reentering_quit(self):
        window, native = self.window('maintained')
        event = Mock()
        window.closeEvent(event)
        event.accept.assert_called_once_with()
        event.ignore.assert_not_called()
        native.quit.assert_not_called()

    def test_explicit_exit_can_receive_native_close_callback_without_recursing(self):
        for tray in (False, True):
            with self.subTest(tray=tray):
                window, native = self.window('maintained', tray)
                event = Mock()
                native.quit.side_effect = lambda: window.closeEvent(event)
                window.exit_app()
                window.exit_app()
                native.quit.assert_called_once_with()
                event.accept.assert_called_once_with()
                event.ignore.assert_not_called()
                window.tray_icon.deleteLater.assert_called_once_with()

    def test_ordinary_tray_close_is_preserved_and_notification_is_once(self):
        window, native = self.window('maintained', True)
        event = Mock()
        window.closeEvent(event)
        window.closeEvent(event)
        self.assertEqual(event.ignore.call_count, 2)
        event.accept.assert_not_called()
        window.tray_icon.showMessage.assert_called_once()
        window.tray_icon.deleteLater.assert_not_called()
        native.quit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
