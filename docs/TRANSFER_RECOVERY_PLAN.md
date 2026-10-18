# Transfer interruption and recovery: evidence and patch contract

## Scope and evidence baseline

The first fault-injection pass ran against maintained application source at
`c6c78a8` on 2026-10-18. It added 28 offline tests in
`tests/test_transfer_recovery.py`. All 28 passed, including tests that deliberately
**asserted unsafe baseline behavior**. A green characterization run is not a
claim that interrupted transfers are safe. Tests named `test_unsafe_*` identify
remaining characterized defects; tests named `test_safe_*` assert an enforced
boundary. Convert unsafe assertions as their corresponding fixes land.

The harness compiles the unchanged maintained executor/provider/analyzer method
bodies. Revision tests use the actual immutable-consent, observed-version, and
staging helpers through the existing disposable consent fixture. The test
provider models only its external responses and a tiny committed-object list.
No replacement synchronization implementation is tested in place of production.

No live account, OAuth flow, CAD file, GUI initialization, or network access is
used. Local files are disposable. The fake Recycle Bin is a same-fixture rename,
so the tests can assert that the old bytes remain recoverable. It does not test
real Windows Recycle Bin behavior. Injected exceptions and synchronous signal
adapters do not establish process-kill, power-loss, filesystem, or Qt scheduling
durability.

Run the focused suite with:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. \
  python -m unittest discover -s tests -p test_transfer_recovery.py -v
