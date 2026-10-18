"""Explicit, selection-specific consent for the existing sync selection flow."""
import json
from pathlib import Path
import unicodedata

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
)


DESTRUCTIVE_ACTIONS = frozenset({
    'upload_revision', 'upload_unknown', 'download_revision', 'download_unknown',
    'delete_local', 'delete_cloud',
})

ACTION_DIRECTIONS = {
    'upload_revision': 'OVERWRITE REMOTE with LOCAL',
    'upload_unknown': 'OVERWRITE REMOTE with LOCAL (mismatch / unknown history)',
    'download_revision': 'OVERWRITE LOCAL with REMOTE',
    'download_unknown': 'OVERWRITE LOCAL with REMOTE (mismatch / unknown history)',
    'delete_local': 'DELETE LOCAL',
    'delete_cloud': 'DELETE REMOTE',
    'upload_new': 'CREATE REMOTE from LOCAL',
    'download_new': 'CREATE LOCAL from REMOTE',
}


def selection_identity(selection):
    """Detect replaced rows, action objects and in-place edits across a Qt modal."""
    return tuple((row, id(action), tuple(sorted(vars(action).items())))
                 for row, action in selection)


def display_value(value):
    """Quote exact names without letting control/bidi metadata spoof the warning."""
    quoted = json.dumps(str(value), ensure_ascii=False)
    return ''.join(f'\\u{ord(char):04x}' if unicodedata.category(char) in ('Cc', 'Cf', 'Zl', 'Zp')
                   else char for char in quoted)


class DestructiveSyncDialog(QDialog):
    def __init__(self, selection, project_name, project_id, working_directory, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Confirm destructive sync changes')
        self.setObjectName('destructiveSyncConfirmation')
        self.setModal(True)
        self.resize(760, 520)
        self.confirmed = False
        self._finished = False
        layout = QVBoxLayout(self)
        destructive = sum(action.action.value in DESTRUCTIVE_ACTIONS for _, action in selection)
        self.warning = QLabel(
            f'{destructive} selected action(s) will overwrite or delete existing files.\n'
            'Local changes or the remote version may be lost. Review each direction and path below.\n'
            'Keep a separate backup. Sync is not an atomic project or CAD assembly update.\n'
            'Names are quoted; control and direction-changing characters are escaped.', self)
        self.warning.setTextFormat(Qt.PlainText)
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)
        root = Path(working_directory) / project_name
        lines = [f'LOCAL project folder: {display_value(root)}',
                 f'REMOTE project: {display_value(project_name)}',
                 f'REMOTE Drive folder ID: {display_value(project_id)}', '', 'EXACT SELECTED ACTIONS', '']
        for row, action in selection:
            direction = ACTION_DIRECTIONS[action.action.value]
            lines.extend((f'{direction}', f'Project-relative path: {display_value(action.path)}',
                          f'LOCAL path: {display_value(root / action.path)}'))
            if action.drive_id:
                lines.append(f'REMOTE file ID: {display_value(action.drive_id)}')
            lines.append('')
        self.details = QPlainTextEdit(self)
        self.details.setObjectName('destructiveSyncPaths')
        self.details.setReadOnly(True)
        self.details.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.details.setPlainText('\n'.join(lines))
        self.details.setAccessibleName('Selected file paths and overwrite or delete directions')
        layout.addWidget(self.details)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel, self)
        self.cancel_button = self.buttons.button(QDialogButtonBox.Cancel)
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(True)
        self.destructive_button = QPushButton('Overwrite / delete selected files', self)
        self.destructive_button.setObjectName('confirmDestructiveSync')
        self.destructive_button.setAutoDefault(False)
        self.destructive_button.setDefault(False)
        self.buttons.addButton(self.destructive_button, QDialogButtonBox.DestructiveRole)
        self.buttons.rejected.connect(self.reject)
        self.destructive_button.clicked.connect(self._confirm)
        layout.addWidget(self.buttons)
        self.cancel_button.setFocus(Qt.OtherFocusReason)

    def _confirm(self):
        if self._finished:
            return
        self._finished = True
        self.confirmed = True
        self.destructive_button.setEnabled(False)
        super().accept()

    def reject(self):
        self._finished = True
        self.confirmed = False
        super().reject()


def confirm_destructive_actions(parent, selection, project_name, project_id, working_directory):
    if not any(action.action.value in DESTRUCTIVE_ACTIONS for _, action in selection):
        return True
    dialog = DestructiveSyncDialog(selection, project_name, project_id, working_directory, parent)
    try:
        return dialog.exec() == QDialog.Accepted and dialog.confirmed
    finally:
        dialog.deleteLater()
