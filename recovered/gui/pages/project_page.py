from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame, QSizePolicy
from PySide6.QtCore import Qt
from gui.sync_status_card import SyncStatusCard
from gui.open_folder_button import OpenFolderButton
from gui.open_url_button import OpenURLButton
from core.signal_manager import signal_manager

class ProjectPage(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.app_controller = controller
        self.setMinimumWidth(400)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.project_title = QLabel(' ')
        self.project_title.setStyleSheet('\n                background-color: #191919;\n                color: #E0E0E0;\n                font-family: "Inter", "Segoe UI", "Helvetica Neue", sans-serif;\n                font-size: 24px;\n        ')
        self.project_title.setAlignment(Qt.AlignCenter)
        self.project_title.setWordWrap(True)
        layout.addSpacing(30)
        layout.addWidget(self.project_title)
        layout.addStretch()
        card_row = QHBoxLayout()
        self.download_card = SyncStatusCard(icon_path=':/assets/drive.png', title='Google Drive', description='download', on_click=signal_manager.download_card_clicked)
        self.upload_card = SyncStatusCard(icon_path=':/assets/local.png', title='Local Folder', description='upload', on_click=signal_manager.upload_card_clicked)
        card_row.addSpacing(20)
        card_row.addWidget(self.download_card)
        card_row.addSpacing(20)
        card_row.addWidget(self.upload_card)
        card_row.addSpacing(20)
        layout.addLayout(card_row)
        layout.addStretch()
        button_row = QHBoxLayout()
        self.open_drive_button = OpenURLButton('Open Google Drive')
        self.open_folder_button = OpenFolderButton('Open Local Folder')
        self.open_drive_button.setMaximumHeight(35)
        self.open_folder_button.setMaximumHeight(35)
        self.open_drive_button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.open_folder_button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        button_row.addSpacing(20)
        button_row.addWidget(self.open_drive_button)
        button_row.addSpacing(20)
        button_row.addWidget(self.open_folder_button)
        button_row.addSpacing(20)
        layout.addLayout(button_row)
        self.setLayout(layout)
        signal_manager.project_list_item_activated.connect(self.set_project_info)
        signal_manager.sync_count_updated.connect(self.set_status_card_numbers)

    def set_project_info(self, item, idx):
        name = item.data(Qt.UserRole)['name']
        path = self.app_controller.get_working_directory() / name
        link = item.data(Qt.UserRole)['id']
        self.project_title.setText(name)
        self.open_folder_button.update_folder(path)
        self.open_drive_button.update_link('https://drive.google.com/drive/u/2/folders/' + link)

    def set_status_card_numbers(self, download_count: int, upload_count: int):
        self.download_card.update_value(download_count)
        self.upload_card.update_value(upload_count)
