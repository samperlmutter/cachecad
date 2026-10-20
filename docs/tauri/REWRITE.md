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

The owner supplied an approved Google Desktop OAuth client in the ignored
`google-client-desktop.json`. Native development builds load it without logging
its contents; CI receives distributor configuration through repository variables
and secrets. Live consent, upload, download, and automatic local/remote refresh
have passed against a generated disposable Drive project; extended tests continue.
Apple Developer ID/notarization credentials remain unconfigured. Native SolidWorks
parsing is deferred by user direction.
The owner chose development ad-hoc signing for now; production signing is deferred.
Google's current consent flow shows an unverified-app notice for the supplied
Desktop client. The owner approved continuing that notice, granting identity and
Drive access, and testing generated files in a disposable QA folder. The remaining
live test uses only generated files in the QA project.
Browser consent has a ten-minute deadline and a visible, cancellable waiting status.
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

`tooling/tauri/verify_upgrade.py` builds two isolated application versions with a
throwaway signing key and distinct application identifier. It launches the older
installed app, serves a modified package followed by the valid package, requires
signature rejection before installation, and verifies the newer installed app
restarts and renders its native workspace/Three.js canvas. The updater also requires
the signed version to match the feed version. This harness passed locally on Apple
Silicon (0.0.1 → 0.0.2), and in CI on Windows and Apple Silicon (run
`39371435255`). Intel execution remains a CI gate. Its loopback transport
and automation hook are restricted to the `updater-qa` feature and disposable build
configuration; release jobs do not enable that feature.

The HTTP integration suite exercises all eight sync actions through the real
scanner, planner, transfer executor, SQLite journal and baseline tracking, including
native CAD binary preservation, stale-plan rejection, corrupted-download retention,
uncertain-write replay blocking and backend read-only enforcement.
An upload larger than 8 MiB verifies continuation after Google's resumable chunk
acknowledgement and byte-for-byte completion. File rows are grouped by path once
per plan and rendered in pages of 100; a 350-file interface check verifies search
and selections across pages.


## Live freshness and native UI checks

Native recursive folder watchers debounce CAD saves (350 ms quiet period,
2 second maximum batch). Content events invalidate cached sync plans immediately.
Lock files and Permafrost staging/backups are excluded. Hash-free local browsing
works while signed out; selected model previews reload when local metadata changes.
A native 60-second timer requests a Drive refresh while the application is open,
including in the tray. Reconnection and returning to the app request an earlier
refresh. Operations never overlap; changes arriving mid-operation queue a refresh.
Read-only local metadata browsing runs independently of remote comparisons and
transfers, so a stalled Drive request does not delay local file-list updates.
An interface test holds the Drive request indefinitely while verifying local
notifications replace and remove file rows.
Failed refreshes retain the last successful results and disable sync until fresh.
Only unchanged conflict selections survive a successful refreshed plan.

On the local Mac, the actual `tauri build --debug --bundles app` bundle was opened
and controlled with native computer use. Verified: Permafrost black/white/cyan
branding, model controls, saving/restoring a tray preference, sanitized diagnostic
export, native folder selection, offline project creation, actual STEP import
(1 body, 12 triangles), and automatic UI updates for creating, editing, renaming,
and removing a disposable local file. The owner subsequently completed Google
consent. Actual uploads of a STEP file and text file, a byte-verified download,
automatic remote discovery without pressing Scan, and automatic local revision
detection passed in the disposable project.
The native journal confirms 11 completed live operations covering all eight
action kinds: new uploads/downloads, upload/download revisions, both conflict
directions, Drive trash, and local removal as part of a remote folder move.
Replacement backups and downloaded CAD bytes were independently checked. A
Drive-only STEP preview rendered before the reviewed download promoted its cache.

Mac local builds use a persistent per-machine certificate stored in a dedicated
development Keychain. Only codesign and Apple's signing tools are authorized for
its non-extractable private key. `desktop:setup-signing` creates it outside the
checkout. Builds unlock that signing Keychain using a mode-0600 local password
file and lock it afterward; no trusted root is added.

Native testing confirmed a prompt-free restart but reproduced the prompt after
rebuilding with the same certificate. The login Keychain has a separate partition
gate: the saved Google entry lists individual code hashes. A disposable test
reproduced a successful creator read and a denied rebuilt reader despite equal
designated requirements. Apple's implementation classifies self-signed code by
its code hash: https://github.com/apple-oss-distributions/Security/blob/main/securityd/src/clientid.cpp

