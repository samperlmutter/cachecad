from google.oauth2 import service_account
from PySide6.QtCore import QRunnable
from core.service_credential_manager import service_cred_manager
from google.cloud import logging as cloud_logging
import logging
import os
import traceback
from app.app_version import APP_VERSION, APP_IN_PRODUCTION
from utils.paths import APP_DATA_DIR
from core.thread_manager import thread_pool


class cloudLogging:

    def __init__(self, path=APP_DATA_DIR / "temp.json"):
        self.cloud_logging_disabled = False
        self.creds = service_cred_manager.get_creds()
        if self.creds is None:
            self.cloud_logging_disabled = True
            return
        self.create()

    def create(self):
        if APP_IN_PRODUCTION:
            cloud_client = cloud_logging.Client(project="cachecad", credentials=self.creds)
        else:
            cloud_client = cloud_logging.Client(project="testingdriveapi-387217", credentials=self.creds)
        cloud_log_name = "cloud-log"
        self.cloudlog = cloud_client.logger(cloud_log_name)
        logging.info("Cloud Logger Setup")

    def log_sync(self, struct):
        if self.cloud_logging_disabled:
            return
        log_task = _FunctionRunnable(lambda: self.log_worker(struct))
        thread_pool.start(log_task)

    def log_worker(self, struct):
        try:
            self.cloudlog.log_struct(struct)
            logging.info("Cloud Log sent")
        except Exception as err:
            logging.info("Error with cloud logging")
            logging.info(err)


class _FunctionRunnable(QRunnable):
    """Simple wrapper to run any function in a thread."""
    def __init__(self, func):
        super().__init__()
        self.func = func

    def run(self):
        try:
            self.func()
        except Exception as e:
            logging.error(f"Error in _FunctionRunnable: {e}\n{traceback.format_exc()}")


cloud_logging = cloudLogging()
