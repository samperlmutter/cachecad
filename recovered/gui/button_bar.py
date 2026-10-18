from PySide6.QtWidgets import QWidget, QVBoxLayout, QPushButton, QSizePolicy
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtCore import Signal, Qt
from core.signal_manager import signal_manager
from utils.styles import DISABLED_BUTTON, ENABLED_BUTTON


class ButtonBar(QWidget):
    refresh_clicked = Signal()
    options_clicked = Signal()
    settings_clicked = Signal()
    bug_clicked = Signal()

    def __init__(self, app_controller, parent=None):
        super().__init__(parent)
        self.setMaximumWidth(50)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(0, 0, 0, 0)
        self.refresh_btn = self._make_button(":/assets/refresh.png", signal_manager.refresh_clicked)
        self.options_btn = self._make_button(":/assets/options.png", signal_manager.options_clicked)
        self.settings_btn = self._make_button(":/assets/settings.png", signal_manager.settings_clicked)
        self.bug_btn = self._make_button(":/assets/bug.png", signal_manager.bug_clicked)
        layout.addStretch(5)
        for btn in (self.refresh_btn, self.options_btn, self.settings_btn, self.bug_btn):
            layout.addWidget(btn)
        layout.addStretch(2)
        signal_manager.enable_buttons.connect(self.enable_btns)
        signal_manager.disable_buttons.connect(self.disable_btns)

    def _make_button(self, pix_string, signal: Signal):
        btn = QPushButton()
        pix = QPixmap(pix_string)
        icon = QIcon(pix)
        btn.setIcon(icon)
        btn.setIconSize(pix.size())
        btn.setFlat(True)
        btn.clicked.connect(signal.emit)
        btn.setMaximumHeight(50)
        btn.setMaximumWidth(50)
        return btn

    def disable_btns(self):
        for btn in (self.refresh_btn, self.options_btn, self.settings_btn, self.bug_btn):
            self.setStyleSheet(DISABLED_BUTTON)

    def enable_btns(self):
        for btn in (self.refresh_btn, self.options_btn, self.settings_btn, self.bug_btn):
            self.setStyleSheet(ENABLED_BUTTON)
