# Faithful compile-only recovery; original behavior retained, no reporting executed.
'\nCreated on Mon Jul 31 16:03:40 2023\n\n@author: Schre\n'
import requests
import os
import concurrent.futures
import json
import threading
from google.oauth2 import service_account
from googleapiclient.discovery import build
import logging
from googleapiclient.http import MediaFileUpload
import hashlib
from googleapiclient.errors import HttpError
from core.service_credential_manager import service_cred_manager
import traceback
from utils.paths import APP_DATA_DIR, APP_LOG_DIR
from pathlib import Path
from core.thread_manager import network_pool
from utils.timestamps import current_unix_timestamp, timestamp_to_string
from PySide6.QtCore import QRunnable

class BugReport:
    def __init__(self, path=APP_DATA_DIR / 'temp.json'):
        self.bug_report_repository = '189dBwaADV-S4LclKQfKP2c9fld1Ph7aM'
        self.bug_report_repository_backup = '1jAfRElgEQ2Z7PuBXpxGxETPmveNl_Yc-'
        self.creds = service_cred_manager.get_creds()
        if self.creds is None:
            logging.error('No credentials for bug reporting')
            self.cloud_logging_disabled = True

    def new_bug_report(self, project_path, description, user):
        self.project_path = project_path
        self.description = description
        self.user_info = user
        bug_report_task = _FunctionRunnable(self.bug_report_worker)
        network_pool.start(bug_report_task)

    def bug_report_worker(self):
        self.bug_description = Path(APP_DATA_DIR) / '!Bug_Report.txt'
        self.bug_description.write_text(self.description, encoding='utf-8')
        file_list = [self.bug_description,
            Path(APP_LOG_DIR) / 'app.log',
            Path(APP_DATA_DIR) / 'settings.json',
            Path(APP_DATA_DIR) / 'projects.csv',
            Path(self.project_path) / '.cachecad' / 'action_log.csv',
            Path(self.project_path) / '.cachecad' / 'folder_dict.csv',
            Path(self.project_path) / '.cachecad' / 'sync_log.csv']
        try:
            email = self.user_info['email_display']
        except:
            email = 'User_not_logged_in'
        time = timestamp_to_string(current_unix_timestamp())
        report_name = email + '__' + time
        logging.info(f'Creating bug report {report_name}')
        report_folder = self.create_folder(report_name)
        logging.info(f'Created {report_folder}')
        for file in file_list:
            if file.exists():
                self.upload_file(file, report_folder)

    def upload_file(self, local_path: str, parent_id: str):
        logging.info(f'uploading {local_path} to {parent_id}')
        local_path = Path(local_path)
        media = MediaFileUpload(str(local_path), resumable=True)
        service = build('drive', 'v3', credentials=self.creds)
        body = {'name': local_path.name, 'parents': [parent_id], 'keepRevisionForever': False}
        response = service.files().create(body=body, media_body=media, fields='id', supportsAllDrives=True).execute()
        drive_file_id = response.get('id')
        logging.info(f'Bug report file upload complete: {local_path} -> {drive_file_id})')

    def create_folder(self, name, use_backup_folder=False):
        parent_id = self.bug_report_repository
        if use_backup_folder:
            parent_id = self.bug_report_repository_backup
        try:
            self.service = build('drive', 'v3', credentials=self.creds)
            file_metadata = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [self.bug_report_repository]}
            folder = self.service.files().create(body=file_metadata, supportsAllDrives=True, fields='id').execute()
            logging.info(f'Folder {name} created')
            id = folder.get('id')
            if id:
                return id
            else:
                logging.info('did not get id of created folder')
                logging.info(f'{folder}')
        except HttpError as e:
            status = e.resp.status
            if e.resp.status == 404 and not use_backup_folder:
                self.create_folder(name, True)
            try:
                error_details = json.loads(e.content.decode())
                message = error_details.get('error', {}).get('message', 'No error message provided.')
            except Exception:
                message = str(e)
            print(f'[create_folder] HttpError {status}: {message} (name: {name}, parent_id: {parent_id})')
        except Exception as e:
            print(f'[create_folder] Unexpected error: {e} (name: {name}, parent_id: {parent_id})')
        return None

class _FunctionRunnable(QRunnable):
    """Simple wrapper to run any function in a thread."""
    def __init__(self, func):
        super().__init__()
        self.func = func

    def run(self):
        try:
            self.func()
        except Exception as e:
            logging.error(f'Error in _FunctionRunnable: {e}\n{traceback.format_exc()}')
