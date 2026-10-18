"""Real application's account controls. No test success or client setup UI."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QMenu, QMessageBox


class ApplicationAccount(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.manager = controller.auth_manager
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.identity_label = QLabel()
        self.identity_label.setTextFormat(Qt.PlainText)
        self.identity_label.setWordWrap(True)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.PlainText)
        self.status_label.setWordWrap(True)
        self.button = QPushButton('Sign in with Google')
        self.button.clicked.connect(controller.on_signin_clicked)
        self.cancel_button = QPushButton('Cancel')
        self.cancel_button.clicked.connect(self.manager.cancel)
        buttons = QHBoxLayout()
        buttons.addWidget(self.button)
        buttons.addWidget(self.cancel_button)
        layout.addWidget(self.identity_label)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)
        self.menu = QMenu(self)
        self.signout_action = self.menu.addAction('Sign out on this computer')
        self.signout_action.triggered.connect(controller.on_local_signout)
        self.disconnect_action = self.menu.addAction('Disconnect Google access…')
        self.disconnect_action.triggered.connect(controller.on_disconnect_clicked)
        self.manager.status_changed.connect(self.update_state)
        self.update_state(self.manager.state, self.manager.message)

    def update_state(self, state, message):
        identity = self.manager.identity
        self.identity_label.setText(identity.email if identity is not None else '')
        self.status_label.setText(message)
        busy = state in ('authorize', 'restore', 'sign_out', 'disconnect', 'cancelling')
        self.cancel_button.setVisible(state in ('authorize', 'restore', 'cancelling'))
        self.cancel_button.setEnabled(state != 'cancelling')
        self.button.setEnabled(not busy and state not in ('setup_missing', 'restart_required'))
        self.button.setMenu(self.menu if state == 'signed_in' else None)
        self.button.setText('Google account' if state == 'signed_in' else
                            'Retry forgetting this computer' if state == 'cleanup_required' else
                            'Restart required' if state == 'restart_required' else
                            'Setup required' if state == 'setup_missing' else
                            'Try sign-in again' if state == 'error' else 'Sign in with Google')
        active_transfer = self.controller.sync_executing
        self.signout_action.setEnabled(not active_transfer)
        self.disconnect_action.setEnabled(not active_transfer)


def confirm_disconnect(parent):
    return QMessageBox.question(parent, 'Disconnect Google access?',
        'Revoke this application’s Google access and remove its saved sign-in on this computer? '
        'Existing Google Drive files are not deleted. Other installations using the same grant may need to sign in again. '
        'To keep Google access and only forget this computer, choose Sign out instead.',
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes
