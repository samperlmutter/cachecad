import json
import os

from pathlib import Path
from utils.paths import APP_DATA_DIR
import logging
import traceback
from core.signal_manager import signal_manager
class AppSettings:
    def __init__(self, filename='settings.json'):
        self.path = os.path.join(APP_DATA_DIR, filename)
        self.data = {}

    def load(self):
        if not Path(self.path).exists():
            logging.info('No settings file found - creating default settings')
            default_working_directory = str(Path.home() / 'Documents' / 'CacheCAD')
            self.data = {
                'last_opened_project': None,
                'working_directory': Path(default_working_directory).as_posix(),
                'default_drive_folder': '',
                'keep_revision_forever': False,
                'use_tray_icon': False,
            }
            self.save()
            signal_manager.new_install.emit()
        else:
            logging.info(f'Loading settings from {self.path}')
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    self.data.update(json.load(f))
            except Exception as e:
                tb = traceback.format_exc()
                logging.error(f'[Settings] Failed to load settings: {e}\n{tb}')
        if self.get('usage') is None:
            signal_manager.prompt_for_usage_data.emit()
    def save(self):
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f'[Settings] Failed to save settings: {e}\n{tb}')
            signal_manager.error_occurred.emit('')
            signal_manager.error_occurred.emit('')

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def set_working_directory(self, path: Path):
        self.set('working_directory', str(path))

    def get_working_directory(self):
        working_dir = Path(self.get('working_directory'))
        logging.debug(f'Getting working directory: {working_dir}')
        if working_dir:
            return working_dir
        else:
            logging.warning('Working directory not set, returning default.')
            raise ValueError('Working directory not set.')


    def get_default_drive_folder(self):
        return Path(self.get('default_drive_folder'))

    def get_last_opened_project(self):
        return Path(self.get('last_opened_project'))

    def get_use_tray_icon(self):
        return self.get('use_tray_icon', True)

settings = AppSettings()
