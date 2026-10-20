# Tauri rewrite

Branch: `codex/tauri-rewrite`. The immutable Python recovery and maintained
application remain reference material. The new application ships only the Rust
workspace and `desktop/` web UI; no Python runtime or vendor services.

## Behavior inventory and acceptance evidence

Each row is an acceptance requirement, not a claim of completion.

| Requirement | Reference | Required proof |
| --- | --- | --- |
| Google browser sign-in, persistent OS-protected tokens, refresh, sign-out, revoke | maintained/core/desktop_signin.py, google_session.py | Protocol tests plus disposable-account installed-app validation |
| Create/remove/reorder projects; existing Drive folder, configurable local root, read-only projects | recovered/core/project_manager.py | Persistence tests and UI interaction tests |
| Complete local and shared/My Drive recursive scans, MD5 equality, ignore CAD locks | maintained/core/local_client.py, drive_client.py | Filesystem and paginated HTTP fixtures; live shared-drive validation |
| New uploads/downloads, replacement revisions, conflict direction choices | recovered/core/sync_analyzer.py | Full planner truth table and end-to-end disposable project |
| Detect deletes using last synchronized content; explicit selection and destructive review | maintained/core/sync_consent.py | Stale-plan, changed-file, failed-scan, cancellation tests |
| Read-only mode blocks every remote mutation | recovered/gui/pages/selection_page.py | Backend authorization tests |
| Verified transfers, temporary files, atomic commit, durable recovery, no blind uncertain-write retry | maintained/core/transfer_journal.py | Crash/restart and interrupted network tests |
| Revision retention, local backups, transfer progress/cancel/retry, activity history | maintained/core/sync_executor.py | Runtime tests and interface validation |
| Settings, folder reveal, tray, startup scans, local diagnostics | recovered/utils/app_settings.py, maintained/gui/main_window.py | Installed-app interaction tests |
| Windows installer and macOS disk image built and installed/launched in CI | user requirement | Passing native CI artifacts and installer smoke evidence |
| Signed automatic update detection, download/install/restart; release on new version push | user requirement | Two-version signed installed-app upgrade validation |
| Three.js STL/OBJ/GLTF plus STEP/IGES/BREP via local OpenCascade worker | user requirement | Actual model fixtures, browser render and native WebView validation |
| Native SLDPRT/SLDASM sync, with preview of supported CAD exports | User narrowed preview scope to OpenCascade formats | Native-file byte preservation and clear export guidance |
| Beautiful, intuitive, keyboard-accessible responsive UI | user requirement | Rendered screenshots and working interaction checks |

## Architecture

`permafrost-core` owns project persistence, filesystem boundaries, immutable scan
snapshots, three-way plans, journaled transfers, Google OAuth/Drive and preview
preparation. SQLite transactions persist state before external writes. UI requests
reference opaque plan/action identifiers; the backend owns observed versions and
rechecks conditions. Incomplete scans never produce a plan. One active scan/transfer
at a time, cancellation checkpoints, bounded streams and off-UI-thread hashing.

`src-tauri` owns the native shell, narrow IPC commands, OS credential store, folder
pickers, tray, single instance, signed updates and installer smoke hooks.
`desktop` owns React/TypeScript presentation and Three.js rendering. No OAuth
credentials or bearer tokens cross into the WebView. CAD processing stays local.

## External integration gates

The repository has no approved distributor Google Desktop OAuth client or Apple Developer ID, or native SolidWorks reader license (native parsing is deferred by user direction).
These are genuine configuration/validation requirements. Never embed recovered
credentials, generate a pretend login, or describe companion STEP/STL exports as
native SolidWorks parsing. Development and installer smoke checks must be explicit
and isolated from user profiles and real project files.

Official implementation references:
- https://v2.tauri.app/plugin/updater/
- https://developers.google.com/identity/protocols/oauth2/native-app
- https://developers.google.com/drive/api/guides/manage-uploads
- https://github.com/kovacsv/occt-import-js
- https://threejs.org/docs/#examples/en/loaders/STLLoader


## Current local evidence

The initial implementation passes backend planner/filesystem/journal/HTTP-fixture
checks, TypeScript production build, Clippy with warnings denied, and Chromium
interaction tests including a real STEP import through OpenCascade WASM.
These checks do not establish live Google Drive behavior or installed-native parity.

Updater trust is configured with a generated public key in the Tauri configuration
and repository variable. The private release key is held in the repository secret
`TAURI_SIGNING_PRIVATE_KEY` and outside the checkout in
`~/.config/permafrost/release/updater.key` (mode 0600). Back up this key securely;
installed applications depend on the corresponding public key for future updates.
Stable `v2.*` version-tag pushes build and verify native installers, create signed
update artifacts and `latest.json`, then publish only after all platform gates pass.
No release tag has been created as part of initial branch development.


## Live freshness and native UI checks

Native recursive folder watchers debounce CAD saves (350 ms quiet period,
2 second maximum batch). Content events invalidate cached sync plans immediately.
Lock files and Permafrost staging/backups are excluded. Hash-free local browsing
works while signed out; selected model previews reload when local metadata changes.
A native 60-second timer requests a Drive refresh while the application is open,
including in the tray. Reconnection and returning to the app request an earlier
refresh. Operations never overlap; changes arriving mid-operation queue a refresh.
Failed refreshes retain the last successful results and disable sync until fresh.
Only unchanged conflict selections survive a successful refreshed plan.

On the local Mac, the actual `tauri build --debug --bundles app` bundle was opened
and controlled with native computer use. Verified: Permafrost black/white/cyan
branding, model controls, saving/restoring a tray preference, sanitized diagnostic
export, native folder selection, offline project creation, actual STEP import
(1 body, 12 triangles), and automatic UI updates for creating, editing, renaming,
and removing a disposable local file. This does not validate live Google grants.


The first Windows CI installer evidence (commit `4630a59`, run `39367328407`)
proves installation, a rendered native workspace/Three.js canvas, clean exit and
uninstallation. Its Mac gate correctly rejected an incomplete linker-only bundle
signature. Explicit ad-hoc bundle signing fixes that packaging defect; the local
Mac debug DMG then passed disk-image checksum, strict bundle signature, installation,
native render, clean exit, and removal checks. Ad-hoc signing does not establish
Developer ID/notarization or Gatekeeper acceptance for downloaded releases.
