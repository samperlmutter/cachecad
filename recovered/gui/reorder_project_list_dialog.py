from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QHBoxLayout, QPushButton
from .project_list_widget import ProjectListWidget
from core.signal_manager import signal_manager


class ReorderProjectListDialog(QDialog):
    def __init__(self, app_controller):
        super().__init__()
        self.setWindowTitle("Reorder Projects")
        self.app_controller = app_controller
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Reorder your projects:"))
        self.project_list = ProjectListWidget(app_controller)
        layout.addWidget(self.project_list)
        btn_layout = QHBoxLayout()
        self.up_button = QPushButton("Move Up")
        self.down_button = QPushButton("Move Down")
        self.close_button = QPushButton("Close")
        btn_layout.addWidget(self.up_button)
        btn_layout.addWidget(self.down_button)
        btn_layout.addWidget(self.close_button)
        layout.addLayout(btn_layout)
        self.up_button.clicked.connect(self._on_move_up)
        self.down_button.clicked.connect(self._on_move_down)
        self.close_button.clicked.connect(self.accept)
        self.project_list.currentRowChanged.connect(self._update_button_states)
        self._update_button_states()

    def _update_button_states(self):
        index = self.project_list.currentRow()
        count = self.project_list.count()
        self.up_button.setEnabled(index > 0)
        self.down_button.setEnabled(0 <= index < count - 1)

    def _on_move_up(self):
        index = self.project_list.currentRow()
        signal_manager.move_project_up_clicked.emit(index)
        self.project_list.populate_projects()
        self.project_list.setCurrentRow(index - 1)

    def _on_move_down(self):
        index = self.project_list.currentRow()
        signal_manager.move_project_down_clicked.emit(index)
        self.project_list.populate_projects()
        self.project_list.setCurrentRow(index + 1)
