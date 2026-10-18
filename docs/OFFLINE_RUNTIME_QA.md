# Offline actual-application acceptance checklist

This is an independent review of the maintained/recovered source graph for the
next **Linux, Python 3.12, real PySide6, offscreen** milestone. A checklist item is
not evidence that it passed. Record executed checks separately. The recovery
baseline, vendor executable, Windows binaries, real accounts and real projects
are outside this runtime test. The disabled-auth development wrapper is an
intentional maintained behavior change, not a faithful recreation of login.

## Static import review

Before any application import, review the exact assembled source being tested.
Do not load a module merely to discover its imports or resource constants.

| Source | Import/startup effect | Required offline treatment |
| --- | --- | --- |
| `utils.paths` | Creates `app_data` and `logs` beneath the current directory | Enter a new disposable profile before import; never use the repository or a real project as working directory |
| `utils.app_settings` | Constructs a settings object; `load()` reads/writes settings and emits first-install/usage prompts | Seed complete offline settings in the disposable profile, with usage collection and tray behavior disabled |
| `core.service_credential_manager` | Constructs a singleton and immediately creates/parses a credential file | Exclude from the offline import graph, including imports through bug reporting and user validation |
| `utils.oauth_credential_import` | Constructors write redacted payloads; OAuth constructor invokes Windows `ctypes.windll` | Exclude entirely; redaction does not make constructor execution harmless |
| `utils.cloud_logging` | Constructs a singleton and may initialize Google logging | Exclude, including the import reached by `core.sync_executor` |
| `core.auth_manager` | Constructor locates tokens; later checks read tokens or refresh credentials | Do not import or instantiate for disabled-auth startup |
| `core.drive_client` | Constructor starts service-building work and a nested event loop | Use a separately identified in-memory adapter; never instantiate the real client |
| `core.github_updater` | Singleton constructor is inert, later settings/update actions perform HTTP/downloads | Exclude from offline import graph or use an explicitly inert offline adapter |
| `core.single_instance` | Constructor connects/listens with native Qt local sockets | Bypass for isolated offline preview; Python socket patches do not intercept these calls |
| `core.thread_manager` | Creates two real Qt thread pools | Inspect before execution; retain them for actual queued/threaded scan tests and wait for zero active workers at shutdown |
| `core.signal_manager` | Constructs a real QObject singleton and connects a debug slot | Accept reviewed Qt object initialization, with explicit exception capture during smoke tests |
| `core.project_manager` | Constructor reads/creates `projects.csv` and emits a signal | Permit only within a disposable profile with fixture projects |
| `gui.main_window` | Constructor shows a native system tray icon | Suppress in offscreen development mode; retaining a hidden inert object is acceptable |
| `gui.reorder_project_list_dialog` | Imports a sibling as top-level `project_list_widget` | Correct via a declared maintained import change; do not add the entire GUI directory to the search path |

Also review top-level class bodies, decorators, default arguments and assignment
calls, not only expression statements. A dynamic import trace supplements this
review; it does not replace it.

## Launcher and boundary requirements

1. Require explicit offline configuration before application imports. Fail
   closed when it is absent, repeated inconsistently, or initialized after app
   state already exists. An inherited environment variable alone must not
   silently enable a different authentication path.
2. Use a new disposable root. Set current directory, HOME, XDG config/data/cache,
   Qt runtime directory, temporary directory and bytecode-write policy before
   importing application code. Seed only synthetic settings and project data.
3. Refuse pre-existing credentials and real project input. Resolve paths and
   reject symlink/path traversal outside the profile before file operations.
   Do not allow the settings directory picker to redirect future scans elsewhere.
4. Install network/process denial before application imports. Test DNS,
   connection/listen operations, subprocess, shell and browser launch denial.
   Record whether denial is Python-level or OS-level. Python patches/audit hooks
   alone are **not a security sandbox for native Qt or arbitrary native code**.
5. Exclude live service modules structurally, rather than populating `sys.modules`
   with classes that pretend to be the recovered live services. Test the actual
   loaded module identities and source paths. The Qt and app widgets themselves
   must be real, not test doubles.
6. Native outbound routes must also be inert: `QDesktopServices.openUrl`, native
   local/network sockets, external links in `QTextBrowser` and `QLabel`, tray
   operations, folder openers and any native process APIs. For text browsers,
   disable both external links and automatic link navigation; disabling only
   external links can cause in-widget navigation instead.
