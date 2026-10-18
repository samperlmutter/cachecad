# From recovered source to a runnable CacheCAD build

## Current evidence and boundary

The deterministic `tooling/assemble_app.py` output is an application **source
archive**. It is not a runnable installer or a validated desktop application.
The recovered entrypoint is `recovered/entrypoint/main.py`; it adds `src` to
`sys.path` and imports `resources_rc`, PySide6, and the recovered controller/UI.
The earlier maintenance-tools wheel is not the original application runtime.

This assessment used static archive metadata, Python code-object constants and
source inspection. No vendor executable, application module, credential loader,
GUI, or network account was run. Existing selected-function regression tests do
not establish whole-application behavior.

## Dependency evidence

The original CArchive identifies Python **3.12** and `python312.dll`. Its exact
Python patch version and the complete dependency lock are not yet established.

Bundled `.dist-info` metadata establishes:

| Distribution | Version |
|---|---|
| google-api-python-client | 2.179.0 |
| google-api-core | 2.25.1 |
| opentelemetry-api | 1.36.0 |
| importlib_metadata | 8.7.0 |

Static version constants in these PYZ modules additionally establish:

| Module | Version constant |
|---|---|
| PySide6 | 6.9.2 |
| shiboken6 | 6.9.2 |
| requests.__version__ | 2.32.5 |
| google.auth.version | 2.40.3 |
| httplib2 | 0.22.0 |
| google.cloud.logging_v2.gapic_version | 3.12.1 |
| google.protobuf | 6.32.0 |
| oauthlib | 3.3.1 |
| urllib3._version | 2.5.0 |
| charset_normalizer.version | 3.4.3 |

These are observed versions, not a newly resolved or security-reviewed lock.
Further required imports include `google_auth_oauthlib`, `send2trash`, and
`tzlocal`; their exact distribution versions are still unverified. Audit all
transitive/native requirements before generating a hash-locked environment.
Source: `../prototype/dependency-inventory.json`, `../pyz-inventory.txt`,
`../pyinstaller-inventory.json`, and the pinned PYZ listed below.

The extracted application contains Windows `.pyd`/DLL extensions, including
`VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`, Python extensions and Qt libraries.
Those Windows binaries are not Linux runtime dependencies and should not be
copied into a Linux build. A future Windows build requires separately pinned,
trusted Windows wheels/toolchain and platform validation.

## Resources are recoverable, but not yet packaged

`resources_rc` exists in the pinned PYZ. It is a generated Qt resource module
with only the QtCore import, three resource byte constants, resource
register/unregister functions, and a top-level registration call. The two
functions use Qt resource format version `3`.

| Constant | Bytes | SHA-256 |
|---|---:|---|
| qt_resource_data | 239740 | 77fd96db7bb176058bcf71489b55788eb943747c71923bba65ac6de10b12f7b2 |
| qt_resource_name | 684 | d89aa879a0a887fb022c43d09079fd751e9ae843a9d32733f69a3eb12b1df179 |
| qt_resource_struct | 594 | ba09d00d313543e7558aeb62d21e89cec6452f36f81f15d9aa22fde66e21d91a |

The statically decoded name table contains the `assets` prefix and 25 named
resources, including `Comfortaa-Bold.ttf`, `cachecad.ico`, PNG icons/logos, and
`loading.gif` / `loading2.gif`. It includes sponsor and application branding.

Two possible preparation paths:

1. After deciding asset redistribution rights, reconstruct generated
   `resources_rc.py` from the three constants and the two inspected function
   bodies. Compile-only compare against its original code object, add its own
   provenance/fingerprint and explicit assembly manifest entry. Do not execute
   the original module to extract resources.
2. Statically parse the resource tree into bounded, traversal-safe asset files
   plus a `.qrc` manifest, inspect asset types/licenses, and regenerate with a
   separately pinned official `pyside6-rcc`. This is more maintainable but its
   generated module need not be bytecode-identical; verify resource names and
   content hashes instead.

Neither path has been performed by this assessment. Do not label the present
source archive runnable merely because those constants are available.

## Remaining runtime blockers

- `utils.oauth_credential_import` is deliberately sanitized and non-equivalent;
  its placeholders cannot authenticate. It still contains Windows-specific
  `ctypes.windll` calls. Never restore embedded vendor secrets. A maintained,
  explicit user-supplied credential configuration or disabled-auth development
  mode requires separate design and authorization before account access.
- Import-time side effects must be audited before a whole-module smoke test.
  Credential creation, telemetry, updater/network setup, application folders,
  and singleton/thread initialization cannot be treated as harmless imports.
- `gui.reorder_project_list_dialog` contains a top-level
  `from project_list_widget import ProjectListWidget` rather than the packaged
  `gui.project_list_widget` import. The assembled layout only puts `src` on the
  search path; resolve and test this packaging mismatch explicitly.
- Platform assumptions (Windows paths, hidden-file APIs, shell/folder opening,
  tray behavior and native Qt plugins) require a maintained compatibility
  decision. Linux static/source validation does not validate Windows behavior.
- The maintained scan lifecycle serializes DB comparison and CSV generation in
  the analyzer's queued result slot. This removes concurrent shared comparison
  state but can block the GUI on a large project; measure and redesign with
  isolated per-generation comparison state if needed.

## Ordered next build gates

1. Preserve the recovered baseline and validated maintained overlays; rerun
   source fingerprints, changed-function scope checks, secret hygiene and
   selected-function regressions. Assemble twice and compare artifact hashes.
2. Complete the runtime dependency inventory; resolve official registry wheels
   for a chosen Python 3.12 runtime and target OS; pin every transitive version
   and file hash. Keep build/runtime locks separate from the maintenance-tool
   environment. Installing or executing these is a distinct subsequent step.
3. Prepare resources by one reviewed path above and add their explicit source
   and payload provenance to deterministic assembly.
4. Patch packaging/import blockers and separate startup from credentials,
   telemetry and live services. Test actual source startup with reviewed mocks
   and a disposable profile. Do not use real user projects or credentials.
5. Add a runnable source/venv artifact first. Validate offscreen Qt loading,
   assets, startup/shutdown, queued scanner callbacks, cancellation, failure and
   retry with in-memory services and disposable fixtures. Record the platform.
6. Only then define and pin a freezer specification and installer recipe.
   Build on the actual target platform with official dependencies, record
   licenses/hashes, and verify installation/uninstallation in an isolated VM.
   Windows and SolidWorks integration remain deferred as requested.
7. Require separate approval and explicit scope before live Drive/account or
   real-project validation. Source/fixture success alone is not production
   readiness or a guarantee of lossless sync.

Pinned evidence archive SHA-256:
`5482479f417d650d4a3be34def973534b172902baceeed48f9bc877ebb883ea2`
(`../extracted/pyinstaller/PYZ.pyz`).
