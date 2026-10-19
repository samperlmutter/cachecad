"""Explicit configuration for the real application, separate from offline tools.

The installed app's owner supplies a Desktop OAuth client once. There is no
end-user OAuth client picker, embedded vendor credential, or implicit login.
"""
from dataclasses import dataclass
from pathlib import Path
import os
import sys


@dataclass(frozen=True)
class ApplicationConfig:
    profile_root: Path
    client_config_path: Path
    startup_check: bool = False

    @property
    def session_path(self):
        # On macOS this is a Keychain identity, never an on-disk token file.
        name = 'google-session.keychain' if sys.platform == 'darwin' else 'google-session.dpapi'
        return self.profile_root / 'app_data' / name


_configuration: ApplicationConfig | None = None


def default_profile_root():
    """Keep every persisted setting/token separate from old CacheCAD installs."""
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
        return base / 'CacheCADReconstructed' / 'v1'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'CacheCADReconstructed' / 'v1'
    base = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share'))
    return base / 'cachecad-reconstructed' / 'v1'


def configure_application(profile_root, client_config_path, *, startup_check=False):
    global _configuration
    if _configuration is not None:
        raise RuntimeError('The application is already configured')
    # This reviewed, stdlib-only exclusion module must run before profile work.
    if any((name.startswith(('app.', 'core.', 'gui.')) and name != 'core.windows_update_install') or
           name in ('utils.paths', 'utils.app_settings') for name in sys.modules):
        raise RuntimeError('Configure application paths before importing the application')
    from utils.development_mode import current as development
    if development() is not None:
        raise RuntimeError('Real application and offline development are separate modes')
    profile = Path(profile_root).resolve(strict=True)
    if not profile.is_dir() or Path.cwd().resolve() != profile:
        raise ValueError('The application profile must be the current working directory')
    client = Path(client_config_path).absolute()
    _configuration = ApplicationConfig(profile, client, bool(startup_check))
    return _configuration


def current():
    return _configuration
