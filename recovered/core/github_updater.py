# Faithful compile-only bytecode recovery; never executed during reconstruction.
import requests
import hashlib
import os
from pathlib import Path
import subprocess
import logging
import traceback

class GithubUpdater:
    def __init__(self):
        self.response = None
        self.tag = ''

    def request_from_github(self, owner='Schreimr', repo='CacheCAD-Releases'):
        '\n        Fetches the JSON data for the latest release from the GitHub API.\n        '
        url = f'https://api.github.com/repos/{owner}/{repo}/releases/latest'
        logging.debug(f'Fetching latest release info from: {url}')
        try:
            response = requests.get(url, headers={'Accept': 'application/vnd.github+json'})
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f'Error fetching release info: {e}')
            return None

    def get_latest_release_info(self):
        if self.response is None:
            self.response = self.request_from_github()
        if self.response:
            self.tag = self.response.get('tag_name')
            return self.tag

    def download_latest_version(self, owner='schreimr', repo='CacheCAD-Releases', asset_name='CacheCAD_Setup.exe', path=Path.home() / 'Downloads'):
        '\n        Downloads a specific release asset and verifies its SHA256 integrity using the digest from the API.\n        '
        version_str = self.tag.replace('.', '_')
        stem, ext = os.path.splitext(asset_name)
        download_file_path = str(Path(path) / f'{stem}_{version_str}{ext}')
        release_info = self.get_latest_release_info()
        if not release_info:
            return False
        asset = next((a for a in self.response['assets'] if a['name'] == asset_name), None)
        if not asset:
            logging.error(f"Error: Asset '{asset_name}' not found in the latest release.")
            return False
        api_digest = asset.get('digest')
        if not api_digest or not api_digest.startswith('sha256:'):
            logging.error('Error: SHA256 digest not available for this asset.')
            return False
        expected_hash = api_digest.split('sha256:')[1]
        download_url = asset['browser_download_url']
        logging.debug(f"Found asset '{asset_name}'.")
        logging.debug(f'Expected SHA256: {expected_hash}')
        logging.debug(f'Downloading from: {download_url}')
        try:
            file_response = requests.get(download_url, stream=True)
            file_response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logging.error(f'Error downloading file: {e}')
            return False
        hasher = hashlib.sha256()
        with open(download_file_path, 'wb') as f:
            for chunk in file_response.iter_content(chunk_size=8192):
                hasher.update(chunk)
                f.write(chunk)
        calculated_hash = hasher.hexdigest()
        logging.debug(f'Calculated SHA256: {calculated_hash}')
        if calculated_hash.lower() == expected_hash.lower():
            logging.debug('Integrity check passed! The hashes match.')
            return True
        else:
            logging.debug('Integrity check failed! The hashes do NOT match.')
            download_file_path.delete()
            return False

    def verify_signature(file_path, expected_signer):
        '\n        Verifies the digital signature of a file on Windows and confirms the signer.\n        \n        This function uses certutil.exe to get detailed certificate information,\n        parses the output to find the \'Subject\' (signer\'s name), and compares it\n        to the expected name.\n\n        Args:\n            file_path (str): The full path to the file to be verified.\n            expected_signer (str): The full name of the expected signer (e.g., "Microsoft Corporation").\n\n        Returns:\n            bool: True if the signature is valid and the signer matches the expected name,\n                False otherwise.\n        '
        if not os.path.exists(file_path):
            logging.error(f"Error: Executable not found at '{file_path}'.")
            return False
        try:
            command = ['certutil', '-dump', file_path]
            logging.debug(f"Verifying signature and signer for '{file_path}' using certutil.exe...")
            result = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            output = result.stdout
            if 'Verified' not in output:
                logging.debug('Signature verification failed. The file is not properly signed or is corrupted.')
                logging.debug('--- Certutil Output ---')
                logging.debug(output)
                return False
            signer_found = False
            for line in output.splitlines():
                if line.strip().startswith('Subject:'):
                    actual_signer = line.split('Subject:')[1].strip()
                    logging.debug(f"Signer found: '{actual_signer}'")
                    if actual_signer == expected_signer:
                        logging.debug('Signer matches the expected publisher.')
                        return True
                    else:
                        logging.debug(f"Signer mismatch. Expected '{expected_signer}', but found '{actual_signer}'.")
                        return False
            logging.debug("Could not find the signer's name in the certificate dump.")
            return False
        except FileNotFoundError:
            logging.error("Error: 'certutil.exe' not found. This should not happen on a standard Windows installation.")
            return False
        except Exception as e:
            tb = traceback.format_exc()
            logging.debug(f'An unexpected error occurred during verification: {e} \n {tb}')
            return False

github_updater = GithubUpdater()
