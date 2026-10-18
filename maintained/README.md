# Maintained patches against the recovered original

The `recovered/` baseline remains immutable and compiler-fingerprint checked.
These maintained copies change the actual recovered application functions;
`patch-manifest.json` identifies every edited body, signature, added method,
signal and import. New helpers have separate provenance. The earlier
`src/cachecad/` experimental replacement remains a separate package.

## Reproduced defects and corrections

- Partial Drive listings previously produced DELETE_LOCAL plans for unchanged
  files missing from failed scan pages. Incomplete searches, callback/batch/page
  failures and missing responses now fail closed rather than publish partial data.
- Upload error results previously lost terminal accounting after unpacking None.
  Preparation/transfer/unpacking errors now emit failure in the tested paths.
- Cloud deletion previously ignored False results and recorded success. Failed
  deletion now records failure without a successful sync-log entry.
- Updater checksum rejection previously called `.delete()` on a string. It now
  removes the rejected download with Path.unlink and returns False.

## Generation-bound scan lifecycle

Local and Drive scanner adapters accept explicit scan IDs and success/error
callbacks. Tagged scans do not emit untagged legacy result/error signals. Local
hash workers use explicit direct connections with locked per-invocation state,
reject incomplete traversal/hash results,
and do not mutate a shared cache for obsolete generations. Cached Drive roots no
longer trigger the original unbound-variable failure.

The actual analyzer waits for both complete results and rejects stale/settled
scan outcomes. Its queued result slot serializes database/comparison work before
publishing a plan. This prevents the prior background comparison/shared-state
race, but large scans may now block the UI during comparison; Qt scheduling and
responsiveness still require runtime validation.

The controller clears old plans on start/failure/cancellation, restores controls,
shows a visible scan error, and permits Refresh to retry while preserving read-only
permissions. Old analyzer instances/generations cannot replace a new plan. Tests
exercise actual functions with fake signals and shuffled outcomes; they do not
prove real Qt signal delivery or thread lifecycle behavior.

## OneDrive advisory in the actual UI

Initial working-directory selection, settings changes, project selection and
Refresh call the same advisory. It checks three OneDrive environment roots and
folder-name hints, shows a warning with No selected by default, and leaves state
unchanged if declined. Acceptance is remembered only for the same path and root
configuration in the current process. Detection is lexical: it does not resolve
junctions, verify active OneDrive sync, or certify an unrecognized path as safe.

## Bounded new-file transfer protection

New uploads stage a private copy, checking selected hash/size/time and source
identity before sending that fixed snapshot. New downloads verify bytes against
the selected checksum/size, then use same-filesystem atomic no-clobber publication.
Unsupported publication fails instead of falling back to an overwriting rename.
A concurrent same-name Drive create can still produce duplicates.
Drive transfer methods explicitly close their owned upload/download streams on
all terminal paths, without masking a committed outcome if close itself fails.
Cleanup/logging failures after a commit still require outcome reconciliation.

Normal revision synchronization remains available with its original race limits.
Revision actions lack verified destination snapshots and provider conditional
commit; this milestone does not claim race-safe revision replacement. Local
parent-directory races, filesystem behavior, and live Drive semantics need further
validation. SolidWorks assembly-set consistency is not established.

## Evidence and pipeline

Tests execute inspected AST function bodies from baseline/maintained files using
injected fake services/signals and disposable filesystem fixtures. Application
imports, original executable execution and live Google services are excluded.
The harness has import/file/network/process tripwires; it is not a hostile-code
security sandbox. Exactly-once assertions apply to tested outcomes, not every
executor action or Qt delivery.

`tooling/maintained.py` checks source hashes and explicit AST exceptions, rejecting
unreviewed changes. `tooling/assemble_app.py` produces the actual application source
tree from baseline plus this layer, with deterministic ZIP and origin hashes.
The assembled source has an explicitly offline, scanner-only development launcher
using real Qt, generated neutral resources and a separate runtime lock. It is not
an installable production app; Windows and SolidWorks validation remain separate. The existing wheel still
contains only the earlier offline replacement package.

## Remaining defects and limits

- Other executor early returns, logging failures and deletion errors need a broader
  terminal-accounting design.
- Updater cleanup I/O errors can propagate; timeouts/retries and update trust are
  unchanged.
- Full snapshot/conditional-commit revision protection remains open.
- Qt lifecycle, performance, filesystem platform behavior and GUI smoke tests are
  deferred; mocked tests are not whole-app runtime equivalence.
