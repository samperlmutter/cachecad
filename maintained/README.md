# Maintained patches against the recovered original

These three modules are copies of the bytecode-verified recovered source, with
four explicitly tracked function bodies changed. The original `recovered/`
baseline remains immutable and compiler-fingerprint checked. This is separate
from the earlier `src/cachecad/` experimental replacement.

## Reproductions and bounded corrections

- Drive remote listing: real recovered scan callbacks/batch errors can publish a
  partial snapshot. Tests connect that snapshot to the real recovered analyzer
  and reproduce a DELETE_LOCAL decision for an unchanged previously synced file.
  The maintained method refuses to publish incomplete scans, including reported
  incomplete searches, failed pages, missing callbacks and batch exceptions.
- Executor upload: the original None result from an unsuccessful upload causes
  unpacking to fail; its runnable catches the exception without a terminal event.
  The maintained upload method reports a failure when preparation/upload/unpacking
  raises. Successful upload and checksum-mismatch paths remain covered.
- Executor cloud delete: the original ignores a False result, logs a completed
  sync action and emits success. The maintained method requires a truthy deletion
  result before recording success.
- Updater: a checksum mismatch calls `.delete()` on a string, raising AttributeError
  and retaining the rejected download. The maintained method unlinks the path and
  returns False. Successful checksum verification is preserved.

Tests execute inspected, unchanged AST function bodies from the baseline and
maintained sources with explicitly injected fake services/signals and disposable
filesystem fixtures. They do not import application modules, initialize Qt or
credentials, execute the original executable, or access live Google services.
The harness has import/file/network/process tripwires; it is not a security
sandbox for hostile code. These tests establish the selected method behaviors,
not whole-app runtime parity, thread scheduling, or end-to-end safe sync.

`patch-manifest.json` binds each changed source to its baseline hash and names
the only allowed changed function bodies. `tooling/maintained.py` rejects changes
to imports, signatures, class structure or unrelated functions. The source-only
CI gate runs those checks and the regression tests.

## Still open

- Failed remote scans stop planning but the original UI error/retry lifecycle is
  unfinished. Existing stale plans and concurrent scans need generation-aware
  orchestration; this patch does not prove full end-to-end deletion safety.
- Upload logging exceptions and other executor early-return/error paths still
  need centralized terminal accounting. Exactly-once claims apply only to the
  tested selected cases, not every action or Qt delivery.
- Cached-folder inputs containing the root ID retain an unrelated original
  unbound-variable bug. A separate reviewed change is required.
- Updater cleanup I/O failures can still raise. Version naming, authenticated
  update trust and network timeout/retry behavior are not changed here.
- Overwrite races, transactional replacement, immutable upload staging and
  SolidWorks assembly-generation consistency are not yet integrated.

No installable app is produced by this patch layer. Resource/dependency assembly,
GUI runtime, Windows and SolidWorks validation remain separate work. The existing
wheel still contains only the earlier offline replacement prototype; the source
archive contains this maintained layer and the recovered baseline for review.
