"""Local, plain-text report of actual executor outcomes and retained recovery files."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout
from gui.destructive_confirmation import display_value

LABELS = {
    'not_started': 'NOT COMMITTED: no destination mutation was entered',
    'unknown': 'OUTCOME UNKNOWN: do not repeat the write until reviewed',
    'committed': 'COMMITTED: destination mutation confirmed; review any warnings below',
    'committed_recording_failed': 'COMMITTED, RECORDING INCOMPLETE: do not repeat the write',
}


class TransferResultsDialog(QDialog):
    def __init__(self, results, parent=None, *, context=None):
        super().__init__(parent)
        self.setWindowTitle('Transfer results and recovery')
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(760, 480)
        layout = QVBoxLayout(self)
        label = QLabel('Some files may have changed even if another action failed. '
                       'Keep recovery files until you have checked the result. '
                       'No automatic retry or recovery overwrites are performed.', self)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        layout.addWidget(label)
        context = context or {}
        lines = ['Report from completed batch',
                 'Project: ' + display_value(context.get('project_name', 'Unavailable')),
                 'Remote project ID: ' + display_value(context.get('project_id', 'Unavailable')),
                 'Local project: ' + display_value(context.get('project_root', 'Unavailable')), '']
        for result in results:
            lines.extend((LABELS.get(result.get('outcome'), 'UNKNOWN RESULT'),
                          'Action: ' + display_value(result.get('action', '')),
                          'File: ' + display_value(result.get('path', ''))))
            if result.get('operation_id'):
                lines.append('Operation: ' + display_value(result['operation_id']))
            if result.get('recovery_dir'):
                lines.append('Local recovery record: ' + display_value(result['recovery_dir']))
            for warning in result.get('warnings', ()):
                lines.append('Warning: ' + display_value(str(warning)[:1000]))
            error_type = result.get('error_type')
            if isinstance(error_type, str) and error_type.isidentifier() and len(error_type) <= 80:
                lines.append('Failure category: ' + error_type)
            if result.get('cancelled'):
                lines.append('Cancellation requested; the outcome above determines whether a change occurred')
            lines.append('')
        text = QPlainTextEdit(self)
        text.setReadOnly(True)
        text.setPlainText('\n'.join(lines) if results else 'No transfer result has been recorded in this session.')
        text.setObjectName('transferOutcomeDetails')
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
