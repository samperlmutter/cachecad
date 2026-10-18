# Windows offline-development installer

This recipe builds an **unsigned Windows x64 development tryout** of the actual
reconstructed Qt application. It is not the vendor installer or a production
release. Use only for the user's personal/noncommercial development evaluation;
redistribution rights for recovered code have not been established.

## What starts

`CacheCAD-Offline.exe` opens `gui.main_window.MainWindow` and the real maintained
controller. It uses authored placeholder images and a new disposable temporary
profile on every launch. Closing normally removes that profile. Sign-in, live
Drive transfers, updates, external links, and real-project selection are disabled.
The visible console is intentional for diagnostics.

Python, PySide6/Qt and the dependencies required for this mode are bundled in a
PyInstaller onedir layout. An installed Python is not needed to launch it. Qt DLLs
remain separate, and upstream package metadata/license notices plus CPython's
license are retained in `_internal`. No vendor artwork or credentials are used.
The source is `_internal/app-source`; an early source-only import mechanism reads
reviewed source bytes after hash verification. Application packages are excluded
from PyInstaller's frozen module archive, so archived bytecode cannot substitute
for the source that was checked. Google/account libraries are excluded as well.

The offline checks are tripwires for audited code, **not an OS security sandbox**.
Hash validation is not code signing and does not authenticate a maliciously
replaced executable or manifest. Do not supply credentials or real CAD projects.

## Build locally on Windows

Use Windows x64, 64-bit CPython 3.12.10, and a clean virtual environment. The pinned
Inno Setup 6.7.1 compiler must already be installed from its official source.
The build script does not install/download tools or accept a replacement compiler
silently. In CI the expected compiler is provided by the Windows runner image;
its actual reported version and executable SHA-256 are recorded and checked.

From the repository root, in PowerShell:

```powershell
py -3.12 -m venv .venv-windows
.\.venv-windows\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r requirements/windows-build.lock -r requirements/runtime-windows.lock
.\.venv-windows\Scripts\python.exe packaging/windows/build.py --output dist/windows
.\.venv-windows\Scripts\python.exe packaging/windows/test_installer.py --installer dist/windows/installer/CacheCAD-Offline-Development-Setup.exe --report-dir dist/windows/test-results
```

The output directory must be new. Use a new path for another build. If ISCC is
outside its normal installation directory, pass `--iscc C:\path\to\ISCC.exe`.
The exact version requirement still applies. Lock scope and package provenance
are documented in [WINDOWS_DEPENDENCIES.md](WINDOWS_DEPENDENCIES.md).

The build includes only manifest-checked application data, checks both Windows
and offscreen Qt platform plugins are present, then compiles the per-user
installer. It does not start the installed application automatically. The setup
adds Start Menu launch/uninstall shortcuts, an uninstall entry and no services,
autorun entries, file associations, or network rules. The default directory is
`%LOCALAPPDATA%\Programs\CacheCAD Offline Development`. No elevation is requested.

## What the installed-app test verifies

The integration test refuses to run if this development app is already installed
for the current user. It installs into a new temporary directory with spaces in
its name and a unique Start Menu group. It checks the executable, shortcut and
per-user uninstall registration before launching the installed executable with
an environment stripped of Python/Qt overrides and a system-only PATH.

Four launches must pass with the actual Qt GUI rendered:

1. `success` with the offscreen Qt plugin: one synthetic file scan and upload plan.
2. `lifecycle` with offscreen: cancel, injected failure, retry, and stale callbacks.
3. `close-pending` with offscreen: close during a delayed scan, without DB writes.
4. `success` with the native Windows Qt plugin, proving that plugin starts too.

Every result must report frozen execution and the verified-source loader, the
expected GUI, platform and scan outcomes, zero external attempts, zero forbidden
account modules/credential files, no callback errors, and no remaining workers.
The full installation tree must be byte-unchanged and disposable profiles removed
after each run. Finally it invokes the installed uninstaller and requires the
install directory, unique Start Menu group and uninstall registry entry to vanish.
Uninstall is attempted even if a smoke scenario fails.

Native Windows startup is mandatory in the integration gate; it is not silently
replaced with offscreen success. This is not manual visual QA or a comprehensive
Windows-version compatibility test. It does not validate real accounts or sync.

## Evidence and status

- `build-provenance.json`: commit, dirty-tree status, Python/runner/tool versions,
  lock/recipe/source-manifest hashes, installer hash and full bundle file hashes.
- `installer.sha256`: the installer digest for this build.
- `test-results/installed-runtime.json`: terminal install/launch/uninstall status.
- Per-scenario JSON and stdout/stderr logs, plus Inno install/uninstall logs.
- `build-logs/`: assembly, PyInstaller, compiler-version and compiler logs.

A successful build alone leaves installed-runtime verification **pending** in
build provenance; require the matching installer SHA-256 and `status: passed` in
the integration report. Portable source/unit tests cannot establish Windows
startup. A Windows run has not been performed merely because this recipe exists.
Do not describe the EXE/installer as byte-identical across builds: timestamps,
compiler and runner inputs are not completely pinned, and repeat-build binary
reproducibility has not been demonstrated. The reconstructed source assembly has
its own separate deterministic checks.

The installer is unsigned. Windows may warn about its publisher/reputation; this
guide does not recommend disabling security protections. Review its source and
provenance and test only where you are comfortable running the development build.

## Primary references

- [PyInstaller spec files and data](https://pyinstaller.org/en/stable/spec-files.html)
- [PyInstaller runtime paths](https://pyinstaller.org/en/stable/runtime-information.html)
- [Inno Setup least-privilege installs](https://jrsoftware.org/ishelp/topic_setup_privilegesrequired.htm)
- [Inno Setup compiler options](https://jrsoftware.org/ishelp/topic_compilercmdline.htm)
- [Inno Setup install options](https://jrsoftware.org/ishelp/topic_setupcmdline.htm)
- [Inno Setup uninstall options](https://jrsoftware.org/ishelp/topic_uninstcmdline.htm)
- [Official Windows 2022 runner image inventory](https://github.com/actions/runner-images/blob/main/images/windows/Windows2022-Readme.md)
