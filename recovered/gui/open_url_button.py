from PySide6.QtWidgets import QPushButton
from PySide6.QtGui import QIcon, QPixmap, QDesktopServices
from PySide6.QtCore import QUrl
from utils.paths import RESOURCES_DIR


class OpenURLButton(QPushButton):
    def __init__(self, label, url: str = None, icon_path=None, parent=None):
        super().__init__(label, parent)
        self.url = url
        if icon_path:
            pix = QPixmap(icon_path)
            self.setIcon(QIcon(pix))
            self.setIconSize(pix.size())
        self.clicked.connect(self.open_url)

    def update_link(self, url):
        self.url = url

    def open_url(self):
        QDesktopServices.openUrl(QUrl(self.url))
