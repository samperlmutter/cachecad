# New-file transfer protections and remaining revision protocol gaps

Reviewed 2026-10-18. This milestone changes the real recovered executor's
`_upload` and `_download` bodies in `maintained/core/sync_executor.py`.
The bytecode-matched `recovered/` baseline remains untouched.

## Implemented and tested

- New uploads require the selected action's size, modification time, and MD5.
  The actual source must be a regular file with a usable file identity. Its
  identity, size, and nanosecond timestamps are checked before opening, after
  reading, and against the pathname after copying. The copied stream and the
  completed staged file are both hashed against the selected action.
- Uploads use a private, read-only staged copy with the original filename.
  Later edits to the working file do not change the bytes being uploaded.
  Temporary copies are cleaned up when the transfer returns or raises. The
  maintained Drive adapter explicitly closes its owned media/file streams in
  finally blocks; close failures are logged without replacing the transfer result.
- New downloads use a private directory on the destination filesystem. The
  returned file must belong to that directory, be regular, remain unchanged
  during verification, and match the selected MD5 and size. Cancellation is
  rechecked before publication.
- Publication uses `os.link` to create an absent destination. An existing file,
  including one that appears after preflight, is not overwritten. If hard links
  are unsupported, the action fails; there is no rename/replace fallback.
- Tests execute selected method ASTs with fake services/signals and disposable
  files. A per-function test import shim permits only hashlib, stat, and tempfile;
  the shared harness and interpreter importer remain unchanged.
- Baseline-positive fixtures reproduce the original missing terminal event when
  a new-download destination already exists, and the original POSIX overwrite
  when the destination appears during download. Maintained tests cover stale
  metadata, same-size/mtime content changes, mutation/replacement during staging,
  staged-file corruption, post-staging local edits, cancellation, unsupported
  hard links, checksum failures, cleanup, and revision upload API compatibility.

## Important bounds

Existing upload/download revision behavior is preserved, including its remaining
check-then-act races. This milestone does not make revision replacement safe.
It does not add cloud compare-and-swap, a whole-assembly snapshot, scan-generation
isolation, crash recovery, or end-to-end GUI/Windows/SolidWorks validation.

A private read-only copy reduces accidental mutation; it is not tamper-proof
against the same user or a privileged process. Path containment checks are not
a sandbox: replacement of parent directories with links/reparse points between
resolution and filesystem operations remains outside this guarantee. A successful
no-clobber publication can still be edited/deleted by other processes afterward.

MD5 is used for compatibility with the original action and Drive checksums, not
as a security/authenticity primitive. The source metadata does not include the
original filesystem identity, so substitution with identical bytes and timestamps
before staging is not distinguishable. New Drive creates do not overwrite an
existing object, but racing creates can produce duplicate names. A transfer or
publication may have committed even if later cleanup/logging fails; such failures
need durable outcome reconciliation rather than automatic retry.

## What the next revision-protection design needs

1. Carry immutable source and destination snapshots from the completed analysis
   through selection to execution. The original SyncAction drops target version
   information. Include scan generation, local identity/timestamps/size/digest,
   remote file ID/version/head revision/digest, and explicit unknown states.
2. Revalidate both ends immediately before any destructive action. Revalidation
   detects stale plans but does not close the gap before commit.
3. Establish a documented conditional cloud commit. Drive exposes an output-only
   increasing `version` and a binary-file `headRevisionId`, useful for snapshots.
   Neither is itself a writable expected-version condition.
   [Drive File reference](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)
4. The reviewed v3 `files.update` reference and resumable-upload guide do not
   establish `If-Match`/expected-version semantics across session initiation,
   chunk upload, final commit, and retries. This is an unresolved contract, not
   proof that no such capability exists. Do not invent CAS from a final GET or
   from a conditional header accepted by an HTTP client. Require authoritative
   confirmation and a separately authorized disposable-account concurrency test
   before relying on such a protocol.
   [files.update](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/update)
   [Resumable uploads](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
5. For local replacement, validate a Windows-specific locking/backup/commit and
   recovery protocol. CreateFile sharing flags can reject conflicting writers
   and renamers while a handle remains open. Closing that handle before replacing
   the pathname reopens the race; holding it may block the replacement itself.
   [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
6. ReplaceFileW provides backup-assisted replacement, but its path-based API does
   not accept an expected file identity/hash. It has documented partial-failure
   states and requires the files to share a volume. It is not by itself a local
   compare-and-swap protocol. Test open CAD handles, reparse points, concurrent
   saves, crashes, backup retention, and outcome recovery on the target platform.
   [ReplaceFileW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew)

No live account, original executable, or application module was used for this
research or these tests. Revision protection remains a separate reviewed change.
