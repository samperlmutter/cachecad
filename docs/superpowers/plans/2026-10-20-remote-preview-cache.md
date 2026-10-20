# Remote preview cache implementation plan

**Goal:** Preview Drive-only files from a hidden project cache; reuse verified cached content for a normal download and purge expired files daily.

**Design:** Keep cache files in `.permafrost/preview-cache`, which existing scans and watchers ignore. Cache identity includes path, Drive ID, version, hash, and size. Preview fetching verifies metadata before and after download and verifies content before publishing. Normal downloads take ownership of a matching cached file and use the existing journal, consent, backup, and no-clobber commit. Cleanup removes regular cache files older than 24 hours on startup and daily while running, using the operation gate to avoid active previews and syncs.

**Constraints:** Preserve local previews and real project files. Enforce the 512 MiB preview cap before network download. Reject stale/corrupt copies and symlinked cache paths. Do not sync cache files or change baselines for previews. Do not add dependencies or store credentials in cache metadata.

## Task 1: Cache storage and verified fetch

- [x] Add Rust tests for verified preview download, repeat preview reuse, stale version/hash/cancel rejection, ignored cache files, purge scope and symlink handling.
- [x] Run the tests against missing/empty behavior and confirm failure.
- [x] Implement `preview_cache::{fetch, take, purge}` and shared internal directory validation.
- [x] Run the cache tests and core suite.

## Task 2: Reuse cache for normal downloads

- [x] Test that a normal download uses matching cached content without a second media request, removes it from cache, preserves consent and rejects changed destinations/versions.
- [x] Make download transfers take matching cached files before falling back to network downloads.
- [x] Run regression tests and core suite.

## Task 3: Native preview and daily purge

- [x] Make Drive-only preview read the scanned remote version through the cache, preserving local-first behavior and clear errors.
- [x] Start a purge on app startup and every 24 hours, serialized with active operations.
- [x] Verify native tests, frontend build/tests and focused browser regressions; request independent review and address material findings.

## Review focus

Cancellation or corruption must never publish a usable cache entry. A new Drive version must not reuse old bytes. A cache hit must not bypass consent, destination checks or recovery journaling. Purge must never delete project files/backups or follow symlinks. Local previews must remain usable without Google sign-in.