7. Mark the visible window and reports as offline development. Sign-in, updater,
   cloud sync execution, bug submission, usage reporting and real-project
   mutation must be unavailable. Repeated activation must stay inert.
8. Handle an interrupted start and Close during a scan. Cancel current analysis,
   prevent stale callbacks from changing state, wait for both pools to drain,
   process appropriate queued completion/deletion events, hide/delete the tray,
   close the database, and only then remove the profile. A timeout is failure,
   not successful shutdown.

## Deterministic placeholder assets

Original bundled images, sponsor marks and font bytes are not needed to validate
widget startup. Locally authored geometric placeholders with an explicit
development label are acceptable. They must be documented as replacements and
must not be described as recovered assets or evidence of redistribution rights.

- Inventory literal `:/assets/...` references plus dynamically formed names in
  the tree-view icon map. Verify every intended placeholder alias exists.
- Use a deterministic generation recipe and manifest with content hashes.
  Assemble twice and compare asset and archive bytes.
- Actual `QPixmap` loading must produce non-null images with bounded dimensions;
  `QMovie` aliases need either valid fixture animation bytes or an explicitly
  documented disabled-animation fallback. A text file with a `.png`/`.gif` suffix
  is not a valid resource fixture.
- The existing `CCLogo` constructor already falls back to Arial when the bundled
  font is absent. Record that fallback rather than redistributing the vendor
  font or pretending a replacement is the original Comfortaa font.
- Render the actual window to a PNG and inspect it. Screenshots need not be
  byte-reproducible across OS font/rendering configurations; the packaged source
  and placeholder payloads should be reproducible.

## Required execution evidence

Run these only after the static graph and guards have been agreed. Keep the
ordinary compile-only recovery gate separate from the opt-in Qt runtime gate.

### Startup and UI

- Report Python patch version, OS/architecture, PySide6/Shiboken versions and Qt
  platform plugin; require `offscreen` for this milestone.
- Construct the actual maintained `AppController`, actual recovered/maintained
  `MainWindow`, all four pages, original signals and widgets. Record their module
  paths, window title, page count and successful native resource loads.
- Show, resize, render and close the actual window. Capture Qt messages, Python
  exceptions from slots and unexpected modal dialogs as failures or explicitly
  reviewed limitations, never silent pass conditions.
- Navigate Home, Settings, Project and Selection, then repeat navigation. Exercise
  Back/Cancel, repeated action clicks, disabled sign-in/update/report controls,
  and inert external-link/folder-opening routes.
- Run a fresh process twice to catch initialization and cleanup errors. A second
  instance in the same Python process is a separate optional lifecycle test
  because the original signal manager and thread pools are global singletons.

### Queued scanner behavior

- Use real `QThreadPool`, queued Qt signals, maintained `LocalClient` and
  `SyncAnalyzer`, plus an in-memory database and an explicitly fake Drive adapter.
- Cover success, deliberate local/remote failure, Cancel while workers are
  pending, immediate retry, late completion of an old generation, and Close with
  work pending. Verify failure/cancellation clears executable plans and a later
  success produces only current-generation state.
- Verify callbacks that update GUI-facing state execute in the intended Qt
  thread. Selected-function fake-signal tests do not establish this property.
- Verify both thread pools have zero active workers before destroying the
  database/profile. Report any modal-error-dialog accommodation in the test.
- Do not imply the live `DriveClient`, cloud executor or Windows integration was
  tested when fake adapters supplied those boundaries.

### Negative and provenance checks

- Assert prohibited live modules never entered `sys.modules`, no credential
  files were created/read, and no network/process attempts succeeded.
- Verify synthetic project and configuration paths remain within the disposable
  root. Place an outside sentinel and confirm it is unchanged.
- Verify no unwanted `app_data`, `logs`, `__pycache__` or settings appeared in the
  checkout or user's home; confirm the temporary root is removed on success and
  retained/reported safely on a diagnostic failure if needed.
- Re-run original recovery fingerprints, precise maintained AST scope checks,
  secret hygiene and existing regression suites after final edits. New removed
  import exceptions must be narrow, exact, present only on the baseline side,
  and protected against missing/duplicate/unrelated edits.
- Assemble twice from the final revision and compare hashes. Bind runtime
  evidence to the tested source/assembly hash. Do not call an earlier tested
  archive evidence for later untested edits.

Passing this milestone establishes bounded offline source startup and reviewed
fixture behavior on the recorded Linux environment. It does not establish a
production release, an installer, asset redistribution rights, live account
safety, Windows behavior, SolidWorks integration or lossless cloud sync.