```

## Integration results after the first recovery patch

The same suite now contains **35 passing tests** against the integrated working
tree. Thirty assert safe behavior; four intentionally characterize the raw
provider's remaining ambiguous tuple/`None` contract; one reproduces the original
provider's progress-callback defect against recovered source. The earlier baseline
findings below are historical evidence, not a description of every current
executor path.

Verified in the updated actual-method tests:

- ENOSPC, EXDEV, EACCES, and EINTR at revision publication leave the active
  destination absent, but preserve independent verified old/incoming recovery
  copies and an `unknown` outcome. The old simulated Recycle Bin entry remains.
  No automatic restoration or overwrite is claimed.
- A competing destination survives publication conflict, with both recovery
  copies retained.
- A confirmed upload/download followed by sync-log failure remains
  `committed_recording_failed`; staging cleanup failure preserves `committed`
  with a warning. Neither is downgraded to an ordinary transfer failure.
- The actual provider's lost create acknowledgment becomes `unknown` in the
  executor. A fresh executor using the same project refuses a second mutation
  because the durable pending record requires review. This is conservative
  replay blocking, not automatic remote reconciliation or resumable transfer.
- Torn or invalid CSV baselines are rejected wholesale. A valid row for a
  different remote ID does not imply cloud deletion; matching valid commit
  history retains normal inference.
- Initial, folder, and mid-batch scheduling errors settle affected actions;
  duplicate identified results and untagged UI notifications cannot finish the
  batch early; cancellation does not relabel a result recorded beforehand.
- Provider upload progress-callback/status errors no longer erase an acknowledged
  response or abort the intermediate chunk loop. No additional mutation request
  or chunk retry is introduced by this handling.

The four raw-provider characterizations deliberately invoke `DriveClient`
directly, outside the executor journal. They show why bypassing that boundary or
adding unconditional provider retries would reintroduce duplicate creation or
ambiguous results. The integrated executor test separately establishes that
the same lost acknowledgment cannot be replayed through the guarded path.

The existing 66 executor/consent/new-file regression tests also pass with real
journal helpers bound into their fixtures. Earlier observed-version guards were
retained, and publication failures are injected specifically at the project
destination, after durable intent creation.

At the 21:52 UTC integration run, the combined executor, consent, overwrite,
independent lifecycle review, transfer recovery, provider cleanup, journal, and
history suites ran **203 tests: 202 passed, one real-Qt test was skipped** in the
stdlib build environment. Real Qt requires the separate locked runtime. The companion
journal suite includes two real child-process exits at representative boundaries:
after removal of a disposable destination and after publication. Fresh journal
readers retain the evidence and block replay in both cases. These are bounded
process-exit tests, not power-loss or Windows Recycle Bin validation.

## Baseline findings, ranked by consequence

### P0: revision publication has a recoverability gap

In `SyncExecutor._download(revision=True)`, `send2trash(local_path)` precedes
`os.link(staged, local_path)`. The verified download lives inside a
`TemporaryDirectory`. Injecting ENOSPC, EXDEV, EACCES, or EINTR at `os.link`
reproduces all of the following:

- The approved previous file has already moved to the simulated Recycle Bin.
- The ordinary project destination is absent.
- Context cleanup deletes the verified replacement and its staging directory.
- The executor emits generic failure and writes no successful baseline.

This is loss of the active project destination and the downloaded replacement,
not evidence that the old file was permanently erased. A competing writer that
appears after Trash is correctly preserved by no-clobber publication, but the
verified replacement is still discarded.

Evidence: `TransferPublicationRecoveryTests` tests for revision link failure and
destination race. A checksum/truncation failure or cancellation before Trash
already preserves the old destination; keep those controls intact.

### P0: missing remote acknowledgment is not proof of non-commit

`DriveClient.upload_file` and `create_folder` return `None` for both a request
that failed before commit and a server commit whose response was lost. The
tests deliberately commit in the provider double and then raise a timeout.
A second explicit invocation creates a second new object/folder or another
revision. The application performs no automatic retry in these tests: the
duplicate is caused by replaying an indistinguishable failure.

The current create body has name/parent/revision-retention fields, with no
operation identifier used for reconciliation. Name equality alone cannot prove
that an existing remote object belongs to this operation.

Evidence: `RemoteMutationOutcomeTests` lost-response, pre/post-commit comparison,
and explicit-retry tests. These establish an ambiguity in application behavior,
not a claim about undocumented Google server retry semantics.

### P0: a torn baseline can authorize deletion candidates

`SyncAnalyzer.read_sync_log` accepts CSV rows without validating the complete
schema or row. `compute_sync_actions` treats matching path and MD5 as sufficient
baseline evidence. A torn row containing only the first four fields under the
normal header, or a file containing only `path,md5`, can produce `DELETE_LOCAL`
or `DELETE_CLOUD` candidates. A later torn row overwrites a prior valid entry.
Invalid operation names and a wrong recorded Drive ID are also accepted for
deletion inference.

These tests assert **candidate inference**, not unauthorized deletion execution.
The separate explicit-consent/version boundary still applies. Presenting an
incorrect destructive candidate is nevertheless unsafe recovery behavior.

Evidence: `SyncBaselineRecoveryTests` torn-row, missing-schema, invalid-action,
and overwritten-baseline tests. Missing-log behavior already produces
non-destructive new-transfer candidates.

### P1: a confirmed mutation is downgraded by later bookkeeping failure

After successful download publication, sync-log append or staging-directory
cleanup failure produces `action_failed`, despite the destination containing
the correct verified bytes. New and revision uploads likewise report generic
failure after the provider returned the expected ID/checksum if staging cleanup
or log append fails. These outcomes must not invite an unqualified mutation
retry.

The provider already preserves successful upload results across its own stream
and transport cleanup failures; tests retain this positive control. However,
a progress-signal exception after the upload response is received can still
hide that acknowledged result behind `None`.

Evidence: `TransferPublicationRecoveryTests` postcommit log/cleanup cases;
`RemoteMutationOutcomeTests` cleanup controls and progress-callback case.

### P1: batches lack action-identity settlement

- Failure to schedule the initial worker leaves `_started=True` and the batch
  unfinished; a second start does not retry it.
- Failure to schedule a later action leaves the unscheduled remainder pending.
- Failure to schedule folder creation leaves the entire batch pending.
- Two completion emissions for one action can finish a two-action batch before
  the second action runs.
- A failure delivered after the global cancellation flag changes is classified
  as cancellation without carrying its original action identity/cause.
- An unrecognized action kind can execute no branch and emit no outcome.

Evidence: `TransferBatchSettlementTests`. Queued uploads cancelled before they
run already perform no provider call and settle once when all callbacks arrive.
The tests do not reproduce a real Qt race; they deterministically demonstrate
the accounting assumptions that such races would exercise.

## Recovery acceptance contract and remaining work

The initial patch implements durable material retention, explicit executor
outcomes, conservative pending-operation replay blocking, strict history
validation, and identified batch settlement. The contract below remains the
review checklist, including work not yet established: user-guided reconciliation
and restore, automatic provider reconciliation, and comprehensive target-platform
durability validation. A read-only results/recovery report has been added; it is
not an automatic repair mechanism.

### 1. Preserve verified recovery material before destructive local commit

Before Trash, create a durable, operation-owned recovery record and preserve
the verified replacement outside an auto-deleting context. Record project/root
identity, approved action identity, previous local identity, remote
ID/version/size/checksum, recovery-file location, and phase. Use restrictive
permissions and reject symlinked or out-of-root journal/recovery paths. Never
store credentials or resumable-upload tokens in ordinary diagnostics.

Suggested phases are intent, replacement verified, old destination moved,
replacement published, baseline recorded, and cleanup pending/complete. Persist
the necessary intent **before** each irreversible boundary, and record the
observed outcome afterward. A phase is evidence, not permission to overwrite:
recovery must compare current filesystem identity and checksum before changing
anything. If a different file appeared, preserve both it and the recovery copy
and request an explicit decision.

If journal creation or required flush fails, block before Trash. If publication
fails afterward, preserve the old recoverable copy and verified replacement,
expose a recovery-required outcome, and return a stable recovery reference.
Do not claim automatic rollback succeeded without checking it. Never substitute
an overwriting rename/replace for the existing no-clobber publication guard.

Acceptance: every injected error between verification, Trash, link, recording,
and cleanup leaves enough verified material and metadata for a later process
to inspect/recover. A fresh reader must reconstruct state without process
memory. Exact power-loss guarantees must be limited to tested filesystem and
platform flush semantics.

### 2. Separate operation outcome from recording and cleanup outcome

Use structured, action-bound outcomes rather than tuple/`None` and an untagged
failure signal. Distinguish at least:

- Not started / definitely not committed
- Cancelled before dispatch
- Committed and verified
- Committed, baseline recording pending/failed
- Committed, cleanup pending/failed
- Outcome unknown after dispatch
- Conflict/stale approval
- Local recovery required after a destructive boundary

A postcommit error must retain the confirmed ID/checksum or local publication
identity. It may report a warning/recovery requirement, but must not become a
retryable "nothing happened" failure. UI progress, logging, and diagnostics
must not alter the operation's outcome. Cancellation after dispatch cannot by
itself prove that a remote mutation did not commit.

Acceptance: acknowledged mutations are never resubmitted merely to repair a
log; cleanup/log failures are visibly distinguished; full success is not shown
while required reconciliation or recovery remains unresolved.

### 3. Reconcile unknown remote outcomes before any mutation retry

Persist an operation identifier before dispatch and retain bounded details
needed to reconcile it. If a response disappears, label the result unknown.
Do not immediately reissue create, update, or folder-create. Reconcile through
supported provider metadata/queries, verifying parent, operation identity,
size/checksum and observed version as applicable. Duplicate names or matching
bytes alone are insufficient attribution. Verify provider support before
claiming idempotency or conditional-update protection.

If reconciliation is ambiguous, stop and preserve evidence for a user decision.
Any subsequent retry must use the same operation identity and enforce current
authorization/observed-version checks. Bound read-only retry/backoff separately
from mutation retry. Never silently reuse expired or stale consent.

Acceptance: a simulated successful server commit followed by response loss,
followed by restart and retry, does not create a second object or revision; an
unresolvable result remains unknown rather than success or definite failure.

### 4. Treat the baseline as validated commit evidence

Parse the entire baseline transactionally into a new structure, validate exact
schema and every required field, and publish it only when complete. Detect torn
rows, missing/extra fields, invalid action values, incompatible identities and
conflicting duplicates. A malformed tail must not replace the last valid
baseline. Avoid relying on an append-only unvalidated CSV as a durable commit
journal; use versioned atomic records or an appropriately transactional store.

Deletion inference must be disabled when baseline trust is uncertain. A clean
re-scan can establish current presence, but cannot reconstruct historical
deletion intent by itself. Preserve the suspect file for diagnosis and present
non-destructive recovery choices. A repair operation should not manufacture a
successful baseline for an unconfirmed or unknown mutation.

Acceptance: every malformed baseline fixture produces no destructive candidate;
interrupted writes and replayed records are detected; a complete validated record
remains usable; the existing explicit destructive-consent boundary stays intact.

### 5. Settle each batch action exactly once by stable identity

Create a bounded ledger before scheduling, keyed by execution and action ID.
Wrap scheduling itself and every action body. Scheduling failure must settle
the affected/unscheduled actions without resubmitting accepted tasks. Unknown
action types must become explicit failures. Terminal messages must carry their
operation identity and actual outcome. Ignore duplicate/late messages without
incrementing counts or affecting another run.

Finish the batch when all distinct accepted actions have terminal outcomes,
including cancelled, failed, unknown, and recovery-required outcomes. A settled
batch is not necessarily a successful batch. Cancellation must stop undispatched
mutations while preserving the result of already committed work.

Acceptance: initial/folder/mid-batch scheduling errors cannot strand a batch;
duplicate/out-of-order/late completions cannot falsely finish it; a cancelled
batch can contain correctly reported committed and unknown operations.

## Validation still required beyond these tests

After implementation, convert each fixed unsafe characterization into a named
safety regression. Run the full offline gate and existing consent, transfer,
provider-lifecycle, manifest, and packaging tests without weakening guards to
make fixtures pass. Keep fault injection at actual maintained-method boundaries.

Then test process termination/restart at every durable phase with a disposable
project; Windows file sharing/read-only cleanup; actual Recycle Bin recovery;
disk-full conditions during journal writes and publication; unsupported link
filesystems; and real Qt cancellation/queued-event delivery. Live-provider
interruption tests require separately authorized disposable remote resources.
Passing offline doubles alone cannot establish those platform/service claims.
