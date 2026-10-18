# Windows maintained application installer

The current recipe packages the actual recovered-plus-maintained Qt application as
`CacheCAD.exe` and `CacheCAD-Setup.exe`. This is an unsigned development build of
the real application path. Release rights, Google verification, controlled live
sync and CAD compatibility remain release gates. Neutral authored artwork replaces
upstream assets.

## What is included

- Real Google Desktop OAuth with state, PKCE and verified identity; one app-owner
  client configuration, not an OAuth project for every end user.
- Windows CurrentUser DPAPI session storage, refresh, local sign-out and separate
  explicit Google disconnect/revocation.
- The actual maintained Drive client, local scanner, analyzer and executor.
- Bundled Python, Qt, official Google API/auth SDKs, discovery data, CA certificates
  and dependency license notices. No installed Python is required.
- Hash-verified maintained source under `_internal/app-source`. Development
  synthetic launchers, the obsolete original entrypoint and retired vendor account,
  telemetry and updater modules are excluded from the application payload.

The default CI build contains no owner OAuth configuration. It opens the genuine
signed-out application with a visible distributor-setup requirement. It cannot
sign in until the application owner supplies its client metadata. See
[owner Google setup](PRODUCTION_GOOGLE_SETUP.md). No real account, grant, token or
CAD project is used by CI.

The earlier offline installer and identity-only launcher remain historical test
work. Their success does not prove this new artifact works. Use the matching new
commit's terminal CI report.

## Download and try

1. Open a successful run in the private repository's Actions tab. Download
   `CacheCAD-Windows-Application-<commit>` from Artifacts.
2. Extract the ZIP and find `CacheCAD-Setup.exe`, `installer.sha256`,
   `build-provenance.json` and `test-results/installed-runtime.json`. Artifact ZIPs
   may preserve subdirectories.
3. Check the installer SHA-256 matches both provenance and the terminal report.
   The report must say `status: passed` for that same installer digest.
4. Install as your normal Windows user and open its CacheCAD Start Menu shortcut.
   The unsigned publisher/reputation warning is expected; this guide does not
   recommend disabling security protections.
5. An unconfigured build explains that its distributor must configure Google.
   End users should not create their own OAuth clients. Before any real project
   transfer, use an approved disposable test project and retain independent backups.
6. Close normally. Uninstall through Installed Apps or the Start Menu uninstaller.
   Persistent application state and protected login are separate from installed
   program files; use local sign-out before uninstalling if you want the remembered
   session removed. Google revocation is a separate explicit action.

Artifacts expire after 30 days. Source recipes remain in the repository.

## Build on Windows

Use Windows x64, CPython 3.12.10 x64 and the official Inno Setup 6.7.1 compiler.
The build validates the actual compiler-engine banner and hashes ISCC.exe and
ISCmplr.dll; it does not silently download or accept a different compiler.

```powershell
py -3.12 -m venv .venv-windows
.\.venv-windows\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r requirements/windows-build.lock -r requirements/runtime-windows.lock
.\.venv-windows\Scripts\python.exe packaging/windows/build.py --output dist/windows
.\.venv-windows\Scripts\python.exe packaging/windows/test_installer.py --installer dist/windows/installer/CacheCAD-Setup.exe --report-dir dist/windows/test-results
```

The output directory must be new. The owner may explicitly supply
`--client-config PATH` to the build after configuring its own Desktop client. The
build validates accepted metadata and records it in the packaged source manifest.
Never provide a service-account key, access/refresh token or upstream credential.
Client creation, console changes and live grants are owner actions, not build steps.
Dependency lock scope is documented in [WINDOWS_DEPENDENCIES.md](WINDOWS_DEPENDENCIES.md).

## Installed-app evidence

The Windows integration test installs the actual installer into a fresh directory
with spaces, with a unique Start Menu group. It checks installer exit status,
installed executable, resolved shortcut target/arguments and per-user uninstall
registration. It launches the installed EXE with a sanitized environment and
system-only PATH, rather than running source or a portable shortcut.

The real signed-out window must render and exit cleanly using both the offscreen
and native Windows Qt plugins. Startup-check mode denies network, browser and
process effects, skips session restoration, uses a disposable profile, and never
fabricates a signed-in user, credential or successful scan. Reports require verified
source loading, no credentials/Drive client, no callback errors or external attempts,
clean worker shutdown, unchanged installed bytes and removed disposable profiles.
The test finally invokes the installed uninstaller and verifies removal of the
installation, Start Menu group and uninstall registry entry.

These checks prove bounded installed startup, not live OAuth, real Drive sync,
manual visual QA, all Windows versions, revision race safety or SolidWorks mates.
Native startup is mandatory; offscreen success is not a substitute. DPAPI also has
a Windows-only synthetic round-trip test. Protocol/session tests use injected
fixtures outside the production payload.

Require `installed-runtime.json` terminal success and its matching digest; a
successful build alone is insufficient. Windows installer/EXE byte-for-byte
reproducibility is not claimed. Source assembly is separately deterministic.

## References

- [Google owner setup and verification](PRODUCTION_GOOGLE_SETUP.md)
- [PyInstaller runtime paths](https://pyinstaller.org/en/stable/runtime-information.html)
- [Inno Setup least-privilege installs](https://jrsoftware.org/ishelp/topic_setup_privilegesrequired.htm)
- [Inno Setup installer options](https://jrsoftware.org/ishelp/topic_setupcmdline.htm)
- [Windows 2022 runner inventory](https://github.com/actions/runner-images/blob/main/images/windows/Windows2022-Readme.md)
