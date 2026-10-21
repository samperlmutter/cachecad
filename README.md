# Permafrost desktop rewrite

The Tauri 2 rewrite lives in `desktop/`, `src-tauri/`, and
`crates/permafrost-core/`. It replaces the Python runtime with Rust,
React/TypeScript, and SQLite. File actions reveal the local file in Finder on
macOS or Explorer on Windows. The Python recovery below remains reference
material and is not shipped in the Tauri application.

For beta downloads and help finding the installed Windows app, see
[Install Permafrost](docs/tauri/INSTALL.md). Current downloads are named
`Permafrost-<version>-<platform>-Installer`; older Python/Qt builds are labeled
`Legacy-CacheCAD`.

```sh
# Node 22 and stable Rust.
# Native Tauri prerequisites are required.
npm ci
# macOS only, once: npm run desktop:setup-signing
npm run desktop:dev
# Standalone native debug application: npm run desktop:debug
# Browser-only sample workspace: npm run dev, then http://127.0.0.1:1420/?demo=1
npm run check
npm run test:e2e
# macOS: npm run tauri build -- --bundles app,dmg
# Windows: npm run tauri build -- --bundles nsis
```

The desktop workflow builds, installs, launches, verifies the native workspace
and file reveal action, and uninstalls Windows and macOS installers. Signed updates use version
tags and the repository's release configuration. Google sign-in requires the
owner's Desktop OAuth client at build time; no recovered credentials are used.
For local development, place the approved Desktop client configuration at the
ignored `google-client-desktop.json` path (or set `PERMAFROST_GOOGLE_CLIENT_FILE`).
The desktop helpers load it into the native build without logging its contents.
On macOS, run `npm run desktop:setup-signing` once. Debug builds use that machine's
signing certificate and a fixed credential helper so login-Keychain build-hash
checks do not prompt on every app rebuild. The helper accepts only Permafrost
callers signed with the same certificate; Google sessions remain in the login
Keychain. Approve its first saved-session access with Always Allow. The signing
key lives in a separate development Keychain, unlocked only while signing with a
mode-0600 local password file. The local keys and compiled helper stay outside the repo and do not
replace Developer ID signing and notarization for distribution.
`npm run desktop:verify-keychain` checks rebuilt callers and rejects unrelated
ones using only a disposable generated credential.
The feature/validation inventory and remaining release gates are tracked in
[the rewrite document](docs/tauri/REWRITE.md).

Every locally available file has a Reveal in Finder/Explorer button, including
native SolidWorks files and other binary formats. Drive-only files must be
downloaded through the reviewed sync flow before they can be revealed locally.
The app does not parse CAD geometry or bundle a 3D viewer or conversion runtime.

---

# CacheCAD source recovery and maintenance

The `recovered/` directory contains source reconstructed from the original
CacheCAD 1.2.0 beta (2025-09-28) Python 3.12 code objects. Each claimed exact
module is compiled and compared recursively against the pinned original binary's
code, constants, names, signatures, flags and exception tables. Original source
locations, formatting and comments are not recovered. The reviewed
[recovery manifest](recovered/recovery-manifest.json) is the authoritative status
for all 49 first-party PYZ modules; the separate CArchive entrypoint is tracked
under `recovered/entrypoint/`.

Embedded credentials are intentionally removed from one recovered module. That
module is explicitly non-matching. No original credentials are included or used.
Original behavior and bugs remain in the recovery baseline.
The separate [`maintained/` patch layer](maintained/) contains narrowly reviewed
corrections with runtime regression tests against actual recovered functions. Matching compiled
structure is evidence of faithful recovery, not runtime validation or a claim
that the app is fixed.

The earlier `src/cachecad/` code is a new, evidence-informed offline replacement
prototype with mock services and safety tests. Its demo and packaged wheel are
**not the recovered original application**. It remains here as experimental
improvement work; its experimental fixes have not been merged into the recovered baseline.
The maintained layer patches original functions directly and records its intentional
bytecode differences separately.

## Assemble the maintained application source

```sh
python tooling/assemble_app.py /path/to/new-output-directory
```

This combines the immutable recovery with reviewed maintenance overlays and
new helpers, producing a hash-manifested application source tree and byte-reproducible
ZIP. See [assembly instructions](docs/ASSEMBLY.md) and [maintained changes and limits](maintained/README.md).
It includes scan failure/retry isolation, OneDrive path warnings and bounded
new-file transfer protection. Existing overwrite/delete selections now require
[explicit destructive consent and observed-version checks](docs/DESTRUCTIVE_SYNC_SAFETY.md).
Revision sync remains available, with a documented residual check/write race.
[Durable transfer recovery](docs/TRANSFER_RECOVERY.md) retains local evidence, distinguishes
unknown commits, and prevents blind retries of unresolved writes.
[Local crash reports](docs/CRASH_DIAGNOSTICS.md) record sanitized diagnostics without uploads.
[Crash hardening evidence](docs/CRASH_HARDENING_AUDIT.md) separates reproduced defects
from unconfirmed causes of reported crashes. This source artifact is not an installer.

