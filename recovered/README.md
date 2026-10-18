# Original source recovery baseline

Recovered sources preserve the original logic, including known defects.
They are separate from the replacement prototype under `src/cachecad/`.
No recovered application module has been imported or executed during this work.

Consult `recovery-manifest.json` for the current exact module/object counts and
all 49 original first-party PYZ module statuses. `entrypoint/` separately tracks
the original CArchive main script, outside that 49-module inventory. The source
layout here is for inspection and maintenance; runtime packaging is unfinished.

`compare_bytecode.py` verifies the original archive SHA256, reads its code objects
without execution, compiles a candidate source, and recursively compares bytecode,
exact constants/types, names, locals/closures, argument counts, flags, stack sizes
and exception tables. Only filenames, line numbers and line tables are ignored.
Comments, whitespace and the author's exact original source cannot be recovered.

`check_reports.py` can run without original bytes. It compiles all exact modules
listed in the reviewed manifest and checks original structural fingerprints,
source/report hashes and object counts. Missing, deleted, downgraded or extra
reports fail. This trusts the reviewed manifest; it is not an independent
original-byte comparison unless the pinned original is supplied separately.

Example private original-byte check:

    python recovered/compare_bytecode.py --pyz /path/to/PYZ.pyz core.sync_analyzer core.sync_executor core.drive_client core.local_client

The credential-import module is intentionally sanitized and non-matching. Never
restore embedded credentials from the original binary or use its service accounts.
Runtime testing requires a separately authorized, isolated credential setup.

Next stages: recover/replace required Qt resources, establish runtime dependency
locks, exercise isolated original interfaces without cloud services, then apply
reviewed safety patches to this unchanged baseline with focused regressions.
Windows and SolidWorks testing remain deferred. No runtime parity or working-app
claim follows from static compiler matching alone.
