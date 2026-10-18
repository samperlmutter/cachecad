# Separate onefile helper can run from the user's update cache during replacement.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

source = Path(os.environ['CACHECAD_APP_SOURCE']).resolve(strict=True)
datas = []
for filename in ('update-policy.json', 'update-root.json'):
    path = source / 'application' / filename
    if path.is_file():
        datas.append((str(path), 'application'))
datas += collect_data_files('certifi')
for distribution in ('tuf', 'securesystemslib', 'cryptography', 'cffi'):
    datas += copy_metadata(distribution)
hidden = []
for package in ('tuf', 'securesystemslib', 'cryptography'):
    hidden += collect_submodules(package)
a = Analysis([str(source / 'application/update_helper.py')],
             pathex=[str(source / 'src')], binaries=[], datas=datas,
             hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['PySide6', 'googleapiclient', 'google.auth'], noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas,
          [('u', None, 'OPTION'), ('X utf8', None, 'OPTION')],
          name='CacheCADUpdateHelper', debug=False, strip=False, upx=False,
          console=True, bootloader_ignore_signals=False,
          version=os.environ['CACHECAD_HELPER_VERSION_RESOURCE'])
