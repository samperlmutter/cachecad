# Apple Silicon macOS package

This is the actual reconstructed and maintained application, assembled from the
same reviewed production source as Windows. The build emits one arm64
`CacheCAD.app` in `CacheCAD-macOS-arm64.zip`. No DMG installer, signing account,
notarization service, new update platform, or Gatekeeper/trust change is added.

## Build and test

Use native Apple Silicon macOS with CPython **3.12.10**. The official
[actions/python-versions manifest](https://raw.githubusercontent.com/actions/python-versions/main/versions-manifest.json)
was checked on 2026-10-19: 3.12.10 is its latest available Darwin arm64 3.12 build;
3.12.14 is unavailable for this target. The existing source-structure fingerprint
gate remains mandatory. This interpreter pin is for an isolated tryout, not a
claim of current production security support.

From the repository root, in a clean arm64 virtual environment:

```sh
python -m pip --isolated --disable-pip-version-check --no-input install \
  --index-url https://pypi.org/simple --only-binary=:all: --require-hashes \
  -r requirements/runtime-macos.lock -r requirements/macos-build.lock
python -m pip check
python packaging/macos/build.py --output dist/macos
python packaging/macos/test_app.py \
  --archive dist/macos/CacheCAD-macOS-arm64.zip \
  --report-dir dist/macos/test-results
```

The output directory must not already exist. Never use `--no-deps`, alternative
indexes, `--trusted-host`, an x86 interpreter, or Rosetta for this build. Dependency
installation must occur natively; a Linux cross-target dry run is not execution.

The spec uses PyInstaller's explicit `target_arch='arm64'`, `BUNDLE`, and default
ad-hoc signing. No private signing identity or entitlement is supplied. It keeps a
console-capable bootloader so subprocess tests can capture startup diagnostics;
`LSBackgroundOnly=False` explicitly overrides the bootloader's default so Finder
opens a regular foreground `.app`. The build validates Info.plist, compares
bundled source hashes with the reviewed assembly, verifies the signature, audits
every shipped native binary, and creates the ZIP using Apple's `ditto` to preserve
framework links and executable modes. Source is loaded by the existing verified
importer; frozen first-party modules and development backends are forbidden.

The native test extracts the ZIP into a new temporary directory, re-verifies the
signature/native payload, then directly runs the packaged executable with both
`offscreen` and `cocoa` Qt backends. Each run has a fresh disposable profile/home,
an absent OAuth configuration, application socket/browser/process tripwires, and
an OS-level network-denial sandbox. It must render the real signed-out window,
load the real SDK and modern `send2trash.mac.modern` backend, report no credentials
or external attempts, terminate all reported workers, and exit zero. Bundle
bytes/modes/links must remain unchanged. These checks do not authenticate, access
Drive, trash real files, or claim a complete sync/lifecycle/upgrade test on macOS.

`build-provenance.json` records build input hashes, native headers and archive
identity. `test-results/macos-runtime.json` separately records actual extracted-app
test outcomes. The build-only record explicitly leaves runtime validation pending.
A passing hosted macOS job is still not a test on an individual user's M1/M2 Mac,
or evidence that Gatekeeper will accept an internet-downloaded ad-hoc build.

## Dependency and native audit

The locks were resolved from official PyPI with uv 0.12.23, CPython 3.12 and
`aarch64-apple-darwin`, wheel-only, no repository resolver overrides, and upload
cutoff `2026-10-19T00:00:00Z`. Existing shared versions are retained. Runtime and
build tools are separate locks. Every chosen artifact was downloaded and checked
against the matching official PyPI filename, SHA-256, size, metadata name/version,
Python requirement and compatibility tags. Yanked artifacts were rejected.

- Runtime: 33 distributions, 161,906,672 wheel bytes
- Build tools: 11 distributions, 13,136,866 wheel bytes
- Complete metadata dependency closure was checked using Darwin/arm64/CPython
  3.12.10 environment markers and requested transitive extras
- Both locks replayed together in a hash-required, wheel-only, offline pip
  cross-target installation dry run

`dependency-artifacts.json` contains the official metadata URLs, wheel URLs,
hashes, sizes and declared dependencies. `dependency-native-audit.json` records
static Mach-O load-command inspection of the locked wheels. Wheel-supplied dSYM
debug directories are excluded from that dependency audit and are not bundled.
The final bundle is audited independently, with no debug-symbol exclusion.

The advertised **macOS 12.0 minimum** is enforced per arm64 native slice, not
inferred solely from wheel filenames or overwritten with a deployment variable.
Every native file shipped in the final app must be arm64-only and have a valid
`LC_BUILD_VERSION` or `LC_VERSION_MIN_MACOSX` no newer than that floor. The main
executable is also checked using Apple's `lipo`. A dependency requiring a newer
macOS version makes the build fail rather than silently mislabel compatibility.

`PySide6-Essentials==6.9.2` supplies the real PySide6 package and required Qt
Core/Gui/Widgets/Network modules. Unused Qt Addons and retired vendor Google Cloud
Logging/grpc dependencies are omitted. Modern Trash support adds only
`pyobjc-core` and `pyobjc-framework-Cocoa`, not the full PyObjC framework suite.
Real Google OAuth/Drive dependencies and existing shared TUF/crypto imports are
retained; no macOS installer/update helper or feed is introduced. Metadata and
third-party notices are retained for every runtime distribution.

Static dependency verification does not establish native runtime success,
security auditing, binary reproducibility or license approval. PyInstaller is
[not a cross-compiler](https://pyinstaller.org/en/stable/index.html); native CI
must establish freezing and execution. Upstream documents the
[arm64 targeting and ad-hoc signing behavior](https://pyinstaller.org/en/stable/feature-notes.html#macos-multi-arch-support).

## Distribution limits

The app is ad-hoc signed, not Developer ID signed or notarized. macOS may refuse
to open a downloaded copy. No recipe strips quarantine, disables Gatekeeper, or
changes signing/trust settings. A distributor must separately arrange and
explicitly authorize Developer ID signing/notarization for frictionless public
distribution. Manual replacement of the quit app is the supported update method
for this tryout. The user's profile and macOS Keychain session are separate from
the app bundle. Google OAuth still requires an owner-supplied Desktop client;
CI uses no real account and proves no live authorization.
