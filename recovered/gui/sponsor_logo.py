from PySide6.QtWidgets import QPushButton, QMenu
from PySide6.QtGui import QIcon, QPixmap, QDesktopServices, QAction
from PySide6.QtCore import QUrl, Qt
from core.signal_manager import signal_manager


class SponsorLogo(QPushButton):
    def __init__(self):
        super().__init__()
        pix = QPixmap(":/assets/sponsor.png")
        icon = QIcon(pix)
        self.setIcon(icon)
        self.setIconSize(pix.size())
        self.setFlat(True)
        self.setMaximumHeight(60)
        self.setStyleSheet("\n        QPushButton {\n            background-color: #191919;\n            color: #000000;\n            border: none;\n            padding: 5px 10px;\n            border-radius: 4px;\n            font-weight: bold;\n            font-size: 16px;\n            }                   \n        ")
        self.init_menu()
        self.setToolTip("This project is supported by West Coast Products. Visit https://wcproducts.com/.")

    def init_menu(self):
        self.menu = QMenu(self)
        self.menu.setStyleSheet("\n            QMenu {\n                background-color: #191919;\n                color: white;\n                border: 1px solid black;\n                font-size: 14px\n            }\n            QMenu::item {\n                background-color: #191919;\n            }\n            QMenu::item:selected { /* when user selects item using mouse or keyboard */\n                background-color: #303030;\n            }\n        ")
        self.action_donate = QAction("Donate to support CacheCAD", self)
        self.action_open_website = QAction("Open WCProducts.com", self)
        self.action_open_belt_calculator = QAction("Open Belt Calculator", self)
        self.action_donate.triggered.connect(lambda: self.open_url("https://www.buymeacoffee.com/cachecad"))
        self.action_open_website.triggered.connect(lambda: self.open_url("https://wcproducts.com/"))
        self.action_open_belt_calculator.triggered.connect(lambda: self.open_url("https://wcproducts.com/pages/calculator-belt"))
        self.menu.addAction(self.action_donate)
        self.menu.addAction(self.action_open_website)
        self.menu.addAction(self.action_open_belt_calculator)

    def show_menu(self, pos):
        self.menu.exec(self.mapToGlobal(pos))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.show_menu(event.pos())
        super().mousePressEvent(event)

    def open_url(self, URL):
        QDesktopServices.openUrl(QUrl(URL))
