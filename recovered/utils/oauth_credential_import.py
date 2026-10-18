# SANITIZED RECOVERY: deliberately not bytecode-exact.
# Four embedded credential payloads were replaced with inert JSON sentinels.
# No original credential is retained here. This file must not be used for login.
"""
Created on Thu Jun 22 15:42:28 2023

@author: Schre
"""
import json
import os
import ctypes
from utils.paths import hide_file
from app.app_version import APP_IN_PRODUCTION


class OauthCredentialImport:
    def __init__(self, path):
        super().__init__()
        self.path = path
        if APP_IN_PRODUCTION:
            data = json.loads('{"credential_payload":"REDACTED_PRODUCTION_OAUTH_CREDENTIALS"}')
        else:
            data = json.loads('{"credential_payload":"REDACTED_TEST_OAUTH_CREDENTIALS"}')

        with open(path, "w") as file:
            json.dump(data, file)

        file_attributes = ctypes.windll.kernel32.GetFileAttributesW(str(self.path))
        hidden_attributes = file_attributes | 2
        ctypes.windll.kernel32.SetFileAttributesW(str(self.path), hidden_attributes)

    def delete(self):
        os.remove(self.path)


class serviceCredentialImport:
    def __init__(self, path):
        super().__init__()
        self.path = path
        if APP_IN_PRODUCTION:
            string = '{"credential_payload":"REDACTED_PRODUCTION_SERVICE_ACCOUNT_CREDENTIALS"}'
        else:
            string = '{"credential_payload":"REDACTED_TEST_SERVICE_ACCOUNT_CREDENTIALS"}'
        data = json.loads(string, strict=False)

        with open(path, "w") as file:
            json.dump(data, file)
        hide_file(path)

    def delete(self):
        os.remove(self.path)
