# PyInstaller onedir recipe. Application code is verified source data, never PYZ.
import os
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

sys.path.insert(0, str(Path(SPECPATH)))
from support import (LIVE_PACKAGES, SOURCE_PACKAGES, TYPE_IMPORTS, excluded_frozen_module,
                     stdlib_hidden_imports, verified_payload)

source = Path(os.environ['CACHECAD_APP_SOURCE']).resolve(strict=True)
payload = verified_payload(source)
datas = [(str(path), str(Path('app-source', name).parent)) for name, path in payload.items()]
datas += collect_data_files('tzdata')
datas += collect_data_files('PySide6', includes=['**/*LICENSE*', '**/*COPYING*', '**/*NOTICE*', '**/licenses/**'])
for package in ('PySide6', 'PySide6-Essentials', 'PySide6-Addons', 'shiboken6',
                'Send2Trash', 'tzlocal', 'tzdata', 'pyinstaller',
                'google-auth', 'cachetools', 'rsa', 'pyasn1', 'pyasn1-modules'):
    datas += copy_metadata(package)
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if not python_license.is_file():
    raise FileNotFoundError('CPython license notice missing from the build interpreter')
datas += [(str(python_license), 'third-party-notices/Python'),
          (str(Path(SPECPATH) / 'OFFLINE-README.txt'), '.')]

hidden = stdlib_hidden_imports(payload.values())
hidden += ['PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'PySide6.QtNetwork', 'shiboken6']
hidden += collect_submodules('send2trash') + collect_submodules('tzlocal') + ['tzdata']
hidden += list(TYPE_IMPORTS)
a = Analysis(
    [str(source / 'development' / 'launch.py')],
    pathex=[], binaries=[], datas=datas, hiddenimports=sorted(set(hidden)),
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=list(SOURCE_PACKAGES + LIVE_PACKAGES), noarchive=False, optimize=0,
)
# A second fail-closed check protects against future hook changes shadowing source.
for name, *_ in a.pure:
    if excluded_frozen_module(name):
        raise ValueError('Unapproved frozen application/account module: ' + name)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [('u', None, 'OPTION'), ('X utf8', None, 'OPTION')],
    exclude_binaries=True, name='CacheCAD-Offline', debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=True,
    disable_windowed_traceback=False, contents_directory='_internal',
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='CacheCAD-Offline')
