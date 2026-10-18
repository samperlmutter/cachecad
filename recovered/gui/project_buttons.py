from PySide6.QtWidgets import QPushButton
from gui.create_project_dialog import CreateProjectDialog
from gui.remove_project_dialog import RemoveProjectDialog
from gui.reorder_project_list_dialog import ReorderProjectListDialog
from core.signal_manager import signal_manager
import logging


class NewProjectButton(QPushButton):
    def __init__(self, app_controller, parent=None):
        super().__init__('New Project', parent)
        self.app_controller = app_controller
        self.clicked.connect(self.launch_dialog)
        signal_manager.enable_buttons.connect(self.on_enable_buttons)
        signal_manager.disable_buttons.connect(self.on_disable_buttons)

    def launch_dialog(self):
        dialog = CreateProjectDialog(self.app_controller)
        dialog.exec()

    def on_disable_buttons(self):
        self.setDisabled(True)

    def on_enable_buttons(self):
        self.setEnabled(True)


class RemoveProjectButton(QPushButton):
    def __init__(self, app_controller, parent=None):
        super().__init__('Remove', parent)
        self.app_controller = app_controller
        self.clicked.connect(self.launch_dialog)
        signal_manager.enable_buttons.connect(self.on_enable_buttons)
        signal_manager.disable_buttons.connect(self.on_disable_buttons)

    def launch_dialog(self):
        dialog = RemoveProjectDialog(self.app_controller)
        dialog.exec()

    def on_disable_buttons(self):
        self.setDisabled(True)

    def on_enable_buttons(self):
        self.setEnabled(True)


class ReorderProjectButton(QPushButton):
    def __init__(self, app_controller, parent=None):
        super().__init__('Reorder', parent)
        self.app_controller = app_controller
        self.clicked.connect(self.launch_dialog)
        signal_manager.enable_buttons.connect(self.on_enable_buttons)
        signal_manager.disable_buttons.connect(self.on_disable_buttons)

    def launch_dialog(self):
        dialog = ReorderProjectListDialog(self.app_controller)
        dialog.exec()

    def on_disable_buttons(self):
        self.setDisabled(True)

    def on_enable_buttons(self):
        self.setEnabled(True)
