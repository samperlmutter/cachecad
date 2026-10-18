# Transfer recovery in the maintained application

This change applies to the actual assembled application. The recovered baseline
remains unchanged. Network services in regression tests are deterministic fixtures;
no live account, CAD file, or remote write was used to validate these changes.

## What happens during a transfer

Before a destination mutation, a per-operation record is written beneath the
project's `.cachecad/recovery/` directory. Approved local/remote versions and the
operation target are retained. Revision downloads preserve independent copies of
the previous local bytes and verified incoming bytes before removing the original.
Publication never overwrites an unexpected newly appearing destination.

The report distinguishes **not committed**, **unknown**, **committed**, and
**committed with incomplete recording**. A received successful mutation is not
turned into a safe-to-retry failure by a later CSV, progress notification, or
cleanup error. Unknown responses are not automatically retried. A new executor
checks existing pending records before repeating that target mutation.

Every selected action has an identified result, including open-file skips and
scheduling failures. Duplicate/late terminal notifications cannot finish a batch
early. Cancellation does not erase a mutation that already happened or make an
unknown remote outcome definite. Folder creation has its own recorded outcome.

The results window appears after a batch; the existing Options button reopens the
last session report. Its project ID and local root are captured from that batch,
not from the currently selected project. Sign-out clears the session report.
Warnings and local recovery locations appear as plain text. Durable records
remain on disk after the application closes.

## When a result needs review

Keep the entire reported recovery directory. Do not delete or edit its JSON or
byte copies, and do not blindly repeat a remote write. Inspect the current local
and remote versions before deciding what to restore. There is currently no
automatic reconciliation, replay, or recovery-record deletion. A matching remote
name or checksum alone does not establish that an unknown write was yours.

Completed operations also retain recovery material. This can consume substantial
disk space for CAD projects. Automatic retention limits and a reviewed recovery UI
are future work; do not mistake a successful transfer for automatic backup cleanup.
A failed journal preparation prevents the mutation, including disk-space errors.

The legacy CSV is still used for comparison history, but it is now validated as a
whole before planning. Torn/malformed rows, unsafe aliases and invalid identities
stop analysis instead of implying a deletion. A different remote file ID and a
previous deletion record cannot establish a transfer baseline. A damaged history
file must be preserved and reviewed; re-scanning does not recreate deletion intent.

## Boundaries

- The local and remote check/write race remains; Google Drive expected-version
  atomic mutation protection is not established here.
- Windows directory fsync is unavailable through the current Python implementation.
  File flushes and atomic record replacement are tested, but arbitrary power-loss
  durability is not guaranteed.
- POSIX mode bits and read-only material are accidental-modification safeguards.
  They do not establish restrictive Windows DACLs or protect against a hostile
  same-user process changing directories concurrently.
- The tests exercise actual maintained functions, real temporary files, injected
  failures, process termination/restart, and real Qt queued outcomes. Live-provider
  interruption and real CAD/SolidWorks consistency remain unverified.
- No multi-file assembly transaction or automatic destructive retry is introduced.

See [the audit and test matrix](TRANSFER_RECOVERY_PLAN.md) for baseline evidence.
