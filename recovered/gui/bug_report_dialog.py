from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QHBoxLayout, QTextBrowser, QPushButton, QTextEdit, QSizePolicy
from gui.project_list_widget import ProjectListWidget
from core.signal_manager import signal_manager


class BugReportDialog(QDialog):
    def __init__(self, app_controller):
        super().__init__()
        self.setWindowTitle("Report a Bug")
        self.app_controller = app_controller
        self.setMinimumSize(650, 200)
        layout = QVBoxLayout(self)
        self.label = QLabel("Please provide a description of the bug and what you were doing when it happened. Please select the project you were working with when it occurred.")
        self.label.setStyleSheet("font-size: 12pt")
        layout.addWidget(self.label)
        self.bug_description = QTextEdit()
        layout.addWidget(self.bug_description)
        self.project_list = ProjectListWidget(app_controller)
        layout.addWidget(self.project_list)
        btn_layout = QHBoxLayout()
        self.submit_btn = QPushButton("Submit")
        self.cancel_btn = QPushButton("Cancel")
        btn_layout.addWidget(self.submit_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)
        txt_report = QTextBrowser()
        txt_report.setOpenExternalLinks(True)
        txt_report.setPlainText("Reporting a bug shares log info about your CacheCAD install and your projects with the CacheCAD team for purposes of investigating the bug.")
        txt_report.append("")
        txt_report.append("This shares the drive IDs, file names, and locations of files in your Google Drive. This doesn't share files with us unless your share settings are set to share with anyone that has a link. We do not intend to look at your files.")
        txt_report.append("")
        txt_report.append("This log also contains your Google Workspace email address, we will not share this information, this is solely for debugging. We will also not contact you, unless you reach out to us first.")
        txt_report.append("")
        txt_report.append("If you have questions or need support, feel free to reach out to <a href='mailto:mike@cachecad.com'  style='color:#ffffff'>mike@cachecad.com</a>, or <a href='https://discord.gg/juEXp6GBfc' style='color:#ffffff'> join our support discord</a>.")
        txt_report.setStyleSheet("border: 0px solid black")
        txt_report.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        txt_report.setStyleSheet("background-color: #191919")
        layout.addWidget(txt_report)
        layout.setStretch(0, 0)
        layout.setStretch(1, 2)
        layout.setStretch(2, 2)
        layout.setStretch(3, 0)
        layout.setStretch(4, 0)
        self.submit_btn.clicked.connect(self.on_submit_clicked)
        self.cancel_btn.clicked.connect(self.reject)

    def get_selected_index(self):
        return self.project_list.currentRow()

    def on_submit_clicked(self):
        project_idx = self.get_selected_index()
        report = self.bug_description.toPlainText()
        signal_manager.bug_report.emit(project_idx, report)
        self.accept()
