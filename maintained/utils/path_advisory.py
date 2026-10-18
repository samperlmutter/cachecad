"""Visible, non-mutating OneDrive advisory for actual application path selection."""
import os
from PySide6.QtWidgets import QMessageBox
from utils.onedrive_guard import assess_project_path

_ACKNOWLEDGED = set()


def confirm_project_location(path, parent=None):
    """Ask before using a suspected OneDrive path; no result certifies safety."""
    env = {key: os.environ.get(key, '') for key in (
        'OneDrive', 'OneDriveConsumer', 'OneDriveCommercial')}
    assessment = assess_project_path(str(path), env=env)
    if assessment.status not in ('configured_root_match', 'name_hint'):
        return True
    key = (assessment.normalized_path, assessment.matched_root, tuple(sorted(env.items())))
    if key in _ACKNOWLEDGED:
        return True
    message = (
        'This project folder appears to be inside OneDrive. Running two sync tools '
        'on the same CAD files can cause conflicts or unexpected replacements. '
        'A folder outside OneDrive is recommended. Detection is based on configured '
        'paths and folder names, not verified sync activity. Continue with this folder?')
    reply = QMessageBox.warning(parent, 'OneDrive project folder', message,
                               QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
    if reply != QMessageBox.Yes:
        return False
    _ACKNOWLEDGED.add(key)
    return True
