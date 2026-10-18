from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QHBoxLayout, QPushButton
from gui.project_list_widget import ProjectListWidget
from core.signal_manager import signal_manager


class RemoveProjectDialog(QDialog):
    def __init__(self, app_controller):
        super().__init__()
        self.setWindowTitle("Remove Project")
        self.app_controller = app_controller
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select a project to remove. This will not delete any files"))
        self.project_list = ProjectListWidget(app_controller)
        layout.addWidget(self.project_list)
        btn_layout = QHBoxLayout()
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setEnabled(False)
        self.cancel_btn = QPushButton("Cancel")
        btn_layout.addWidget(self.remove_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)
        self.project_list.currentRowChanged.connect(self.on_selection_changed)
        self.remove_btn.clicked.connect(self._on_remove_clicked)
        self.cancel_btn.clicked.connect(self.reject)

    def on_selection_changed(self, current_row):
        self.remove_btn.setEnabled(current_row >= 0)

    def _on_remove_clicked(self):
        index = self.get_selected_index()
        signal_manager.remove_project_clicked.emit(index)
        self.project_list.populate_projects()

    def get_selected_index(self):
        idx = self.project_list.currentIndex()
        if idx.isValid():
            return self.project_list.currentRow()
        else:
            return None
