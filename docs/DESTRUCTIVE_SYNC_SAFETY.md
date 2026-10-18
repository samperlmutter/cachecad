# Destructive sync consent and observed-version checks

The original upload/download selection remains the workflow. Yellow mismatch rows
start unchecked. A selected revision, mismatch overwrite, or local/remote deletion
now requires an explicit warning listing the exact selected paths and directions:
replace REMOTE with LOCAL, replace LOCAL with REMOTE, DELETE LOCAL, or DELETE REMOTE.
Cancel is the default; Escape and closing the warning do not authorize a transfer.
New-file-only selections do not receive an overwrite warning.

Consent is limited to the selected action metadata, project, successful scan and
observed file versions. A changed project, account/client, scan, or selection during
the warning invalidates it. Local versions use fresh content hashes and stable file
identity checks; remote versions originate from the complete scan. Before destructive
writes, the executor revalidates relevant source and destination observations.
Revision uploads stage the approved local bytes rather than streaming a mutable CAD
file directly. Downloads verify their staged bytes and recheck the destination before
replacement. A stale or unavailable observation fails the affected action and asks
for a new scan and confirmation. Already completed actions are not rolled back.

## Limits

Fresh snapshot hashing currently runs in the existing serialized GUI-thread analysis
path. Large CAD projects can make the interface temporarily unresponsive while these
bytes are read; cancellation is processed afterward. This is a known latency limit.

These checks detect deterministic changes before the final validation; they are not
an atomic transaction or a race-free compare-and-swap protocol. Google Drive v3's
`files.update` reference does not expose an expected `File.version` precondition in
the client used here. Another client can change a remote item after the final read
and before the write. Local replacement/trashing likewise has a remaining
check/write interval. Keep separate backups and avoid concurrent editing/syncing.

Neither Drive revision history nor the operating system Trash is a guaranteed backup.
The app does not promise recovery of overwritten or deleted data. Multiple-file CAD
assemblies are still transferred independently, without an assembly-wide commit.
No SolidWorks mate/reference repair or live-account transfer validation is claimed.

## Provider references

- https://developers.google.com/workspace/drive/api/reference/rest/v3/files/update
- https://developers.google.com/workspace/drive/api/reference/rest/v3/files
- https://developers.google.com/workspace/drive/api/guides/manage-uploads
- https://developers.google.com/workspace/drive/api/guides/manage-revisions

Review the maintained patch manifest and regression tests for the exact changed
functions and tested paths. The recovered baseline remains unchanged.
