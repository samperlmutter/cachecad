"""
Created on Mon Jul 31 16:03:40 2023

@author: Schre
"""
from googleapiclient.discovery import build
import logging
import traceback
from PySide6.QtCore import QRunnable
from googleapiclient.errors import HttpError
from core.service_credential_manager import service_cred_manager
from utils.paths import APP_DATA_DIR
from core.thread_manager import thread_pool
from core.signal_manager import signal_manager

class UserValidation:
    def __init__(self):
        self.creds = service_cred_manager.get_creds()
        if self.creds is None:
            logging.error('No credentials for user validation')
            return
        self.sheet_id = '1OA5ws_HAqMdjLhm9e_0BlGikHwLtmkxmOn00_8klh0M'
        self.fetch_data()

    def fetch_data(self):
        logging.info('Fetching user validation data')
        self.domains = set()
        self.banned_user_hashes = set()
        fetch_task = _FunctionRunnable(self.get_user_validation_sheet_info)
        thread_pool.start(fetch_task)

    def get_user_validation_sheet_info(self):
        range_name = 'Domains!A:A'
        try:
            service = build('sheets', 'v4', credentials=self.creds)
            result = service.spreadsheets().values().get(
                spreadsheetId=self.sheet_id, range=range_name).execute()
            rows = result.get('values', [])
            for item in rows:
                self.domains.add(item[0])
        except HttpError as error:
            logging.exception('HTTP Exception reading domains sheet')
        except Exception as err:
            logging.exception('Exception reading domains sheet')
        range_name = 'BannedUsers!C:C'
        self.banned_user_hashes = set()
        try:
            result = service.spreadsheets().values().get(
                spreadsheetId=self.sheet_id, range=range_name).execute()
            rows = result.get('values', [])
            for user in rows:
                self.banned_user_hashes.add(user[0])
        except HttpError as error:
            logging.exception('HTTP Exception reading reports sheet')
        except Exception as err:
            logging.exception('Exception reading domains sheet')
        signal_manager.user_validation_data_fetched.emit(self.domains, self.banned_user_hashes)

class _FunctionRunnable(QRunnable):
    def __init__(self, func):
        super().__init__()
        self.func = func

    def run(self):
        try:
            self.func()
        except Exception as e:
            logging.error(f'Error in _FunctionRunnable: {e}')
