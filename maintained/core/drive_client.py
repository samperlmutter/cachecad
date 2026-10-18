# Maintained derivative of the bytecode-verified recovered/core/drive_client.py.
# Remote scan errors fail closed: incomplete snapshots are never published.
# Offline injected-mock regression tests only; no live-account validation.
from PySide6.QtCore import QObject, Signal, QRunnable, QThreadPool, Qt, QEventLoop, QTimer, QMetaObject, Slot
from PySide6.QtGui import QPixmap, QImage, QPainter, QBrush, QBitmap, QPen
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from requests.exceptions import RequestException
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.auth.transport.requests import AuthorizedSession
from googleapiclient.http import BatchHttpRequest
from googleapiclient.http import MediaFileUpload
from googleapiclient.http import MediaIoBaseDownload
from pathlib import PureWindowsPath
from collections import deque
import hashlib
import requests
import json
import time
import logging
import os
import io
import httplib2
import re
from collections import defaultdict
from pathlib import Path
import traceback
from core.signal_manager import signal_manager
from core.thread_manager import thread_pool
from core.models import DriveItemInfo
from utils.timer import tic, toc
from utils.app_settings import settings


class DriveClient(QObject):
    service_built = Signal(object)

    def __init__(self, creds):
        super().__init__()
        self.creds = creds
        self.service = None
        self.cancelled = False
        self.user_info = None
        self.loop = QEventLoop()
        thread_pool.start(_FunctionRunnable(self.build_service))
        self.service_built.connect(self._finish)
        self.loop.exec()
        signal_manager.token_refreshed.connect(self.on_token_refresh)
        signal_manager.sync_execution_finished.connect(self.on_sync_finished)
        signal_manager.sync_cancelled.connect(self.on_sync_cancelled)
        self.keep_setting = settings.get('keep_revision_forever')
        if not isinstance(self.keep_setting, bool):
            if isinstance(self.keep_setting, str):
                if self.keep_setting.lower() == 'true':
                    self.keep_setting = True
                    settings.set('keep_revision_forever', True)
            else:
                logging.error('Keep revision forever setting invalid, setting to False.')
                self.keep_setting = False
                settings.set('keep_revision_forever', False)

    def on_sync_finished(self):
        self.cancelled = False

    def on_sync_cancelled(self):
        self.cancelled = True

    def build_service(self):
        try:
            service = build('drive', 'v3', credentials=self.creds, cache_discovery=False, num_retries=3)
        except Exception as e:
            tb = traceback.format_exc()
            service = None
            logging.error('service not built {e} \n {tb}')
        self.service_built.emit(service)

    def _finish(self, service):
        if service is not None: self.service = service
        self.loop.quit()

    def on_token_refresh(self, success, creds):
        if success:
            self.creds = creds
            self.loop = QEventLoop()
            thread_pool.start(_FunctionRunnable(self.build_service))
            self.loop.exec()
        if not success:
            logging.info('Token could not be refreshed. Signed out.')

    def get_user_profile(self):
        def task():
            user_info = {}
            pic_url = None
            pixmap = None
            try:
                session = AuthorizedSession(self.creds)
                response = session.get('https://www.googleapis.com/oauth2/v1/userinfo')
                response.raise_for_status()
                data = response.json()
                email = data.get('email')
                if email:
                    user_info['email_display'] = email
                    user_info['email'] = email.lower()
                    user_info['email_hash'] = hashlib.md5(email.encode('utf-8')).hexdigest()
                    user_info['email_domain'] = email.rsplit('@', 1)[1]
                else:
                    print('[get_user_profile] No email found in response.')
                pic_url = data.get('picture')
                if not pic_url:
                    print('[get_user_profile] No profile picture URL provided.')
            except RequestException as e:
                tb = traceback.format_exc()
                status = getattr(e.response, 'status_code', 'N/A')
                reason = getattr(e.response, 'reason', 'No reason provided')
                logging.error(f'[get_user_profile] HTTP error {status}: {reason} - {e} \n {tb}')
                signal_manager.error_occurred.emit('')
            except Exception as e:
                tb = traceback.format_exc()
                logging.error(f'[get_user_profile] Unexpected error fetching user info: {e} \n {tb}')
            if pic_url:
                try:
                    pic_response = requests.get(pic_url)
                    pic_response.raise_for_status()
                    pixmap = QPixmap()
                    pixmap.loadFromData(pic_response.content)
                except RequestException as e:
                    status = getattr(e.response, 'status_code', 'N/A')
                    reason = getattr(e.response, 'reason', 'No reason provided')
                    tb = traceback.format_exc()
                    logging.error(f'[get_user_profile] Error downloading profile pic - HTTP {status}: {reason} - {e} \n {tb}')
                except Exception as e:
                    tb = traceback.format_exc()
                    logging.erorr(f'[get_user_profile] Unexpected error loading profile pic: {e} \n {tb}')
                    signal_manager.error_occurred.emit('')
            if pixmap is not None:
                mask = QImage(pixmap.size(), QImage.Format.Format_ARGB32)
                mask.fill(Qt.GlobalColor.transparent)
                painter = QPainter(mask)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                brush = QBrush(Qt.GlobalColor.white)
                painter.setBrush(brush)
                painter.setPen(QPen(Qt.GlobalColor.white))
                offset_width = round(pixmap.width() * 0.05)
                offset_height = round(pixmap.height() * 0.05)
                width = round(pixmap.width() * 0.95)
                height = round(pixmap.height() * 0.95)
                painter.drawEllipse(offset_width, offset_height, width, height)
                painter.end()
                mask.invertPixels()
                pixmap.setMask(QBitmap.fromImage(mask))
                pixmap = pixmap.scaled(35, 35)
                padding = 15
                new_width = pixmap.width() + padding
                new_height = pixmap.height()
                new_pixmap = QPixmap(new_width, new_height)
                new_pixmap.fill(Qt.GlobalColor.transparent)
                painter = QPainter(new_pixmap)
                painter.drawPixmap(0, 0, pixmap)
                painter.end()
                pixmap = new_pixmap
            if user_info is not None:
                self.user_info = user_info
                signal_manager.user_profile_loaded.emit(user_info, pixmap)
        thread_pool.start(_FunctionRunnable(task))

    def get_user_info_for_cloud_logging(self):
        if self.user_info is not None:
            return self.user_info
        if self.user_info is None:
            logging.warning('drive_client.user_info is None')

    def get_drive_id_from_item_id(self, project_id):
        logging.info('requesting drive id for drive item ' + project_id)
        try:
            results = self.service.files().get(fileId=project_id, fields='driveId', supportsAllDrives=True).execute()
            return results.get('driveId', '')
        except HttpError as e:
            tb = traceback.format_exc()
            status = e.resp.status
            try:
                error_details = json.loads(e.content.decode())
                message = error_details.get('error', {}).get('message', 'No error message provided.')
            except Exception:
                message = str(e)
            logging.error(f'[get_drive_id_from_item_id] HttpError {status}: {message} (project_id: {project_id}) \n {tb}')
        except KeyError:
            tb = traceback.format_exc()
            logging.error(f"[get_drive_id_from_item_id] Key 'driveId' not found in API response (project_id: {project_id}) \n {tb}")
            signal_manager.error_occurred.emit('')
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f'[get_drive_id_from_item_id] Unexpected error: {e} (project_id: {project_id}) \n {tb}')
            signal_manager.error_occurred.emit('')
        return ''

    def get_folder_name_from_id(self, project_id):
        try:
            results = self.service.files().get(fileId=project_id, fields='id, name, driveId', supportsAllDrives=True).execute()
            name = results.get('name')
            id = results.get('driveId')
            if name == 'Drive' and id is not None:
                if len(id) <= 19:
                    drive = self.service.drives().get(driveId=results['driveId'], fields='id, name').execute()
                    return drive['name']
            if name:
                return name
            else:
                return ''
        except HttpError as e:
            status = e.resp.status
            try:
                error_details = json.loads(e.content.decode())
                message = error_details.get('error', {}).get('message', 'No error message provided.')
            except Exception:
                message = str(e)
            return ''
        except KeyError:
            print(f"[get_folder_name_from_id] Key 'name' not found in API response (project_id: {project_id})")
        except Exception as e:
            print(f'[get_folder_name_from_id] Unexpected error: {e} (project_id: {project_id})')
        return ''

    def get_drive_items_from_project_id(self, project_id, cached_folders=None, *,
                                        scan_id=None, on_success=None, on_error=None):
        """Publish one complete snapshot or an error, preserving its scan identity."""
        tagged = scan_id is not None or on_success is not None or on_error is not None
        if tagged and (scan_id is None or not callable(on_success) or not callable(on_error)):
            raise ValueError('Tagged scans require scan_id, on_success, and on_error')
        settled = False

        def fail(message):
            nonlocal settled
            if settled:
                return
            settled = True
            if tagged:
                on_error(scan_id, message)
            else:
                signal_manager.error_occurred.emit(message)

        def succeed(items):
            nonlocal settled
            if settled:
                return
            settled = True
            if tagged:
                on_success(scan_id, items)
            else:
                signal_manager.drive_file_list_acquired.emit(items)

        try:
            if cached_folders is None:
                cached_folders = []
            request_queue = deque()
            all_files = []
            all_folders = []
            already_requested_folders = set(cached_folders)
            scan_failed = False
            def build_list_request(folder_id, page_token=None, service=self.service):
                query = f"'{folder_id}' in parents and trashed = false"
                return service.files().list(q=query, pageToken=page_token or None, supportsAllDrives=True,
                    includeItemsFromAllDrives=True, corpora='allDrives', pageSize=1000,
                    fields='nextPageToken, incompleteSearch, files(id, name, parents, md5Checksum, version, capabilities(canAddChildren), modifiedTime, size, driveId, lastModifyingUser)'), query
            logging.debug(f'Starting file scan from project_id: {project_id}')
            tic()
            starting_folders = list(dict.fromkeys(
                [f for f in cached_folders if f != project_id] + [project_id]))
            for folder_id in starting_folders:
                request, query = build_list_request(folder_id)
                request_queue.append((request, query))
            total_batches = 0
            while request_queue:
                batch = self.service.new_batch_http_request()
                batch_size = min(len(request_queue), 100)
                logging.debug(f'\nExecuting batch #{total_batches + 1} with {batch_size} requests...')
                total_batches += 1
                event_loop = QEventLoop()
                pending = batch_size
                for _ in range(batch_size):
                    req, q = request_queue.popleft()
                    def make_callback(query=q):
                        def callback(request_id, response, exception):
                            nonlocal pending, scan_failed
                            logging.debug(f'[Callback] Request ID: {request_id}')
                            if exception:
                                tb = traceback.format_exc()
                                logging.error(f'Batch error for query [{query}]: {exception} \n {tb}')
                                scan_failed = True
                                pending -= 1
                                return
                            if response.get('incompleteSearch'):
                                logging.error('Drive reported an incomplete search; refusing partial results.')
                                scan_failed = True
                                pending -= 1
                                return
                            items = response.get('files', [])
                            logging.debug(f'Found {len(items)} items in response for query [{query}]')
                            for item in items:
                                if item.get('capabilities', {}).get('canAddChildren'):
                                    folder_id = item.get('id')
                                    folder_name = item.get('name')
                                    all_folders.append(item)
                                    if folder_id not in already_requested_folders:
                                        logging.debug(f'Adding Folder: {folder_name} to request queue')
                                        new_req, new_q = build_list_request(folder_id)
                                        request_queue.append((new_req, new_q))
                                    else:
                                        logging.debug(f'   Skipping already-seen folder: {folder_name} ({folder_id})')
                                    already_requested_folders.add(folder_id)
                                elif item.get('md5Checksum'):
                                    if item.get('id') == '1ej1rRP1tJ2PFvom51bqC11gVnmow5UaX':
                                        logging.debug(f" - File: {item.get('name')} ({item.get('id')})")
                                    all_files.append(item)
                            next_token = response.get('nextPageToken')
                            if next_token:
                                folder_id = query.split("'")[1]
                                logging.debug(f'Next page for folder {folder_id}, token: {next_token}')
                                next_req, _ = build_list_request(folder_id, page_token=next_token)
                                request_queue.append((next_req, query))
                            pending -= 1
                            if pending == 0:
                                QTimer.singleShot(0, event_loop.quit)
                        return callback
                    batch.add(req, callback=make_callback())
                try:
                    logging.debug('Executing batch')
                    batch.execute()
                except Exception as e:
                    tb = traceback.format_exc()
                    logging.error(f'Batch execution failed: {e} \n {tb}')
                    print(f'Batch execution failed: {e}')
                    scan_failed = True
                if scan_failed or pending != 0:
                    logging.error('Drive scan incomplete; sync analysis was not allowed to continue.')
                    fail('Drive scan incomplete. Sync analysis stopped without generating actions.')
                    return
            logging.info('\n')
            logging.info('Finished scanning.')
            logging.info(f'Drive files found: {len(all_files)}')
            logging.info(f'Drive folders found: {len(all_folders)}')
            print(f'Drive files found: {len(all_files)}')
            print(f'Drive folders found: {len(all_folders)}')
            toc('Requests from Google:')
            if tagged:
                drive_items = self.build_drive_files(all_folders, all_files, project_id, report_error=fail)
            else:
                drive_items = self.build_drive_files(all_folders, all_files, project_id)
            succeed(drive_items)
        except Exception as error:
            logging.exception('Drive scan failed before a complete snapshot was available.')
            fail('Drive scan incomplete: ' + str(error))

    def build_drive_files(self, folders: list, files: list, root_id: str, *, report_error=None) -> dict[str, DriveItemInfo]:
        folder_by_id = {folder['id']: folder for folder in folders}
        INVALID_WIN_CHARS = re.compile(r'[<>:"/\\|?*]')
        RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
        def is_valid_path(path_str: str) -> bool:
            if not isinstance(path_str, str) or not path_str.strip():
                return False
            path = PureWindowsPath(path_str)
            for part in path.parts:
                if INVALID_WIN_CHARS.search(part):
                    return False
                if part.endswith(' ') or part.endswith('.') or part.startswith('.') or part.startswith('~$'):
                    return False
                if part.upper().split('.')[0] in RESERVED_NAMES:
                    return False
            return True
        def get_path(item: dict):
            parents = item.get('parents')
            if not parents:
                return None
            if len(parents) > 1:
                print(f"Skipping item {item['id']} ('{item['name']}') — multiple parents not supported.")
                logging.warning(f"Skipping item {item['id']} ('{item['name']}') — multiple parents not supported.")
                return None
            parts = [item['name']]
            current = item
            depth = 0
            drive_id = item.get('driveId')
            while True:
                parents = current.get('parents')
                if not parents:
                    return None
                parent_id = parents[0]
                if parent_id == root_id:
                    break
                if parent_id == drive_id:
                    return None
                parent = folder_by_id.get(parent_id)
                if not parent:
                    return None
                parts.append(parent['name'])
                current = parent
                depth += 1
                if depth > 100:
                    print(f"Max depth exceeded for item {item['id']}")
                    return None
            parts.reverse()
            return str(PureWindowsPath(*parts))
        drive_items = []
        for file in files:
            full_path = get_path(file)
            if not is_valid_path(full_path):
                logging.warning(f"Skipping item {file['id']} ('{file['name']}') — invalid Windows path or temp file: {full_path}")
                continue
            drive_items.append(DriveItemInfo.from_drive_item(file, full_path))
        for folder in folders:
            full_path = get_path(folder)
            logging.debug(f'Folder full path: {full_path}')
            if full_path is None:
                continue
            if not is_valid_path(full_path):
                logging.warning(f"Skipping item {folder['id']} ('{folder['name']}') — invalid Windows path: {full_path}")
            drive_items.append(DriveItemInfo.from_drive_item(folder, full_path))
        seen_files = {}
        seen_folders = {}
        unique_drive_items = []
        duplicate_items = []
        for item in drive_items:
            key = (item.path, item.name)
            if item.is_folder:
                if key in seen_folders:
                    logging.error(f"Duplicate FOLDER '{item.name}' in Drive folder {item.path}. Keeping {seen_folders[key].id}, skipping {item.id}")
                    duplicate_items.append(item)
                    continue
                seen_folders[key] = item
                unique_drive_items.append(item)
            else:
                if key in seen_files:
                    logging.error(f"Duplicate FILE '{item.name}' in Drive folder {item.path}. Keeping {seen_files[key].id}, skipping {item.id}")
                    duplicate_items.append(item)
                    continue
                seen_files[key] = item
                unique_drive_items.append(item)
        if len(duplicate_items) > 0:
            if report_error is not None:
                report_error('Drive scan incomplete: duplicate items with the same name.')
            else:
                signal_manager.error_occurred.emit('Duplicate items with same name:')
            for item in duplicate_items:
                logging.warning(f'{item.path}')
        return unique_drive_items

    def create_folder(self, name, parent_id):
        try:
            file_metadata = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
            folder = self.service.files().create(body=file_metadata, supportsAllDrives=True, fields='id').execute()
            return folder.get('id')
        except HttpError as e:
            tb = traceback.format_exc()
            status = e.resp.status
            try:
                error_details = json.loads(e.content.decode())
                message = error_details.get('error', {}).get('message', 'No error message provided.')
            except Exception:
                message = str(e)
            logging.error(f'[create_folder] HttpError {status}: {message} (name: {name}, parent_id: {parent_id}) \n {tb}')
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f'[create_folder] Unexpected error: {e} (name: {name}, parent_id: {parent_id}) \n {tb}')
            signal_manager.error_occurred.emit('')
        return None

    def upload_file(self, local_path: str, parent_id: str, idx: int = None, existing_drive_id: str = None):
        """
        Upload a new file or a revision to Google Drive and emit progress updates.

        Args:
            local_path (str): Path to the local file.
            parent_id (str): Google Drive folder ID to upload into.
            idx (int, optional): An index for UI tracking. Defaults to None.
            existing_drive_id (str, optional): If provided, upload as a revision to this file.
        """
        if self.cancelled:
            return None, None
        media = None
        try:
            local_path = Path(local_path)
            if not local_path.is_file():
                logging.error(f'File not found in local path: {local_path}')
                return None, None
            media = MediaFileUpload(str(local_path), resumable=True)
            service = build('drive', 'v3', credentials=self.creds)
            if existing_drive_id:
                request = service.files().update(fileId=existing_drive_id, media_body=media, fields='id, md5Checksum', supportsAllDrives=True)
            else:
                body = {'name': local_path.name, 'parents': [parent_id], 'keepRevisionForever': self.keep_setting}
                request = service.files().create(body=body, media_body=media, fields='id, md5Checksum', supportsAllDrives=True)
            response = None
            progress = 0.0
            while response is None:
                if self.cancelled:
                    return None, None
                status, response = request.next_chunk()
                if status:
                    progress = round(status.progress() * 100, 2)
                    if idx is not None:
                        signal_manager.file_progress.emit(idx, progress)
                    logging.debug(f'Uploading {local_path.name}: {progress}%')
            if idx is not None:
                signal_manager.file_progress.emit(idx, 100.0)
            drive_file_id = response.get('id')
            drive_md5 = response.get('md5Checksum')
            logging.info(f'Upload complete: {local_path} -> {drive_file_id} in parent {parent_id} (MD5: {drive_md5})')
            return drive_file_id, drive_md5
        except HttpError as e:
            tb = traceback.format_exc()
            status = e.resp.status
            try:
                error_details = json.loads(e.content.decode())
                message = error_details.get('error', {}).get('message', 'No error message provided.')
            except Exception:
                message = str(e)
            logging.error(f'[download_file] HttpError {status}: {message} \n {tb}')
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f'[download_file] Unexpected error: {e} \n {tb}')
            signal_manager.error_occurred.emit('')
        finally:
            if media is not None:
                try:
                    # MediaFileUpload owns this stream. Release the staged file
                    # before its caller removes the temporary upload directory.
                    media.stream().close()
                except Exception:
                    # The remote operation may already have committed. Cleanup
                    # must not replace its result with a retryable transfer error.
                    logging.exception('Upload stream cleanup failed; preserving the remote upload outcome.')
        return None

    def download_file(self, file_id: str, local_path: str, idx, use_temp: bool = True) -> Path:
        """
        Download a file from Google Drive to a local path.

        Args:
            file_id (str): The ID of the file on Google Drive.
            local_path (str): The local filesystem path to save the file.
        """
        if self.cancelled:
            return None
        fh = None
        try:
            temp_path = Path(local_path).parent / f'~$downloading{str(idx)}.temp'
            service = build('drive', 'v3', credentials=self.creds)
            request = service.files().get_media(fileId=file_id)
            fh = io.FileIO(temp_path, 'wb')
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            progress = 0.0
            while not done:
                if self.cancelled:
                    return temp_path
                status, done = downloader.next_chunk()
                if status:
                    progress = round(status.progress() * 100, 2)
                    signal_manager.file_progress.emit(idx, progress)
                    logging.debug(f'Downloading {file_id}: {progress}%')
            signal_manager.file_progress.emit(idx, 100.0)
            logging.info(f'Downloaded: {file_id} -> {local_path} as temp file {temp_path}')
            return temp_path
        except HttpError as e:
            tb = traceback.format_exc()
            status = e.resp.status
            try:
                error_details = json.loads(e.content.decode())
                message = error_details.get('error', {}).get('message', 'No error message provided.')
            except Exception:
                message = str(e)
                logging.error(f'[download_file] HttpError {status}: {message} (file_id: {file_id}) \n {tb}')
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f'[download_file] Unexpected error: {e} (file_id: {file_id}) \n {tb}')
            signal_manager.error_occurred.emit('')
        finally:
            if fh is not None:
                try:
                    # Finish local I/O before verification or publication by
                    # the executor; Windows cannot replace an open target.
                    fh.close()
                except Exception:
                    logging.exception('Download stream cleanup failed; preserving the transfer outcome for caller verification.')
        return None

    def delete_file(self, file_id: str):
        if self.cancelled:
            return False
        is_trashed = False
        attempts = 0
        max_attempts = 3
        service = build('drive', 'v3', credentials=self.creds)
        while not is_trashed and attempts < max_attempts:
            attempts += 1
            file_metadata = {'trashed': True}
            service.files().update(fileId=file_id, body=file_metadata, supportsAllDrives=True, fields='id').execute()
            try:
                result = service.files().get(fileId=file_id, fields='trashed', supportsAllDrives=True).execute()
                is_trashed = result.get('trashed', False)
                logging.info(f'File {file_id} trashed status: {is_trashed}')
                return is_trashed
            except HttpError as e:
                status = e.resp.status
                tb = traceback.format_exc()
                try:
                    error_details = json.loads(e.content.decode())
                    message = error_details.get('error', {}).get('message', 'No error message provided.')
                except Exception:
                    message = str(e)
                logging.error(f'[delete_file] HttpError {status}: {message} (file_id: {file_id}) \n {tb}')
            except Exception as e:
                tb = traceback.format_exc()
                logging.error(f'[delete_file] Unexpected error: {e} (file_id: {file_id}) \n {tb}')
                signal_manager.error_occurred.emit('')
        return False


class _FunctionRunnable(QRunnable):
    def __init__(self, func):
        super().__init__()
        self.func = func

    def run(self):
        try:
            self.func()
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f'Error in _FunctionRunnable: {e} \n {tb}')
            signal_manager.error_occurred.emit('')