Local Mac debug builds therefore use a small fixed credential helper outside the
checkout. The helper remains unchanged across app rebuilds and authenticates its
live parent using both the Permafrost identifier and the helper's signing
certificate. It accepts only get/set/delete requests for Permafrost's Google
service. Sessions stay in the login Keychain; secrets travel only through a
private pipe to native Rust. The app checks the helper's pinned file SHA-256 and
authenticates its running process against its pinned code hash before sending
credential bytes. A native regression accepts a process with the exact hash and
rejects a wrong or malformed requirement. The helper uses hardened runtime.
Release builds use the normal
OS credential store and contain no development-helper path or dependency.

`desktop:verify-keychain` uses a disposable generated credential in the actual
login Keychain. Two different authenticated callers restore and update it;
unrelated identifiers, the right identifier without the certificate, and direct
invocation by another process are rejected. The test removes its generated item.
Both Mac CI jobs run this regression. After the owner approved the helper for
the existing Google entry, native computer use verified saved-session restoration
after a complete quit/relaunch, forced process termination, and a changed app
rebuild. The main executable's SHA-256 changed from `3c57f60c…` to `70e6ef08…`;
the fixed helper stayed unchanged. These launches restored the account and
completed live Drive comparisons without another credential prompt.

## Hardening follow-up

The owner accepted the rewrite phase and requested chaos testing and a UX audit.
The complete installer workflow passed on Windows, Apple Silicon, and Intel in
run `39373561854`, including tampered-update rejection and actual two-version
signed replacement, restart, native rendering, and removal.

The first follow-up browser run reproduced four issues before fixes: Project
options had no edit flow; empty meshes and non-finite coordinates were accepted;
and an unexpected preview setup exception could blank the interface. Regression
tests now verify editing name/read-only without changing the connection or
duplicating projects, clear invalid-model errors followed by successful preview,
and workspace recovery after an injected graphics-resource failure. All 18
browser tests passed after those fixes.

Filesystem probes also reproduced an indefinitely blocked named-pipe reader and
an unrelated disconnected project preventing project settings changes. File reads
now reject special files and final symlinks without blocking; stored project roots
are canonical and reserve their paths while disconnected. The 31-test core suite
passes with both regressions. Further stress and native restart checks continue.

Targeted stress checks now pass: 60 seeded navigation actions with injected Drive
failures and offline events; 20 rapid STL and OBJ replacements with no remaining
workers; 400 atomic native CAD replacements observed through 800 hash inspections;
and 128 seeded journal shutdown/reopen cycles preserving completed baselines and
requiring explicit review of every uncertain operation. STL/OBJ parsing and normal
generation now run in disposable workers alongside the existing OpenCascade
worker. Preview setup failures also release already-created renderer resources
before the recovery screen appears.

Additional failing probes caught a GLB texture leak, cancellation waiting for a
stalled root-folder metadata request, a duplicate initial scan when connecting a
project, and Activity navigation replacing the active operation's Cancel state.
All now have passing targeted regressions. Textures and ImageBitmaps are released
when a model closes; scans and token refresh waits honor cancellation; refresh
starts are serialized; and reading Activity no longer owns the operation status.
Cancelling also clears queued automatic retries and displays a neutral status
instead of an offline error. Direction cards say changes “to apply” because their
counts include renames and removals as well as transfers.

The integrated local pass completed 42 Rust tests, 21 frontend unit tests, and all
24 browser interaction/stress tests, plus the production frontend build and
Clippy with warnings denied. Settings and recovery screenshots were visually
reviewed. Installer CI run `39424611440` passed on Windows, Apple Silicon, and
Intel, including the actual login-Keychain helper regressions on both Mac jobs.

On October 20, native computer use also exercised the live disposable QA project:

- Read-only mode suppressed uploads while the watcher detected a newly created
  local file and 120 rapid create/edit/rename/remove mutations. A local-only file
  was incorrectly labelled Synced; it now says Local only with selection disabled.
  A browser regression reproduced the old label before the fix, and the rebuilt
  native app verified the corrected label.
- A test harness killed only the QA app process while its generated 96 MiB
  upload was recorded as running. Reopening changed that record to uncertain,
  without advancing its baseline or replaying the upload. An independent Drive
  listing showed no completed file, and the local payload still matched its
  original MD5. Activity required the explicit recovery acknowledgement before
  marking the record reviewed. The generated payload was retained outside the
  project after inspection.
