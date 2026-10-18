from PySide6.QtWidgets import QListWidget, QListWidgetItem
from PySide6.QtCore import Signal, Qt
from core.signal_manager import signal_manager


class ProjectListWidget(QListWidget):
    def __init__(self, app_controller):
        super().__init__()
        self.app_controller = app_controller
        self.populate_projects()
        self.itemActivated.connect(self._handle_activation)
        signal_manager.enable_buttons.connect(self.on_enable_buttons)
        signal_manager.disable_buttons.connect(self.on_disable_buttons)

    def populate_projects(self, projects=None):
        self.clear()
        if not projects:
            projects = self.app_controller.get_project_list()
        if not projects:
            return
        for idx, (name, project_id, *rest) in enumerate(projects):
            read_only = rest[0] if rest else False
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, {
                "name": name,
                "id": project_id,
                "read_only": read_only,
                "idx": idx
            })
            self.addItem(item)

    def _handle_activation(self, item):
        index = self.row(item)
        signal_manager.project_list_item_activated.emit(item, index)
        print("Selected " + item.data(Qt.UserRole)["name"])

    def on_disable_buttons(self):
        self.setDisabled(True)

    def on_enable_buttons(self):
        self.setEnabled(True)
