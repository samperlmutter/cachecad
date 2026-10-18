import os
import sys
from pathlib import Path
import logging

if getattr(sys, 'frozen', False):
    bundle_dir = Path(sys.executable).parent
    sys.path.insert(0, str(bundle_dir / 'src'))
else:
    sys.path.insert(0, str(Path(__file__).parent / 'src'))

from PySide6.QtWidgets import QApplication
from app.app_controller import AppController
from gui.main_window import MainWindow
import resources_rc
from utils.app_logger import setup_logging
from app.app_version import APP_VERSION, APP_IS_BETA, APP_BUILD_DATE

def main():
    setup_logging()
    title = f'CacheCAD v{APP_VERSION}'
    if APP_IS_BETA:
        title += f' - (Beta {APP_BUILD_DATE})'
    title += '\n'
    logging.info(title)
    logging.info(f'Python Version {sys.version}')
    logging.info(f'Platform: {sys.platform}')
    logging.info(f'Executable: {sys.executable}')
    logging.info(f'Current Working Directory: {os.getcwd()}')
    logging.info('\n')
    logging.info('\n')
    app = QApplication(sys.argv)
    app_controller = AppController()
    window = MainWindow(app_controller)
    app_controller.set_main_window(window)
    window.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