## Run the actual application offline

The [offline development launcher](development/README.md) runs the actual assembled
GUI with real Qt and disposable synthetic scan fixtures. A separate hash-locked
Linux runtime and generated neutral placeholder assets are included. Authentication,
live services and transfers are disabled in this development mode. Hosted CI tests
startup, failure/retry/stale-result isolation and closing a pending scan.

## Windows application and Google sign-in

The maintained Windows application includes real Google Desktop OAuth,
Windows-protected persistent credentials, refresh and the actual Drive client,
with neutral placeholder artwork. The distributor must supply its own reviewed
OAuth client configuration; end users do not create their own OAuth clients.

See [production Google setup](docs/PRODUCTION_GOOGLE_SETUP.md) for owner setup,
consent/scope requirements and the remaining release gates. A successful signed-out
installer startup test does not validate a live Google grant, real sync, or CAD
compatibility. The separate identity-only development launcher is retained for
tests; it is not the production authentication architecture.

## Apple Silicon macOS application

The Mac packaging pipeline builds the same maintained application as a native
arm64 `CacheCAD.app` ZIP. CI extracts that distribution and requires both offscreen
and native Cocoa startup, rendered-window and clean-exit evidence before uploading
it. Session storage uses the current user's macOS Keychain with no plaintext
fallback. Profiles live under `~/Library/Application Support/CacheCADReconstructed/v1`.
See [Mac build and tryout instructions](packaging/macos/README.md).

The tryout is ad-hoc signed, without Developer ID or notarization; macOS may block
its first launch. Mac updates are manual app replacement after quitting. Live
Google authorization, CAD transfers and SolidWorks compatibility are separate,
unverified release gates. CI results, rather than this source description, establish
whether a particular artifact passed native startup.

## Linux x86_64 application

The Linux pipeline packages the actual maintained app as an extractable tarball.
Its test harness installs the archive into a separate directory and checks the
frozen executable with Qt's X11 backend on a disposable Xvfb display, alongside
offscreen, interaction, repeated-launch and synthetic crash-report checks.
See [Linux build and tryout instructions](packaging/linux/README.md) for system
libraries, installation, tested host and artifact-specific GLIBC requirements.

This Linux tryout supports signed-out interface testing. Protected credential
storage is not implemented on Linux, so Google sign-in and Drive sync fail closed
with an explicit error; no plaintext credential fallback is provided. Updates
are manual directory replacement after quitting. Native X11 tests on a virtual
display do not establish compatibility with every desktop, Wayland compositor,
GPU, live account or CAD project.

## Application updates

A real, manual **Check → Download → Verify → Install and close** flow is now
implemented with TUF metadata, Windows publisher verification and coordinated
installer handoff. Default update trust remains unconfigured; no production keys,
signing service, hosting or release publication has been set up. The helper does
not automatically relaunch the app or provide rollback. See [update setup, current
behavior and release gates](docs/APPLICATION_UPDATES.md). Local fixture tests and
the new Windows upgrade harness do not certify a signed end-to-end release.

## Maintenance checks

Use Python 3.12 and the hash-locked Linux tools:

```sh
python -m pip install --require-hashes -r requirements/linux-build.lock
python tooling/check.py --require-recovery-manifest --require-maintained-manifest --ruff
```

This runs source-only compiled-fingerprint checks, comparator regression tests,
synthetic prototype tests, extraction-tool tests and lint. It never imports recovered application modules. Selected inspected function bodies
are executed only by isolated mocked regression tests, without top-level
initialization, real credentials, network services or user project files. Reports and source hashes are bound to
the reviewed recovery manifest; reducing the verified module set fails the gate.

For independent original-byte revalidation, privately extract the pinned release
as documented in [recovery tooling](tools/recovery/), then run
`recovered/compare_bytecode.py` against its PYZ. Do not publish the installer,
extracted binaries, original bytecode, disassemblies or credential-bearing data.
The fingerprint gate trusts the reviewed manifest; changing both source and its
expected fingerprint is not independent evidence.

The earlier offline demo is available with:

```sh
PYTHONPATH=src python -m cachecad --demo --json
```

It uses disposable fixtures and fake services. Do not use live projects or
credentials for development tests. The actual application installer is a separate
artifact from this earlier demo. Windows CI has verified building, installing,
starting the installed application with the native Windows Qt backend, and
uninstalling the offline development build. Production artwork, signing,
SolidWorks validation and live Google Drive integration remain open.

See [development instructions](docs/DEVELOPMENT.md) and
[the recovery baseline notes](recovered/README.md).
