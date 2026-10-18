from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QTextBrowser
from gui.cc_logo import CCLogo

class HomePage(QWidget):
    def __init__(self, controller=None, parent=None):
        super().__init__(parent)
        self.app_controller = controller
        self.setMinimumWidth(400)
        layout = QVBoxLayout()
        layout.addWidget(CCLogo(40))
        layout.addSpacing(12)
        greeting = QTextBrowser()
        greeting.setOpenExternalLinks(True)
        greeting.setPlainText("CacheCAD acts as a file manager between your computer and Google Drive™ and let's you selectively sync files.")
        greeting.append('')
        greeting.append("CacheCAD's use and transfer of information received from Google APIs to any other app will adhere to <a href='https://developers.google.com/terms/api-services-user-data-policy#additional_requirements_for_specific_api_scopes' style='color:#ffffff'> Google API Services User Data Policy</a>, including the Limited Use requirements")
        greeting.append('')
        greeting.append("If you have questions, comments, or want to report a bugs please reach out to <a href='mailto:mike@cachecad.com'  style='color:#ffffff'>mike@cachecad.com</a>.")
        greeting.append('')
        greeting.append("For more info, <a href='https://www.cachecad.com/ToS'  style='color:#ffffff'>Terms of Service</a>, and <a href='https://www.cachecad.com/privacy-policy'  style='color:#ffffff'>Privacy Policy</a> check out <a href='https://www.cachecad.com/'  style='color:#ffffff'>cachecad.com</a>")
        greeting.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        greeting.setStyleSheet('QTextBrowser {background-color: #191919; color: white; border: 1px solid gray; font-size: 14pt}')
        greeting.setReadOnly(True)
        layout.addWidget(greeting)
        self.setLayout(layout)
        self.setMinimumSize(500, 500)
