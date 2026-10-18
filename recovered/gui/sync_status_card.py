from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame, QSizePolicy
from PySide6.QtGui import QPixmap, QMouseEvent, QCursor, QMovie
from PySide6.QtCore import Qt, Signal
from core.signal_manager import signal_manager
import random
import logging


class SyncStatusCard(QFrame):
    def __init__(self, icon_path: str, title: str, description: str, on_click: Signal, parent=None):
        super().__init__(parent)
        self.on_click = on_click
        self.description = description
        self.singular_description = "File to " + description
        self.plural_description = "Files to " + description
        self.button_enabled = True
        self.setMinimumHeight(300)
        self.setFrameShape(QFrame.Box)
        self.setLineWidth(1)
        self.setObjectName("SyncCard")
        self.not_synced_style = "\n            QFrame#SyncCard {\n                border: 2px solid #C0C0C0;\n                border-radius: 8px;\n                padding: 8px;\n                background-color: #191919;\n            }\n            QFrame#SyncCard:hover {\n                border: 3px solid #E0E0E0;\n            }\n            QLabel {\n                background-color: transparent;\n            }\n        "
        self.synced_style = "\n            QFrame#SyncCard {\n                border: 2px solid #606060;\n                border-radius: 8px;\n                padding: 8px;\n                background-color: #191919;\n            }\n            QFrame#SyncCard:hover {\n                border: 2px solid #606060;\n            }\n            QLabel {\n                background-color: transparent;\n            }\n        "
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 0)
        top_row = QHBoxLayout()
        icon_label = QLabel()
        icon_label.setPixmap(QPixmap(icon_path).scaled(50, 50, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        title_label = QLabel(title)
        title_label.setStyleSheet("font-weight: bold;")
        title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        top_row.addStretch()
        top_row.addWidget(icon_label)
        top_row.addWidget(title_label)
        top_row.addStretch()
        top_row.setSpacing(20)
        layout.addLayout(top_row)
        layout.addStretch(5)
        self.synced_icon_label = QLabel()
        self.synced_icon_label.setAlignment(Qt.AlignCenter)
        self.synced_icon_label.setPixmap(QPixmap(":/assets/synced.png"))
        layout.addWidget(self.synced_icon_label)
        self.loading_gif_label = QLabel()
        self.loading_gif_label.setAlignment(Qt.AlignCenter)
        if self.description == "upload":
            self.loading_gif = QMovie(":/assets/loading.gif")
        else:
            self.loading_gif = QMovie(":/assets/loading2.gif")
        self.loading_gif_label.setMovie(self.loading_gif)
        layout.addWidget(self.loading_gif_label)
        self.value_label = QLabel()
        self.value_label.setAlignment(Qt.AlignCenter)
        self.value_label.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(self.value_label)
        self.description_label = QLabel(" ")
        self.description_label.setAlignment(Qt.AlignCenter)
        self.description_label.setWordWrap(True)
        layout.addWidget(self.description_label)
        layout.addStretch(2)
        self.update_value(0)
        signal_manager.sync_analysis_started.connect(self.loading)
        signal_manager.enable_buttons.connect(self.on_enable_buttons)
        signal_manager.disable_buttons.connect(self.on_disable_buttons)

    def update_value(self, value: int):
        self.value_label.setText(str(value))
        self.loading_gif.stop()
        self.loading_gif_label.hide()
        if value == 0:
            self.button_enabled = False
            self.value_label.hide()
            self.synced_icon_label.show()
            self.description_label.setText("Synced")
            self.setStyleSheet(self.synced_style)
            self.unsetCursor()
        elif value == 1:
            self.button_enabled = True
            self.value_label.show()
            self.synced_icon_label.hide()
            self.description_label.setText(self.singular_description)
            self.setStyleSheet(self.not_synced_style)
            self.setCursor(QCursor(Qt.PointingHandCursor))
        else:
            self.button_enabled = True
            self.value_label.show()
            self.synced_icon_label.hide()
            self.description_label.setText(self.plural_description)
            self.setStyleSheet(self.not_synced_style)
            self.setCursor(QCursor(Qt.PointingHandCursor))

    def loading(self):
        print("loading")
        self.value_label.hide()
        self.synced_icon_label.hide()
        local_random = random.Random(id(self))
        self.loading_gif.jumpToFrame(local_random.randint(0, self.loading_gif.frameCount() - 1))
        self.loading_gif.start()
        self.loading_gif_label.show()
        self.description_label.setText("Loading")
        self.setStyleSheet(self.synced_style)
        self.unsetCursor()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton and self.button_enabled:
            self.on_click.emit()

    def on_disable_buttons(self):
        self.button_enabled = False

    def on_enable_buttons(self):
        if self.value_label.text() != "0":
            self.button_enabled = True
