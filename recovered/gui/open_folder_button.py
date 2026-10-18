import os
import sys
import subprocess
from pathlib import Path
from PySide6.QtWidgets import QPushButton
from PySide6.QtGui import QIcon, QPixmap
from utils.paths import RESOURCES_DIR
import logging
import traceback


class OpenFolderButton(QPushButton):
    def __init__(self, label, folder_path=None, icon_path=None, parent=None):
        super().__init__(label, parent)
        self.folder_path = folder_path
        if icon_path:
            pix = QPixmap(icon_path)
            self.setIcon(QIcon(pix))
            self.setIconSize(pix.size())
        self.clicked.connect(self.open_folder)

    def update_folder(self, folder_path: Path):
        self.folder_path = folder_path

    def open_folder(self):
        if not self.folder_path.exists():
            print(f"[OpenFolderButton] Folder does not exist: {self.folder_path}")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(self.folder_path)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(self.folder_path)])
            else:
                subprocess.run(["xdg-open", str(self.folder_path)])
            print("Opening", self.folder_path)
        except Exception as e:
            tb = traceback.format_exc()
            logging.warning(f"[OpenFolderButton] Failed to open folder: {e} \n {tb}")
