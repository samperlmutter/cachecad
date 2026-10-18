from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame, QPushButton
from PySide6.QtCore import Qt, Signal
from gui.button_bar import ButtonBar
from gui.sync_selection_tree import SyncSelectionTreeView
from gui.open_folder_button import OpenFolderButton
from gui.open_url_button import OpenURLButton
from core.signal_manager import signal_manager
from PySide6.QtWidgets import QProgressBar
import logging

class SelectionPage(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.app_controller = controller
        self.setMinimumWidth(400)
        main_frame = QFrame()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.project_title = QLabel(' ')
        self.project_title.setStyleSheet('\n                background-color: #191919;\n                color: #E0E0E0;\n                font-family: "Inter", "Segoe UI", "Helvetica Neue", sans-serif;\n                font-size: 24px;\n        ')
        self.project_title.setAlignment(Qt.AlignCenter)
        self.project_title.setWordWrap(True)
        self.download_text = 'Download Changes'
        self.upload_text = 'Upload Changes'
        layout.addSpacing(30)
        layout.addWidget(self.project_title)
        button_row = QHBoxLayout()
        button_row.setSpacing(10)
        button_row.setContentsMargins(20, 10, 20, 10)
        self.select_all_button = QPushButton('Select All')
        self.execute_sync_button = QPushButton('Changes')
        self.back_button = QPushButton('Back')
        button_row.addWidget(self.select_all_button)
        button_row.addWidget(self.execute_sync_button)
        button_row.addWidget(self.back_button)
        layout.addLayout(button_row)
        self.selection_tree = SyncSelectionTreeView()
        layout.addWidget(self.selection_tree)
        self.setLayout(layout)
        self.execute_sync_button.clicked.connect(self.on_execute_sync_clicked)
        self.back_button.clicked.connect(signal_manager.back_clicked.emit)
        self.select_all_button.clicked.connect(self.select_all)
        signal_manager.sync_cancelled.connect(self.on_sync_cancelled)
        signal_manager.action_complete.connect(self.update_progress_bar)
        signal_manager.action_failed.connect(self.update_progress_bar)
        signal_manager.sync_execution_finished.connect(self.hide_progress_bar)
        signal_manager.sync_execution_finished.connect(self.set_back_button_to_back)
        signal_manager.project_list_item_activated.connect(self.set_project_info)
        signal_manager.download_card_clicked.connect(self.set_button_to_download)
        signal_manager.upload_card_clicked.connect(self.set_button_to_upload)
        signal_manager.disable_buttons.connect(self.on_disable_buttons)
        signal_manager.enable_buttons.connect(self.on_enable_buttons)
        signal_manager.project_read_only.connect(self.on_project_read_only)

    def set_project_info(self, item, idx):
        name = item.data(Qt.UserRole)['name']
        self.project_title.setText(name)

    def set_button_to_download(self):
        self.execute_sync_button.setText(self.download_text)
        self.execute_sync_button.setEnabled(True)

    def set_button_to_upload(self):
        self.execute_sync_button.setText(self.upload_text)
        self.execute_sync_button.setEnabled(True)

    def on_execute_sync_clicked(self):
        self.sync_started = True
        action_list = self.selection_tree.get_selected_actions()
        self.show_progress_bar(len(action_list))
        print(f'Found {len(action_list)} actions selected for sync.')
        self.set_back_button_to_cancel()
        signal_manager.execute_sync.emit(action_list)

    def set_back_button_to_cancel(self):
        self.back_button.setText('Cancel')

    def set_back_button_to_back(self):
        self.back_button.setText('Back')
        self.back_button.setEnabled(True)

    def show_progress_bar(self, total_actions: int):
        if not hasattr(self, 'progress_bar'):
            self.progress_bar = QProgressBar()
            self.progress_bar.setRange(0, total_actions)
            self.layout().addWidget(self.progress_bar)
        self.progress_bar.show()

    def hide_progress_bar(self):
        if hasattr(self, 'progress_bar'):
            self.progress_bar.hide()
            self.layout().removeWidget(self.progress_bar)
            self.progress_bar.setParent(None)
            del self.progress_bar

    def update_progress_bar(self):
        if hasattr(self, 'progress_bar') and self.app_controller.sync_executing:
            current_value = self.progress_bar.value()
            self.progress_bar.setValue(current_value + 1)

    def on_disable_buttons(self):
        self.execute_sync_button.setEnabled(False)
        self.select_all_button.setEnabled(False)

    def on_enable_buttons(self):
        self.execute_sync_button.setEnabled(True)
        self.select_all_button.setEnabled(True)

    def select_all(self):
        signal_manager.check_all.emit()

    def on_sync_cancelled(self):
        self.back_button.setDisabled(True)

    def on_project_read_only(self):
        logging.info('Project is read-only, disabling upload button.')
        self.execute_sync_button.setDisabled(True)
        self.execute_sync_button.setText('Read-Only')