- Temporarily renaming only the QA root produced an unavailable-folder message
  and disabled sync. Restoring it recovered scans and live watching. This exposed
  a stale warning after recovery; watcher health now clears its own warning only
  when all project watches are healthy, preserving independent preview-cache
  warnings.
- A generated STL rendered through the actual native mesh worker with its
  one-triangle geometry and camera controls visible. The fixture was then moved
  outside the QA project. Prior native STEP and Drive-only preview checks remain
  recorded above.

The final local acceptance pass completed 42 Rust tests, 21 frontend unit tests,
all 25 browser interaction/stress tests, the production frontend build, strict
Mac bundle signature verification, and all-features Clippy with warnings denied.
The final native reconnect check recovered automatically and cleared the warning
without dismissal. The disposable project returned to three synchronized files,
with no running or uncertain transfer records. Temporary Mac idle-time changes
used for computer testing were restored to their original values afterward.

A further recovery test forcibly kills a separate process with SQLite still open,
both during a running operation and after completion was committed. Reopening the
database preserves the completed baseline and marks the interrupted operation
uncertain without advancing its baseline. Project removal remains blocked until
explicit review. This tests OS process loss without a clean connection close or
WAL checkpoint, using only disposable local data.

Windows CI reproduced an atomic-save observation whose canonical path moved into
NTFS `$Extend/$Deleted`. The scanner still rejects that out-of-root observation,
but now reports a changed snapshot requiring another scan instead of telling the
user to rename a valid filename. The focused `core_test_filter` workflow input
reproduces this guard without rebuilding installers and passed on Windows in run
`39381005050`. Temporary path probes were
removed after capturing the cause.

The cached-preview CI image mismatch was limited to 665 pixels of the Reset
button's CSS hover fade (at most two channel values); scene pixels were identical.
That comparison now disables finite CSS animations while retaining exact image
equality. Lazy preview, worker, and native-plugin dependencies are pre-optimized
to prevent Vite from reloading an in-progress dev workspace on first use.

The onboarding sample now goes through binary STL export and the real mesh
worker. Native installer readiness requires that reader as well as visible
geometry; failed previews write structured failure evidence. This exercises the
same worker-loading path in native WebView2 and WKWebView, alongside the browser
contract regression, without touching user profiles or Google credentials.


The first Windows CI installer evidence (commit `4630a59`, run `39367328407`)
proves installation, a rendered native workspace/Three.js canvas, clean exit and
uninstallation. Its Mac gate correctly rejected an incomplete linker-only bundle
signature. Explicit ad-hoc bundle signing fixes that packaging defect; the local
Mac debug DMG then passed disk-image checksum, strict bundle signature, installation,
native render, clean exit, and removal checks. Ad-hoc signing does not establish
Developer ID/notarization or Gatekeeper acceptance for downloaded releases.
Production version-tag releases fail before publication if Mac Developer ID or
notarization credentials are missing; development installer gates use ad-hoc signing.

Intel GitHub Mac runners have no usable WebGL acceleration. A forced-no-WebGL
browser regression first reproduced that failure. The preview now falls back to
Three.js SVGRenderer using the same model geometry, scene, and controls, with a
50,000-triangle software limit to protect responsiveness. An actual STEP import
and wireframe interaction pass without WebGL. CI run `39373210625` built an x64
DMG and installed it on `macos-15-intel`: the native workspace and 511 × 445 model
render passed using `software-svg`, followed by clean exit and removal. Temporary
readiness probes were removed after this proof; native stdout/stderr remain useful
installer failure artifacts.

Local comparisons cache hashes using size and a file metadata fingerprint. Unix
fingerprints include device, inode and change time; Windows includes creation time
and attributes in addition to modification time. Scans rehash changed files,
execution independently rehashes every selected file, and a complete hash pass is
forced at least every 15 minutes of remote refreshes. Watcher errors discard cached
hashes and plans. Native watcher maintenance runs every 10 seconds to reconnect
removed, replaced or unavailable project roots without restarting the application.
Content events evict affected cached paths and directory descendants even if a
writer preserves timestamps. Coarse root events evict the whole project cache.
Generation, cache and plan invalidation share scan completion's lock order, so a
concurrent scan cannot reinsert hashes after an event invalidates them.
