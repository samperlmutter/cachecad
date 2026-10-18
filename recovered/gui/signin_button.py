from PySide6.QtWidgets import QPushButton, QSizePolicy, QMenu
from PySide6.QtGui import QPixmap, QIcon, QAction, QFontMetrics
from PySide6.QtCore import Qt
from core.signal_manager import signal_manager


class SignInButton(QPushButton):
    def __init__(self, app_controller=None, parent=None):
        super().__init__("Sign In", parent)
        self.app_controller = app_controller
        self.signed_in = False
        self.setMinimumWidth(152)
        self.clicked.connect(self.btn_clicked)
        signal_manager.user_profile_loaded.connect(self.set_btn_to_signout)
        signal_manager.has_internet.connect(self.on_internet_check)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        self.no_internet_style = "QPushButton { border-radius: 5px; background-color: #191919; padding: 9px; font-family: 'Inter'; font-weight: bold; font-size: 12px; color: white; text-align: left}QPushButton::hover { border-radius: 5px; background-color: #303030; padding: 9px; font-family: 'Inter'; font-weight: bold; font-size: 10px}; text-align: left"
        self.signout_style = "QPushButton { border-radius: 5px; background-color: #191919; padding: 4px; font-family: 'Inter'; font-weight: normal; font-size: 14px; color: white; text-align: left}QPushButton::hover { border-radius: 5px; background-color: #303030; padding: 4px; font-family: 'Inter'; font-weight: normal; font-size: 14px}; text-align: left"
        self.signin_style = "QPushButton { border-radius: 5px; background-color: #4285F4; padding: 2px; font-family: 'Inter'; font-weight: bold; font-size: 14px}"
        self.signin_style_disable = "QPushButton { border-radius: 5px; background-color: #696969; padding: 2px; font-family: 'Inter'; font-weight: bold; font-size: 14px}"
        self.signin_text = ""
        self.signout_text = "Signed in as \n"
        self.signout_text_and_email = None
        self.no_internet_text = "  Cannot reach Google Server\n  Click to check connection"
        self.pix_signin = QPixmap(":/assets/signin.png")
        self.pix_internet = QPixmap(":/assets/internet.png")
        self.setMinimumHeight(60)
        self.setMaximumHeight(60)
        self.menu = None
        self.set_btn_to_signin()

    def btn_clicked(self):
        if not self.signed_in:
            signal_manager.sign_in_clicked.emit()

    def signout_clicked(self):
        self.signed_in = False
        self.set_btn_to_signin()
        signal_manager.sign_in_clicked.emit()

    def set_btn_to_signin(self):
        self.menu = None
        self.setText(" ")
        self.setIcon(QIcon(self.pix_signin))
        self.setIconSize(self.pix_signin.size())
        self.setStyleSheet(self.signin_style)

    def set_btn_to_no_internet(self):
        self.menu = None
        self.setStyleSheet(self.no_internet_style)
        self.setIcon(QIcon(self.pix_internet))
        self.setIconSize(self.pix_internet.size())
        self.setText("  Cannot reach Google Server\n  Click to check connection")

    def set_btn_to_signout(self, user, pixmap):
        self.signed_in = True
        if self.menu is None:
            self.init_menu()
        self.signout_text_and_email = self.signout_text + user["email"]
        self.setText(self.signout_text_and_email)
        self.setIcon(QIcon(pixmap))
        self.setIconSize(pixmap.size())
        self.setStyleSheet(self.signout_style)
        self.update_button_text()

    def on_internet_check(self, has_internet: bool):
        if not has_internet:
            self.signed_in = False
            self.set_btn_to_no_internet()
        else:
            self.set_btn_to_signin()

    def init_menu(self):
        self.menu = QMenu(self)
        self.menu.setStyleSheet("\n            QMenu {\n                background-color: #191919;\n                color: white;\n                border: 1px solid black;\n                font-size: 14px\n            }\n            QMenu::item {\n                background-color: #191919;\n            }\n            QMenu::item:selected { /* when user selects item using mouse or keyboard */\n                background-color: #303030;\n            }\n        ")
        self.action_signout = QAction("Signout", self)
        self.action_signout.triggered.connect(self.signout_clicked)
        self.menu.addAction(self.action_signout)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.signed_in == True:
            self.show_menu(event.pos())
        super().mousePressEvent(event)

    def show_menu(self, pos):
        self.menu.exec(self.mapToGlobal(pos))

    def update_button_text(self):
        metrics = QFontMetrics(self.font())
        available_width = self.width() + 20
        elided_text = metrics.elidedText(self.signout_text_and_email, Qt.TextElideMode.ElideRight, available_width)
        self.setText(elided_text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.signed_in:
            self.update_button_text()
