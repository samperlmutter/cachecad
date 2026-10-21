# Install the Permafrost beta

Open a successful **Permafrost desktop** run in GitHub Actions and download the
installer artifact for your computer. The version is included in every name.

| Computer | Installer artifact |
| --- | --- |
| Windows 64-bit | `Permafrost-<version>-Windows-x64-Installer` |
| Mac with Apple Silicon (M1 or newer) | `Permafrost-<version>-macOS-Apple-Silicon-Installer` |
| Mac with an Intel processor | `Permafrost-<version>-macOS-Intel-Installer` |

Extract the artifact ZIP. Its top level contains one installer, `START-HERE.txt`,
and `SHA256SUMS.txt`. Artifacts ending in `Test-Reports` contain CI evidence.
Artifacts beginning with `Legacy-CacheCAD` contain the previous Python/Qt app.

## Windows

Run `Permafrost-<version>-Windows-x64-Setup.exe` as your normal Windows user.
On the last setup page, leave **Run Permafrost** and **Create desktop shortcut**
selected. Open **Start** and search for **Permafrost** to launch it later, or
double-click the **Permafrost** desktop shortcut.

The default executable is `%LOCALAPPDATA%\Permafrost\permafrost.exe`. Paste that
path into **Win+R** if you need to open an existing installation directly. A custom
installation uses the directory you chose during setup; its Start shortcut points
there. If closing the window sent the app to the system tray, the Start shortcut
shows the existing window again. You can also open it from its tray menu.

CI installs the actual distributed Windows installer, checks the Start menu and
desktop shortcut targets, launches each shortcut and verifies the native workspace,
then checks that uninstall removes both shortcuts and the executable.

## macOS

Open the DMG, drag **Permafrost** into **Applications**, and launch it from there.
Choose the Apple Silicon or Intel download that matches your Mac.

These are development builds. Production Windows signing and Apple Developer ID
signing/notarization are not configured. Keep operating system security settings
enabled. CI verifies the installed application and its signed update flow using
isolated test profiles; the beta still requires the owner's Google client setup.
