# Reconstructed from the original Python 3.12 code object; comments and formatting are not recovered.
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QStackedWidget,
    QLabel, QGroupBox, QSizePolicy, QSystemTrayIcon, QMenu, QApplication,
)
from PySide6.QtCore import QDir, QUrl, Qt, Signal
from PySide6.QtGui import QPixmap, QIcon, QAction
from gui.project_list_widget import ProjectListWidget
from gui.signin_button import SignInButton
from gui.cc_logo import CCLogo
from gui.open_folder_button import OpenFolderButton
from gui.project_buttons import NewProjectButton, RemoveProjectButton, ReorderProjectButton
from gui.page import Page
from gui.sponsor_logo import SponsorLogo
from core.signal_manager import signal_manager
from gui.pages.home_page import HomePage
from gui.pages.settings_page import SettingsPage
from gui.pages.project_page import ProjectPage
from gui.pages.selection_page import SelectionPage
from utils.app_settings import settings
from utils.styles import DARK_MODE_STYLESHEET
from app.app_version import APP_VERSION, APP_IN_PRODUCTION, APP_IS_BETA, APP_BUILD_DATE
import logging


class MainWindow(QMainWindow):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.app_controller = controller
        self.setStyleSheet(DARK_MODE_STYLESHEET)
        self.setWindowTitle(self.get_window_title_string())
        pix = QPixmap(':/assets/window.png')
        self.setWindowIcon(QIcon(pix))
        self.init_tray_icon()
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        left_frame = QWidget()
        left_layout = QVBoxLayout(left_frame)
        left_layout.setSpacing(5)
        left_layout.setContentsMargins(0, 0, 0, 0)
        row1 = QHBoxLayout()
        self.signin_button = SignInButton(self.app_controller)
        row1.addWidget(self.signin_button)
        left_layout.addLayout(row1)
        row2 = QHBoxLayout()
        self.new_project_button = NewProjectButton(self.app_controller)
        self.remove_project_button = RemoveProjectButton(self.app_controller)
        self.reorder_project_button = ReorderProjectButton(self.app_controller)
        row2.addWidget(self.new_project_button)
        row2.addWidget(self.remove_project_button)
        row2.addWidget(self.reorder_project_button)
        left_layout.addLayout(row2)
        self.group_box = QGroupBox('Projects')
        group_layout = QVBoxLayout(self.group_box)
        self.project_list_widget = ProjectListWidget(self.app_controller)
        group_layout.setSpacing(0)
        group_layout.setContentsMargins(5, 5, 0, 0)
        group_layout.addWidget(self.project_list_widget)
        left_layout.addWidget(self.group_box)
        sponsor_layout = QHBoxLayout()
        self.sponsor_label = QLabel('Supported by')
        self.sponsor_label.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
        self.sponsor_label.setStyleSheet('color: white; font-size: 12px')
        self.sponsor_logo = SponsorLogo()
        left_layout.addSpacing(10)
        left_layout.addWidget(self.sponsor_label)
        left_layout.addWidget(self.sponsor_logo)
        left_layout.addSpacing(20)
        row4 = QHBoxLayout()
        self.settings_btn = self._make_button(':/assets/settings.png', signal_manager.settings_clicked)
        self.bug_btn = self._make_button(':/assets/bug.png', signal_manager.bug_clicked)
        self.open_cachecad_folder_button = OpenFolderButton('  CacheCAD', controller.get_working_directory(), ':/assets/folder.png')
        signal_manager.bug_report.connect(self.on_bug_report)
        signal_manager.error_occurred.connect(self.on_error_occurred)
        signal_manager.disable_buttons.connect(self.on_disable_buttons)
        signal_manager.enable_buttons.connect(self.on_enable_buttons)
        row4.addWidget(self.settings_btn)
        row4.addWidget(self.bug_btn)
        row4.addWidget(self.open_cachecad_folder_button)
        left_layout.addLayout(row4)
        self.open_cachecad_folder_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        left_layout.setStretch(0, 0)
        left_layout.setStretch(1, 0)
        left_layout.setStretch(2, 1)
        left_layout.setStretch(3, 0)
        main_layout.addWidget(left_frame)
        right_frame = QWidget()
        right_frame.setContentsMargins(0, 0, 0, 0)
        self.main_stack = QStackedWidget()
        self.main_stack.setContentsMargins(0, 0, 0, 0)
        right_layout = QVBoxLayout(right_frame)
        right_layout.setSpacing(0)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.pages = {
            Page.HOME: HomePage(controller=self.app_controller),
            Page.SETTINGS: SettingsPage(controller=self.app_controller),
            Page.PROJECT: ProjectPage(controller=self.app_controller),
            Page.SELECTION: SelectionPage(controller=self.app_controller),
        }
        self.page_list = []
        for page_enum, page_widget in self.pages.items():
            self.main_stack.addWidget(page_widget)
            self.page_list.append(page_enum)
        right_layout.addWidget(self.main_stack)
        main_layout.addWidget(right_frame)
        main_layout.setStretch(0, 2)
        main_layout.setStretch(1, 5)
        self.corner_logo = CCLogo(14, Qt.AlignRight, parent=central_widget)
        self.corner_logo.setVisible(False)
        signal_manager.page_change.connect(self.set_page)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.corner_logo.move(self.width() - self.corner_logo.width() - 10, 10)

    def set_controller(self, controller):
        self.app_controller = controller

    def show_status_message(self, message: str, duration_ms: int = 3000):
        self.statusBar().showMessage(message, duration_ms)

    def set_page(self, page):
        if isinstance(page, Page):
            new_index = page.value
        elif isinstance(page, int) and 0 <= page < self.main_stack.count():
            new_index = page
        elif isinstance(page, str):
            try:
                new_index = Page[page.upper()].value
            except KeyError:
                print(f'Invalid page name: {page}')
                return
        else:
            print(f'Invalid page input: {page}')
            return
        current_index = self.main_stack.currentIndex()
        if current_index != new_index:
            self._last_page_index = current_index
        print('Switching to page ' + str(new_index))
        self.main_stack.setCurrentIndex(new_index)
        if new_index == Page.HOME.value:
            self.corner_logo.setVisible(False)
            print('Hide Logo')
        else:
            self.corner_logo.setVisible(True)
            print('Show Logo')

    def temp_test_function(self):
        self.set_page(Page.HOME)

    def previous_page(self):
        self.set_page(self._last_page_index)

    def _make_button(self, pix_string, signal: Signal):
        btn = QPushButton()
        pix = QPixmap(pix_string)
        icon = QIcon(pix)
        btn.setIcon(icon)
        btn.setIconSize(pix.size())
        btn.setFlat(True)
        btn.clicked.connect(signal.emit)
        btn.setMaximumHeight(35)
        btn.setMaximumWidth(40)
        return btn

    def get_window_title_string(self):
        title = f'CacheCAD v{APP_VERSION}'
        if APP_IS_BETA:
            title += f' - (Beta {APP_BUILD_DATE})'
        return title

    def init_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(QIcon(':/assets/window.png'))
        self.tray_icon_notifier = True
        self.tray_icon.show()
        menu = QMenu()
        show_action = QAction('Open', self)
        exit_action = QAction('Exit', self)
        self.tray_icon.activated.connect(self.show)
        show_action.triggered.connect(self.show_app)
        exit_action.triggered.connect(self.exit_app)
        menu.addAction(show_action)
        menu.addAction(exit_action)
        self.tray_icon.setContextMenu(menu)

    def closeEvent(self, event):
        self.setVisible(False)
        use_tray = settings.get_use_tray_icon()
        use_tray = use_tray and not getattr(self, '_exit_requested', False)
        if self.tray_icon_notifier and use_tray:
            self.tray_icon.showMessage('CacheCAD', 'CacheCAD is still running.\nYou can open it from the system tray', QIcon(':/assets/window.png'), 4000)
            self.tray_icon_notifier = False
        if use_tray:
            event.ignore()
        else:
            # Qt already closes windows while handling Quit. Do not re-enter it.
            event.accept()

    def show_app(self):
        self.show()
        self.activateWindow()

    def exit_app(self):
        if getattr(self, '_exit_requested', False):
            return
        self._exit_requested = True
        self.tray_icon.hide()
        self.tray_icon.deleteLater()
        QApplication.quit()

    def on_error_occurred(self):
        logging.error('ERROR OCCURRED. UPDATING BUG ICON')
        pix = QPixmap(':/assets/alert.png')
        icon = QIcon(pix)
        self.bug_btn.setIcon(icon)

    def on_bug_report(self):
        pix = QPixmap(':/assets/bug.png')
        icon = QIcon(pix)
        self.bug_btn.setIcon(icon)

    def on_disable_buttons(self):
        self.settings_btn.setEnabled(False)

    def on_enable_buttons(self):
        self.settings_btn.setEnabled(True)
