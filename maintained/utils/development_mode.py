"""Explicit, process-local configuration for the reviewed offline launcher.

This switch is not a security sandbox and cannot make arbitrary adapters safe.
The launcher must install its I/O tripwires and isolate the profile before
calling configure_offline, then import the real application. Normal startup
never calls this function. There is deliberately no environment-variable toggle.
"""
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Callable


@dataclass(frozen=True)
class OfflineDevelopmentConfig:
    profile_root: Path
    drive_client_factory: Callable[[], object] | None = None

    def require_profile_path(self, path: str | Path) -> Path:
        """Reject paths resolving outside the disposable development profile."""
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.profile_root):
            raise ValueError('Offline development paths must remain inside the disposable profile')
        return resolved


_configuration: OfflineDevelopmentConfig | None = None


def configure_offline(profile_root: str | Path, drive_client_factory=None) -> OfflineDevelopmentConfig:
    """Activate once, before application imports, from a reviewed dev launcher.

    A fixture factory takes no arguments and must return an adapter declaring
    is_offline_fixture = True. It never receives credentials. That declaration
    catches accidental live-client injection; the launcher supplies isolation.
    """
    global _configuration
    if _configuration is not None:
        raise RuntimeError('Offline development is already configured for this process')
    if any(name.startswith(('app.', 'core.', 'gui.')) or
           name in ('utils.paths', 'utils.app_settings', 'utils.cloud_logging',
                    'utils.oauth_credential_import') for name in sys.modules):
        raise RuntimeError('Configure offline development before importing the application')
    root = Path(profile_root).resolve(strict=True)
    if not root.is_dir() or Path.cwd().resolve() != root:
        raise ValueError('The disposable profile must exist and be the current working directory')
    if drive_client_factory is not None and not callable(drive_client_factory):
        raise TypeError('The offline Drive fixture factory must be callable')
    _configuration = OfflineDevelopmentConfig(root, drive_client_factory)
    return _configuration


def current() -> OfflineDevelopmentConfig | None:
    return _configuration


def is_enabled() -> bool:
    return _configuration is not None
