"""Actual Qt outcome dialog, isolated from services and user profiles."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = os.environ.get('CACHECAD_RUNTIME_PYTHON')


@unittest.skipUnless((ROOT / 'maintained/gui/transfer_results.py').is_file(), 'Maintained sources absent')
class TransferResultsTests(unittest.TestCase):
    def test_successful_batch_is_presented_as_success(self):
        python = RUNTIME or sys.executable
        script = r"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'maintained'))
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit
from gui.transfer_results import TransferResultsDialog
app = QApplication([])
dialog = TransferResultsDialog([dict(path='part.step', action='download_new',
    outcome='committed', warnings=(), error_type=None)])
assert dialog.windowTitle() == 'Sync complete', dialog.windowTitle()
assert dialog.findChild(QLabel).text() == '1 file synchronized successfully.'
text = dialog.findChild(QPlainTextEdit, 'transferOutcomeDetails').toPlainText()
assert 'SUCCESS' in text and 'Warning:' not in text and 'Failure category:' not in text
print('clean-transfer-dialog-pass')
"""
        probe = subprocess.run([python, '-c', 'import PySide6'], capture_output=True)
        if probe.returncode:
            self.skipTest('Reviewed Qt runtime unavailable')
        result = subprocess.run([python, '-c', script], cwd=ROOT,
            env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
            text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('clean-transfer-dialog-pass', result.stdout)

    def test_actual_dialog_preserves_warnings_and_cancellation_uncertainty(self):
        python = RUNTIME or sys.executable
        script = r'''
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'maintained'))
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from gui.transfer_results import TransferResultsDialog
app = QApplication([])
results = [dict(path='part\n<bold>.sldprt', action='upload_revision', outcome='unknown',
                cancelled=True, error_type='TimeoutError', operation_id='fixture',
                recovery_dir='fixture/.cachecad/recovery/operation', warnings=('Keep both copies',)),
           dict(path='second.step', action='download_revision', outcome='committed',
                warnings=('Directory flush failed; retain recovery copies',))]
dialog = TransferResultsDialog(results)
dialog.show(); app.processEvents()
text = dialog.findChild(QPlainTextEdit, 'transferOutcomeDetails').toPlainText()
assert 'OUTCOME UNKNOWN' in text and 'Cancelled before mutation' not in text
assert 'Cancellation requested' in text and 'Failure category: TimeoutError' in text
assert 'Keep both copies' in text and 'Directory flush failed' in text
assert 'part\\n<bold>.sldprt' in text
assert 'transfer and local recording confirmed' not in text
assert dialog.findChild(QPlainTextEdit).isReadOnly()
QTest.keyClick(dialog, Qt.Key_Escape); app.processEvents()
from shiboken6 import isValid
assert not isValid(dialog) or not dialog.isVisible()
print('transfer-dialog-pass')
'''
        probe = subprocess.run([python, '-c', 'import PySide6'], capture_output=True)
        if probe.returncode:
            self.skipTest('Reviewed Qt runtime unavailable')
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
        result = subprocess.run([python, '-c', script], cwd=ROOT, env=env,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('transfer-dialog-pass', result.stdout)
