# Local crash diagnostics

The production application writes small, structured error reports to the
`diagnostics` folder inside its isolated application profile:

- Windows default: `%LOCALAPPDATA%\CacheCADReconstructed\v1\diagnostics`
- Linux default: `$XDG_DATA_HOME/cachecad-reconstructed/v1/diagnostics`, or
  `~/.local/share/cachecad-reconstructed/v1/diagnostics` when unset
- A custom `--profile-dir` uses `<profile-dir>/diagnostics`.

On a subsequent normal launch, a nonblocking notice identifies this folder if a
previous report exists. Reports remain local. There is **no automatic upload,
email, telemetry endpoint, or vendor bug-report integration**. The user can
inspect a report and choose whether to share it. Deleting these reports is safe;
it does not delete projects, settings, or the encrypted account session.

## Report contents and privacy

Each `crash-<random-id>.json` includes:

- Schema, application version, SHA-256 of the assembled-source manifest, UTC time
- OS family, Python version, process bitness
- Failure category and, for Python exceptions, an allowlisted built-in exception
  type or the generic `ApplicationError` classification
- At most 64 traceback frames: reviewed application source basenames, reviewed
  function names, and line numbers. Unrecognized sources/functions are replaced
  with `<external>` / `<redacted>`.
- At most 16 fixed lifecycle breadcrumbs, such as `window_shown` and
  `shutdown_started`

The reporter deliberately does **not** format exception messages, chained
exception messages, object representations, locals, source lines, provider
responses, URLs/query strings, access/refresh tokens, account identities, project
paths, CAD filenames, or file contents. Thread names/identifiers are omitted.
Only names derived from the reviewed application source catalog are retained.
The original application's raw loggers remain disabled; its raw stdout/stderr
suppression and OAuth-safe handling remain in place.

Finalized reports are atomically replaced and fsynced. Retention keeps the newest
8 reports, each limited to 32 KiB, or at most 256 KiB of finalized report content.
Interrupted private temporary reports older than one hour are reclaimed on the
next activation/write, after a nonblocking file-lock check; active writers and
recent temporary files are left alone. Writes use owner-only file modes where
the OS supports them; Windows uses the
user profile's inherited access controls. Linked diagnostic directories are
rejected. Reporting/storage failures are best-effort and must not turn an
otherwise normal application operation into a crash.

## Coverage and native capture

The verified bootstrap installs `sys.excepthook` and `threading.excepthook`
before loading Qt, account configuration, or application modules. The launcher
also records exceptions caught by its final safe startup boundary and explicit
worker shutdown timeouts. Qt callback errors continue through the actual
`sys.excepthook`; the launcher no longer replaces that hook with a silent list.

When supported, CPython `faulthandler` writes fatal-error stack output to an
anonymous pipe, **never directly to a raw file**. One small, local, same-launcher
helper process parses that pipe and persists only the safe fields above. It
verifies the source snapshot, validates a marked profile and its currently held
process lock, and signals readiness before native capture is enabled. It does
not load Qt, OAuth sessions, Drive, or project data. Source launches run this
helper with isolated Python and without site initialization. Frozen launches
invoke the same installed executable in its private helper mode.

The helper drains the pipe after a fatal parent exit and then exits itself.
Normal shutdown closes the pipe and reaps the helper. Startup/shutdown cleanup
uses bounded waits, termination, and a final kill/reap attempt for a stuck helper.
The startup check reports this explicitly in its `diagnostics` result object:
`enabled`, `local_only`, `native_capture`, and `helper_processes`. This fixed local
helper starts before the startup check's external-operation tripwire; that
tripwire still forbids later arbitrary process/network/browser activity.

Capture is not a guarantee: OS termination without a catchable fatal signal,
power loss, disk failure/fullness, extreme interpreter corruption, unavailable
native support, or failure before source verification may prevent a report.
Diagnostics do not create memory dumps or control separately configured OS
crash-dump services. A sanitized report may not contain enough information to
reproduce an issue; missing details are not grounds for enabling raw credential-
bearing logs.

## Disposable verification

Never inject faults into a real user's profile. All fault commands require all
three independent gates:

1. `CACHECAD_ENABLE_DIAGNOSTIC_TESTS=1` in the test process environment
2. `--startup-check`
3. An explicit `--profile-dir` that passes the new/empty disposable-profile check

Use a different new directory for every invocation:

```text
CacheCAD.exe --startup-check --profile-dir <new-disposable-profile> --diagnostic-test main
CacheCAD.exe --startup-check --profile-dir <new-disposable-profile> --diagnostic-test thread
CacheCAD.exe --startup-check --profile-dir <new-disposable-profile> --diagnostic-test qt
CacheCAD.exe --startup-check --profile-dir <new-disposable-profile> --diagnostic-test native
```

`main`, `thread`, and `qt` exit with code 1 and generate `startup-failure`,
`unhandled-thread`, and `qt-callback` records respectively. `qt` uses a real Qt
`QTimer` callback. `native` calls `abort` only after native capture is confirmed
ready; its platform-dependent nonzero exit should produce `native-fatal`.
The native test process suppresses Windows error-reporting dialogs and disables
POSIX core files. These test-only settings do not affect normal application
launches. Allow up to 10 seconds for the native helper's post-exit atomic report
write before declaring the installed-executable test failed.

Portable regression tests cover genuine subprocess unhandled main/thread
exceptions and a disposable POSIX abort, privacy filtering, retention, filesystem
failure, hook restoration, helper cleanup, required source verification and
fault-mode gates. Set `CACHECAD_RUNTIME_PYTHON` to the reviewed runtime to also
exercise the actual assembled launcher, actual Qt callback delivery, native
helper round-trip and clean signed-out startup. Target Windows installed-EXE
fault tests are a separate packaging gate; portable tests do not prove that the
frozen Windows payload works.
