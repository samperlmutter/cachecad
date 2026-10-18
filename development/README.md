# Offline development launch of the recovered application

This launches the actual assembled CacheCAD `AppController`, `MainWindow`, local
scanner, SQLite manager and analyzer using real PySide6/Qt. It uses a disposable
profile, disables live authentication/services and transfers, and replaces only
remote I/O with an explicitly synthetic adapter in smoke scenarios.

## Reproduce

Use Linux x86_64 and Python 3.12.14. First run the normal maintenance gates and
assemble a new output directory, then install the separate runtime lock into a
fresh environment:

```sh
python -m venv /path/to/runtime
/path/to/runtime/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements/runtime-linux.lock
python tooling/assemble_app.py /path/to/new-app-source
/path/to/runtime/bin/python /path/to/new-app-source/development/launch.py --app-source /path/to/new-app-source --smoke
/path/to/runtime/bin/python /path/to/new-app-source/development/launch.py --app-source /path/to/new-app-source --smoke --scenario lifecycle
/path/to/runtime/bin/python /path/to/new-app-source/development/launch.py --app-source /path/to/new-app-source --smoke --scenario close-pending
```

The assembled `development/` directory includes its runtime lock, launcher and
resource generator. Without `--smoke`, the launcher opens the signed-out offline
UI on the available display. It accepts no real project, credential or account
configuration. All development data is disposable and removed on normal exit.
A worker-shutdown timeout deliberately exits without deleting its profile.

## What is exercised

- Actual Qt offscreen startup, window rendering, placeholder icons/animation,
  real queued scan results and deterministic synthetic `part.txt` upload planning.
- A cancelled generation, a failed generation with a real error dialog closed by
  a test timer, then a successful retry with obsolete callbacks rejected. Only
  the successful generation writes the comparison database.
- Window closure with a scan pending, cancellation, pool shutdown and profile cleanup.
- No live-service/credential modules loaded, credential files created, external
  operations attempted, unhandled Qt slot exceptions or remaining worker threads.

These are Linux fixture observations, not Windows/SolidWorks validation or
production sync certification. Transfer execution is intentionally disabled in
this development mode; ordinary maintained revision behavior is unchanged.

## Audited isolation boundary

The launcher applies Python network/DNS/process/write tripwires, confines settings
and application paths to a new temporary profile, suppresses native tray display,
disables native external links/folder browsing and blocks Qt process/browser
entry points used by the reviewed source. The offline configuration must be
explicitly activated before application imports and cannot be enabled by an
environment variable.

This is an audited development harness, **not an OS sandbox for hostile Python or
native code**. Python hooks cannot universally intercept arbitrary native I/O.
Do not run unreviewed code, plug-ins, real credentials or user projects through it.
The teardown removes the disposable profile only after both application pools
finish; a timeout preserves it instead of racing active workers.

## Placeholder resources

`build_resources.py` authors a neutral checkerboard PNG and a one-frame GIF from
scratch, then uses official pinned Qt 6.9.2 `pyside6-rcc` to map the resource aliases
used by the GUI. Fixed source timestamps make the generated `resources_rc.py`
byte-reproducible. No vendor image, sponsor artwork, animation or font bytes are
included. The original font-loading code uses its existing system-font fallback.
The development window title makes the offline mode explicit.

The original recovered entrypoint and production branding remain separate.
This is a runnable development source workflow, not a rebuilt Windows installer.
