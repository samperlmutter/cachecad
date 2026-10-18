# Refresh, threading and sign-in hardening audit

Audit date: 2026-10-18. This is an offline code/test audit of the recovered
CacheCAD 1.2.0 beta baseline and the maintained derivative. No live account,
OAuth grant, Drive transfer, or user CAD file was used. The user's installed
version and crash logs were unavailable. **These findings do not establish the
cause of a crash in the user's installation.**

## What the refresh button actually calls

In the bytecode-verified baseline, `gui/button_bar.py` connects the refresh button
to `signal_manager.refresh_clicked`; `AppController.set_main_window` connects that
signal to `AppController.on_refresh_clicked`. That original handler is `pass`.
It does not check for remote changes in this recovered version. Project selection
and post-sync completion do start an analysis. A different user version or a
reference to a different control therefore cannot be equated with this handler.

The maintained handler starts `_begin_sync_analysis`, which clears the previous
plan and invokes generation-bound `SyncAnalyzer.start_sync_analysis`. It refuses
refresh while a scan/transfer is active. Cancel/retry and superseded results are
covered by the existing analyzer lifecycle regressions and real-Qt smoke tests.

## Reproduced hazards and corrections

- Original Drive scans with the project root already in cached folders raise
  `UnboundLocalError`. The existing maintained implementation initializes/deduplicates
  starting roots. `test_recovered_scan.py` reproduces the baseline exception and
  checks its correction.
- Concurrent original scans use the same `DriveClient.service` and underlying
  HTTP transport. A two-thread/barrier test reproduces overlapping use of that
  instance. Google explicitly states that httplib2 transports are not thread-safe.
  The maintained derivative now constructs an independent transport for each scan,
  metadata lookup, folder creation, upload, download and trash operation. Each
  operation closes its service in `finally`; cleanup errors preserve the original
  outcome. Tests prove isolation/cleanup, **not a native crash in the original app**.
- Service construction uses bundled static discovery, a ten-second socket timeout
  and zero discovery retries. The Google authorization adapter may refresh and
  replay once after HTTP 401. The timeout bounds individual socket operations;
  it is not a total scan deadline and does not make DNS cancellable.
- Original `delete_file` could repeat a trash mutation after a failed readback.
  Maintained code performs one mutation and one readback. An uncertain outcome
  is reported as failure with a refresh instruction; it is never automatically
  reissued by that method. A failed confirmation does not mean the first mutation
  failed to commit.
- Maintained Google refresh previously processed `invalid_grant` before checking
  HTTP status. Synthetic 429/500/502/503 responses with that field erased the
  remembered login. This was reproduced before the fix. Only HTTP 400 plus
  `invalid_grant` now invalidates the grant; transient statuses preserve it and
  return a retryable network error. Existing validation, persistence-before-use,
  cooldown and revoked-grant tests still apply.

## Sign-in expiry: code defects versus provider policy

Original `AuthManager.refresh_token` deletes its token file on every SDK
`RefreshError`; it does not distinguish a temporary provider failure from a
revoked grant. Its project-selection fallback also calls `refresh_token()`
without the required `creds` argument, which can raise `TypeError` when credential
checks fail. Its startup refresh runs a nested Qt event loop. The maintained
AuthManager instead restores protected credentials off the UI thread and leaves
ordinary access-token refresh to the locked `ManagedCredentials` API boundary.

Google access-token expiry is normal and should be handled without asking the
user to sign in again while the refresh grant remains valid. Google documents
refresh-grant invalidation for revocation, six months of non-use, token-count
limits, time-based consent and some administrative restrictions. An external app
in OAuth **Testing** status that requests Drive access receives a seven-day
refresh grant. These are possible causes, not evidence about this application's
actual Cloud project or the user's account. There is no demonstrated universal
"every couple of months" expiry mechanism in this code. An invalid grant requires
a fresh sign-in; application code cannot override Google's grant policy.

## Coverage and remaining limits

`tests/test_remote_refresh_audit.py` adds synthetic concurrent transport, cleanup,
service-construction, one-shot-trash and refresh-status regressions. It includes
real pinned Google SDK construction with network tripwires. Existing real-Qt
integration suites cover startup, auth cancellation/retry, stale completion,
scan failure/retry and closing with pending work. The parent task records final
aggregate test results after all parallel changes are reconciled.

At this audit checkpoint, the focused audit plus actual Qt application/auth and
offline scan integration suites passed **20 tests with no skips** using the
pinned Linux application runtime. This includes real GUI startup, account
cancellation/retry, scan cancellation/failure/retry/stale-result rejection and
closing with a pending scan. The source-only audit also passes its applicable
tests; its two Google SDK tests intentionally skip without runtime dependencies.

The scan/hash implementation uses QRunnable/QThreadPool, not an owned QThread
per scan; no "QThread destroyed while running" failure was established. Pending
hash work is retained until completion, and cancelled generations ignore stale
results. Cancelling a generation does not interrupt an in-flight socket/file read.
Constructor nested-event-loop reentrancy is guarded by the controller's
post-construction credential/shutdown checks. Large comparisons still execute on
the UI thread. Native Windows behavior, real Google failures and CAD workflows
remain separate validation gates.

## Primary references

- [Google API Python client thread safety](https://googleapis.github.io/google-api-python-client/docs/thread_safety.html)
- [Google OAuth refresh-token expiration](https://developers.google.com/identity/protocols/oauth2/#expiration)
