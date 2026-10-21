# Permafrost desktop

The current application lives in `crates/permafrost-core`, `src-tauri`, and
`desktop`. It ships Rust and a React/TypeScript interface. The recovered Python
application is reference material and is not bundled.

The owner removed 3D previews from the product scope. Local files are revealed in
Finder on macOS or Explorer on Windows; files stored only in Drive must first be
downloaded through the reviewed sync workflow. No geometry parsing, conversion
runtime, WebAssembly reader, model worker, or converted-model cache remains.

## Behavior inventory

| Behavior | Implementation and verification |
| --- | --- |
| Google browser sign-in, token persistence, refresh, sign-out, revocation | Native OAuth with OS-protected credentials; protocol and disposable-account checks |
| Create, edit, remove, reorder and mark projects read-only | SQLite persistence, native validation and interface interaction tests |
| Recursive local, shared-drive and My Drive scans | Paginated Drive reads, MD5 comparison, portable paths, ignored CAD lock files |
| New files, revisions, conflicts, deletes and renames | Three-way planner, explicit direction choices, last synchronized content |
| Reviewed uploads/downloads and destructive consent | Opaque plan/action identifiers, stale-plan rejection, rechecked source/target versions |
| Atomic verified transfers and recovery | Temporary staging, size/hash verification, durable journals, uncertain-write blocking |
| Revision retention, backups, progress, cancellation, activity | Native transfer engine, filesystem/HTTP fixtures and interface tests |
| Live local changes and periodic remote refresh | Recursive native watchers, independent metadata browsing, online refresh loop |
| Reveal local files without reading their contents | Project-bound path validation and native system file-manager reveal |
| Windows and macOS installers | Native build/install/workspace/reveal/uninstall CI on Windows x64 and both Mac architectures |
| Signed updates | Tampered-package rejection, verified install, restart and uninstall of disposable two-version builds |

## Architecture

`permafrost-core` owns SQLite state, filesystem boundaries, immutable scan
snapshots, three-way planning, journaled transfers, Google OAuth and Drive HTTP.
SQLite transactions record intent before external mutations. The backend owns
remote versions and rechecks transfer preconditions; incomplete scans never
produce a plan. One active scan/transfer is permitted at a time. Local metadata
browsing and file reveal remain independent of a slow or offline Drive operation.

`src-tauri` owns narrow IPC commands, folder selection, native file reveal, the OS
credential store, tray, single-instance handling, native watchers, signed updates,
and isolated installer/upgrade verification profiles. Reveal validates a relative
path against its saved project root, rejects traversal, internal paths, symlinks,
missing files and directories, then asks the native file manager to select it.
It does not read file contents or launch the file's associated CAD application.

`desktop` owns the interface, user selection and review flow. A file row exposes
its native Reveal action; Drive-only rows explain that download is required.
Errors from deleted or inaccessible files are displayed without disrupting sync.
OAuth credentials and bearer tokens never enter the WebView.

## Sync safety and freshness

Only reviewed, explicitly selected actions can write local files or Drive data.
Every replacement, removal or rename requires destructive confirmation. Local
backups are retained in `.permafrost/backups` when enabled, and remote deletions
use Drive trash. An interrupted write with an uncertain outcome blocks replay
until the user inspects and resolves it. Read-only projects prohibit Drive writes.

Native recursive watchers debounce saves, invalidate affected content hashes,
advance project generations and invalidate stale plans. Scans cache expensive
hashes without reusing an observation across a reported mutation. Local browsing
continues while signed out or waiting for Drive. The interface refreshes Drive
periodically while online, preserves still-valid selections, and marks retained
results stale/offline when verification cannot complete.

Downloads are streamed into staging, verified against the observed remote version
and checksum, then atomically committed after rechecking the local target. The
removed viewer's caches are no longer consulted or populated. Original project
files and recovery backups are unaffected by removing the viewer.

## Credentials and distribution

The owner supplied an approved Desktop OAuth client in ignored
`google-client-desktop.json`; local build helpers load it without logging its
contents. CI uses distributor configuration from repository variables and secrets.
The owner approved Google consent and generated-file tests in a disposable Drive
project. Live sign-in, upload/download, local notifications and remote refresh
were validated there without modifying unrelated Drive files.

On macOS, local development builds use a stable signing identity and fixed
credential helper. The helper accepts only callers signed with that identity;
Google sessions remain in the login Keychain. Rebuild checks use disposable
credentials and reject unrelated callers. The signing key stays outside source
in a dedicated development Keychain. Apple Developer ID signing and notarization
remain deferred at the owner's request; development signing is used for now.

Tagged `v2.*` builds produce signed updater artifacts and publish only after the
installer gates pass. Updater signature/version requirements remain enabled.
The two-version upgrade test uses a disposable identity, signing key, loopback
server and isolated profile. Production distributor credentials are excluded.

## Verification

Local checks include the planner/filesystem/journal/HTTP fixtures, project-bound
reveal validation, TypeScript production build, Vitest, Playwright, and Clippy
with warnings denied. Interface tests cover both Finder and Explorer labels,
correct project/path IPC, Drive-only rows, missing-file errors, native readiness,
watcher updates during stalled Drive calls, periodic refresh, consent, pagination,
renames, project edits and navigation recovery.

The installer smoke profile generates a text file in a disposable local project,
renders its actual native workspace/file list and reveal button, then performs a
real native reveal. A structured receipt is required before clean exit. Installer
verification checks that no CAD helper or WASM reader survived packaging, verifies
signatures where applicable, and uninstalls the artifact. All geometry-related
build dependencies and CI steps have been removed.

Windows verification additionally checks both installed shortcut targets, opens
the real Start menu and desktop links through the Windows shell, and verifies
their removal on uninstall. Beta installer downloads are staged flat with a
version, readable platform name, checksum, and launch instructions. Test reports
and legacy CacheCAD builds are separate, explicitly labeled artifacts. Staging
copies the verified installer without changing updater asset names or signatures.

Official references:

- https://v2.tauri.app/plugin/updater/
- https://v2.tauri.app/plugin/opener/
- https://developers.google.com/identity/protocols/oauth2/native-app
- https://developers.google.com/drive/api/guides/manage-uploads
