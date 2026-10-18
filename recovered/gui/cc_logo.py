from PySide6.QtWidgets import QLabel
from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics
from PySide6.QtCore import Qt


class CCLogo(QLabel):
    def __init__(self, font_size=16, alignment=Qt.AlignCenter, parent=None):
        super().__init__(parent)
        font_id = QFontDatabase.addApplicationFont(":/assets/Comfortaa-Bold.ttf")
        if font_id != -1:
            font_family = QFontDatabase.applicationFontFamilies(font_id)[0]
        else:
            font_family = "Arial"
        font = QFont(font_family)
        font.setPointSize(font_size)
        self.setStyleSheet(f"""
            QLabel {{
                font-family: '{font_family}';
                font-size: {font_size}pt;
                font-weight: bold;
                background: transparent;
            }}
        """)
        self.setFont(font)
        metrics = QFontMetrics(font)
        text_width = metrics.horizontalAdvance("CacheCAD")
        text_height = metrics.height()
        self.resize(text_width, text_height)
        self.setText('<span style="color: white;">Cache</span><span style="color: #00693e;">CAD</span>')
        self.setAlignment(alignment)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setContentsMargins(0, 0, 0, 0)
