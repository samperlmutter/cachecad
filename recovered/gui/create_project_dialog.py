from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QPushButton, QCheckBox, QVBoxLayout, QHBoxLayout, QSpacerItem, QSizePolicy
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices


class CreateProjectDialog(QDialog):
    def __init__(self, app_controller):
        super().__init__()
        self.app_controller = app_controller
        self.setWindowTitle("New Project")
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout()
        layout.addWidget(QLabel("Paste a link to an existing folder on Google Drive"))
        self.link_input = QLineEdit()
        layout.addWidget(self.link_input)
        read_only_layout = QHBoxLayout()
        read_only_label = QLabel("Make Project Read Only:")
        self.read_only_checkbox = QCheckBox()
        self.read_only_checkbox.setChecked(False)
        tooltip_text = "<p style='white-space:pre-wrap; width: 250px;'>If checked, you will not be able to upload changes to this project. This is useful if you don't have write access to the Google Drive folder or want to avoid accidental changes.</p>"
        self.read_only_checkbox.setToolTip(tooltip_text)
        read_only_layout.addWidget(read_only_label)
        read_only_layout.addWidget(self.read_only_checkbox)
        layout.addLayout(read_only_layout)
        button_layout = QHBoxLayout()
        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self._on_ok_clicked)
        layout.addWidget(ok_button)
        open_button = QPushButton("Open Drive")
        open_button.clicked.connect(self._on_open_drive_clicked)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(open_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)
        self.setLayout(layout)

    def _on_ok_clicked(self):
        link = self.link_input.text().strip()
        if not link:
            self._show_error("Please paste a link.")
            return
        success = self.app_controller.create_project_from_link(link, self.read_only_checkbox.isChecked())
        if isinstance(success, str):
            self._show_error(success)
        if isinstance(success, bool):
            if success:
                self.accept()
            if not success:
                self._show_error("Could not create project.")

    def _on_open_drive_clicked(self):
        QDesktopServices.openUrl(QUrl("https://drive.google.com/drive/my-drive"))

    def _show_error(self, message):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(self, "Error", message)
