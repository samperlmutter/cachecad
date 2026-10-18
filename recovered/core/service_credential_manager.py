from pathlib import Path
from utils.oauth_credential_import import serviceCredentialImport
from google.oauth2 import service_account
from utils.paths import APP_DATA_DIR
import logging


class ServiceCredentialManager:
    def __init__(self, path=APP_DATA_DIR / "temp.json"):
        self.path = path
        self.creds = None
        self.create_creds()

    def create_creds(self):
        try:
            SCOPES = []
            SERVICE_ACCOUNT_FILE = serviceCredentialImport(self.path)
            self.creds = service_account.Credentials.from_service_account_file(
                self.path, scopes=SCOPES)
            SERVICE_ACCOUNT_FILE.delete()
            logging.info("Service credentials created")
        except Exception as err:
            logging.info("Error creating service credentials")
            try:
                SERVICE_ACCOUNT_FILE.delete()
            except:
                pass

    def get_creds(self):
        return self.creds


service_cred_manager = ServiceCredentialManager()
