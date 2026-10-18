from PySide6.QtWidgets import QScrollArea, QWidget, QVBoxLayout, QWidget, QVBoxLayout, QPushButton, QFileDialog, QMessageBox, QCheckBox, QSizePolicy
from PySide6.QtWidgets import QLabel
from PySide6.QtCore import Qt, QSize
from app.app_version import APP_VERSION
from utils.app_settings import settings
from core.signal_manager import signal_manager
from pathlib import Path
import logging

class SettingsPage(QWidget):
    def __init__(self, controller=None, parent=None):
        super().__init__(parent)
        self.app_controller = controller
        self.setMinimumHeight(0)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.setStyleSheet('font-size: 12pt;')
        scroll_area = FixedScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        settings_widget = QWidget()
        settings_layout = QVBoxLayout(settings_widget)
        current_version_label = QLabel(f'Current Version: {APP_VERSION}')
        self.latest_version_label = QLabel('Latest Version: Checking...')
        self.update_button = QPushButton('Check for Updates')
        self.update_button.clicked.connect(self.on_update_button_clicked)
        project_directory_title = QLabel('Project Directory:')
        self.project_directory_label = QLabel(settings.get_working_directory().as_posix())
        change_project_directory_button = QPushButton('Browse...')
        change_project_directory_button.clicked.connect(self.on_change_working_directory_clicked)
        keep_rev_title = QLabel('Keep older file versions on Drive for 30 days or forever')
        keep_rev_checkbox = KeepRevisionCheckBox()
        tray_icon_title = QLabel('Leave CacheCAD open when you close this window. Close CacheCAD through the System Tray.')
        tray_icon_title.setWordWrap(True)
        tray_icon_checkbox = UseTrayIconCheckbox()
        icons_provided_by_label = QLabel("Icons provided by <a href='https://icons8.com/'>icons8</a>")
        icons_provided_by_label.setOpenExternalLinks(True)
        icons_provided_by_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        user_usage_label = QLabel('Update user data:')
        user_usage_label2 = QLabel('This is the only info collected on who is using CacheCAD and helps to determine which requests, features, and fixes to prioritize.')
        user_usage_label2.setWordWrap(True)
        user_usage_button = QPushButton('Update Usage Info')
        user_usage_button.clicked.connect(lambda: signal_manager.prompt_for_usage_data.emit())
        settings_widget.setLayout(settings_layout)
        settings_layout.addWidget(current_version_label)
        settings_layout.addWidget(self.latest_version_label)
        settings_layout.addWidget(self.update_button)
        settings_layout.addSpacing(20)
        settings_layout.addWidget(project_directory_title)
        settings_layout.addWidget(self.project_directory_label)
        settings_layout.addWidget(change_project_directory_button)
        settings_layout.addSpacing(20)
        settings_layout.addWidget(keep_rev_title)
        settings_layout.addWidget(keep_rev_checkbox)
        settings_layout.addSpacing(20)
        settings_layout.addWidget(tray_icon_title)
        settings_layout.addWidget(tray_icon_checkbox)
        settings_layout.addSpacing(20)
        settings_layout.addWidget(user_usage_label)
        settings_layout.addWidget(user_usage_button)
        settings_layout.addWidget(user_usage_label2)
        settings_layout.addSpacing(20)
        settings_layout.addWidget(icons_provided_by_label)
        settings_layout.addStretch()
        scroll_area.setWidget(settings_widget)
        main_layout = QVBoxLayout(self)
        settings_title = QLabel('Settings')
        settings_title.setStyleSheet('font-size: 20pt;')
        main_layout.addWidget(settings_title)
        main_layout.addWidget(scroll_area)
        self.setLayout(main_layout)
        signal_manager.latest_app_version.connect(self.update_latest_version)

    def update_latest_version(self, latest_version):
        if self.latest_version_label:
            self.latest_version_label.setText(f'Latest Version: {latest_version}')
        if latest_version == APP_VERSION:
            self.update_button.setText('Up to Date')
            self.update_button.setEnabled(False)
        else:
            self.update_button.setText('Download Update')
            self.update_button.setEnabled(True)

    def on_change_working_directory_clicked(self):
        old_directory = settings.get_working_directory()
        new_directory = Path(QFileDialog.getExistingDirectory(self, 'Select Project Directory', str(settings.get_working_directory())))
        if new_directory:
            if new_directory == old_directory or new_directory == Path('.'):
                return
            self.project_directory_label.setText(new_directory.as_posix())
            signal_manager.change_working_directory.emit(new_directory, old_directory, False)

    def on_update_button_clicked(self):
        signal_manager.download_app_update.emit()

class KeepRevisionCheckBox(QCheckBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        keep_setting = settings.get('keep_revision_forever')
        if not isinstance(keep_setting, bool):
            if isinstance(keep_setting, str):
                if keep_setting.lower() == 'true':
                    keep_setting = True
                    settings.set('keep_revision_forever', True)
            else:
                logging.error('Keep revision forever setting invalid, setting to False.')
                keep_setting = False
                settings.set('keep_revision_forever', False)
        self.setChecked(keep_setting)
        self.update_text()
        self.stateChanged.connect(self.change_keep_setting)
        self.setStyleSheet('\n            QCheckBox {\n                spacing: 10px;\n                font-size: 12px;\n            }\n            QCheckBox::indicator {\n                width: 20px;\n                height: 20px;\n            }\n            QCheckBox::indicator:checked {\n                background-color: #4CAF50;\n                border: 2px solid #388E3C;\n                border-radius: 4px;\n            }\n            QCheckBox::indicator:unchecked {\n                background-color: #FFEB3B;\n                border: 2px solid #FBC02D;\n                border-radius: 4px;\n            }\n        ')

    def change_keep_setting(self):
        settings.set('keep_revision_forever', self.isChecked())
        self.update_text()

    def update_text(self):
        self.setText('Forever' if self.isChecked() else '30 days')

class UseTrayIconCheckbox(QCheckBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        keep_setting = settings.get('use_tray_icon')
        if not isinstance(keep_setting, bool):
            if isinstance(keep_setting, str):
                if keep_setting.lower() == 'true':
                    keep_setting = True
                    settings.set('use_tray_icon', True)
            else:
                logging.error('use tray icon setting invalid, setting to False.')
                keep_setting = False
                settings.set('use_tray_icon', False)
        self.setChecked(keep_setting)
        self.update_text()
        self.stateChanged.connect(self.change_keep_setting)
        self.setStyleSheet('\n            QCheckBox {\n                spacing: 10px;\n                font-size: 12px;\n            }\n            QCheckBox::indicator {\n                width: 20px;\n                height: 20px;\n            }\n            QCheckBox::indicator:checked {\n                background-color: #4CAF50;\n                border: 2px solid #388E3C;\n                border-radius: 4px;\n            }\n            QCheckBox::indicator:unchecked {\n                background-color: #FFEB3B;\n                border: 2px solid #FBC02D;\n                border-radius: 4px;\n            }\n        ')

    def change_keep_setting(self):
        settings.set('use_tray_icon', self.isChecked())
        self.update_text()

    def update_text(self):
        self.setText('Yes' if self.isChecked() else 'No')

class FixedScrollArea(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)

    def sizeHint(self):
        return QSize(600, 400)
