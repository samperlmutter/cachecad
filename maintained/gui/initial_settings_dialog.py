from utils.path_advisory import confirm_project_location
from PySide6.QtWidgets import QLabel, QFileDialog, QMessageBox, QDialog, QCheckBox, QLineEdit, QVBoxLayout, QHBoxLayout, QPushButton, QApplication, QGridLayout
from PySide6.QtGui import QPixmap, QIcon, QIntValidator
from PySide6.QtCore import QTimer
from utils.app_settings import settings
from core.signal_manager import signal_manager
from pathlib import Path
import logging
import traceback


def select_working_directory(parent=None):
    logging.info('Launching QMessageBox')
    default_dir = settings.get_working_directory()
    reply = QMessageBox.question(parent, 'Select Working Directory', f"It looks like this is the first time you've opened CacheCAD. By default, your projects are saved to {default_dir}. Do you want to change this?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
    if reply == QMessageBox.Yes:
        new_directory = QFileDialog.getExistingDirectory(parent, 'Select Working Directory', str(default_dir))
        if new_directory:
            new_directory = Path(new_directory)
            if new_directory.exists() and new_directory != Path('.'):
                if not confirm_project_location(new_directory, parent):
                    return
                signal_manager.change_working_directory.emit(new_directory, default_dir, False)


class CacheCADUsageDialog(QDialog):
    def __init__(self):
        super().__init__()
        pix = QPixmap(':/assets/window.png')
        self.setWindowIcon(QIcon(pix))
        self.setWindowTitle('CacheCAD Usage Survey')
        self.resize(500, 300)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(5)
        label = QLabel('What do you use CacheCAD for? This information helps me understand who is using the app so I can continue to support.')
        label.setWordWrap(True)
        label.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(label)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(5)
        self.competition_checks = {}
        self.competition_inputs = {}
        competitions_numeric = ['FRC', 'FTC', 'VEX']
        competitions_text = ['SAE Competitions']
        row = 0
        for comp in competitions_numeric:
            cb = QCheckBox(comp)
            le = QLineEdit()
            le.setPlaceholderText('Team Number')
            le.setVisible(False)
            le.setValidator(QIntValidator(0, 99999, self))
            cb.toggled.connect(le.setVisible)
            grid.addWidget(cb, row, 0, 1, 1)
            grid.addWidget(le, row, 1, 1, 1)
            self.competition_checks[comp] = cb
            self.competition_inputs[comp] = le
            row += 1
        for comp in competitions_text:
            cb = QCheckBox(comp)
            le = QLineEdit()
            le.setPlaceholderText('School')
            le.setVisible(False)
            cb.toggled.connect(le.setVisible)
            grid.addWidget(cb, row, 0, 1, 1)
            grid.addWidget(le, row, 1, 1, 1)
            self.competition_checks[comp] = cb
            self.competition_inputs[comp] = le
            row += 1
        self.business_cb = QCheckBox('Business')
        self.business_input = QLineEdit()
        self.business_input.setPlaceholderText('Company / Info')
        self.business_input.setVisible(False)
        self.business_cb.toggled.connect(self.business_input.setVisible)
        grid.addWidget(self.business_cb, row, 0, 1, 1)
        grid.addWidget(self.business_input, row, 1, 1, 1)
        row += 1
        self.personal_cb = QCheckBox('Personal / Hobby Projects')
        grid.addWidget(self.personal_cb, row, 0, 1, 2)
        row += 1
        main_layout.addLayout(grid)
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton('OK')
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        main_layout.addLayout(btn_layout)

    def get_results(self):
        """Return a dictionary with selected options and their values"""
        results = {}
        for comp, cb in self.competition_checks.items():
            if cb.isChecked():
                val = self.competition_inputs[comp].text().strip()
                results[comp] = val if val else True
        if self.business_cb.isChecked():
            val = self.business_input.text().strip()
            results['Business'] = val if val else True
        if self.personal_cb.isChecked():
            results['Personal / Hobby Projects'] = True
        settings.set('usage', results)
