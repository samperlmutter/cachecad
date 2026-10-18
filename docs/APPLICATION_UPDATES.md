# Application updates: implemented flow and owner setup

Status: 2026-10-18. The maintained application now contains a real TUF update
client, Qt consent dialog, Windows publisher verifier and staged installer helper.
The shipped `application/update-policy.json` remains `{"schema":1,"configured":false}`.
No production update root, publisher certificate pins, signing keys, hosting or
release publication has been configured. Opening Settings → Check for updates
therefore shows owner setup is required; it does not contact a server or claim
that the installed release is current.

This is implementation and local test evidence, not a signed end-to-end release
certification. The previous [design and acceptance gates](APPLICATION_UPDATE_PLAN.md)
remain relevant; its original inventory is explicitly historical.

## What the user can do

With an approved configuration packaged into the application:

1. **Check for updates:** explicit and manual. The new client authenticates TUF
   metadata, applies product/channel/platform/profile constraints and refuses
   rollback or conflicting release identities. The dialog shows the installed
   and offered versions, build, channel, size and release-note reference.
2. **Download update:** a separate choice. A worker reauthenticates the offer,
   downloads with bounded transport/size checks, verifies TUF length/SHA-256 and
   promotes complete bytes into the profile's update cache. Downloaded bytes do
   not authorize execution.
3. **Verify Windows publisher:** `WinVerifyTrust` and the locally approved
   certificate policy must pass. The file and its directory chain are held with
   restrictive sharing. The dialog distinguishes content verification from
   publisher verification; Install remains disabled until both pass.
4. **Install and close:** a separate explicit click. Busy transfers, scans or
   account/background work defer installation. The controller invalidates the
   old sync plan and prevents new activity during handoff. A staged, verified
   helper independently refreshes the offer and verifies the installer.
5. **Orderly handoff:** helper READY only requests clean application shutdown.
   COMMIT is sent only after the launcher confirms clean shutdown, including
   workers, databases and the diagnostics helper. The installer still waits for
   the identified old process to exit and observes the shared install/run gates.
6. **Finish:** the installer is interactive, per-user and does not force-close
   applications or reboot. Reopen CacheCAD normally afterward. The helper records
   `installation_finished`, `installation_cancelled`, `installation_failed` or
   `installation_deferred` in `app_data/update-result.json` when it can safely do
   so. An installer exit code is not proof of a healthy first application launch.

Cancel is the default dialog action. Cancel, Escape, Later and window close
invalidate pending results; a retry cannot reuse a cancelled worker or transferred
installer handle. A handoff failure requires a fresh check and verification.
Cancellation does not pretend that a native Windows trust call or other worker has
already stopped. Downloads do not resume a previous sync, authorize Google access,
modify Drive, or touch CAD project content.

## Owner configuration and release prerequisites

Do not enable updates by editing an installed policy or copying test fixtures into
production. Configuration is a reviewed build input, covered by the assembled
source manifest. End users do not supply feed URLs or trust keys.

Before the first distributable update, the owner must approve the distribution
location, publisher identity, release authority, key custody/rotation and support
policy. Keep the source repository private. No such approval or service setup is
implied by the implementation.

Package these public verification inputs through the reviewed assembly manifest:

- `application/update-policy.json`: an enabled schema-1 policy with exactly the
  fields listed below
- `application/update-root.json`: the owner's reviewed initial TUF root metadata,
  containing public verification material only

| Enabled policy field | Required meaning |
| --- | --- |
| `schema`, `configured` | `1` and `true` only after the owner setup is complete |
| `metadata_base_url` | Approved HTTPS TUF metadata base URL, ending in `/` |
| `target_base_url` | Approved HTTPS installer base URL, ending in `/` |
| `channel` | Explicit release channel; no implicit stable/beta switching |
| `allowed_redirect_origins` | Explicit HTTPS origin allowlist; empty when none are needed |
| `publisher_certificate_sha256` | Lowercase SHA-256 fingerprints of approved DER signing certificates; never publisher-name substrings |
| `max_installer_bytes` | Positive download limit, at most 1 GiB |

The parser rejects unknown/duplicate fields, unsafe local paths and noncanonical
URLs. Credentials, arbitrary command arguments and trust overrides are not policy
inputs. An invalid enabled policy fails closed rather than using a fallback feed.
See [`load_update_config`](../maintained/core/application_updates.py) for the exact
contract and bounds.

The logical TUF target is `channels/<channel>/<architecture>/installer.exe`.
Its authenticated length and SHA-256 cover the final signed installer bytes.
Its `custom.cachecad` object binds schema 1, product ID, channel, semantic version,
monotonic build, architecture, Windows build range, minimum updater version,
profile read/write schema ranges, immutable installer URL and release-note URL.
A version/build cannot identify different bytes. Publication must advance signed
metadata only after the exact installer is approved and available.

The current build pipeline intentionally produces **unsigned test artifacts**.
A production release still needs a reviewed signing pipeline: sign/timestamp the
helper before binding its final digest into the app; sign the app and installer/
uninstaller with the approved publisher; then verify and test the exact final
bytes. Post-build helper changes invalidate its embedded identity. Never replace
production trust checks with test-only signing fixtures or bypass flags.

Use `version.json` as the maintained product's version/build source. Assembly and
Windows packaging generate matching app, diagnostics and PE/installer identity.
The historical recovered vendor version and the separate experimental scaffold
wheel are not this product's release inputs.

## Data, recovery and distribution limits

- The per-user install identity and profile generation remain stable. Settings,
  the project index, encrypted Google session, custom profile paths, project-local
  recovery records and CAD files must be preserved. No schema migration is added.
- In-place installer replacement is not a transactional or power-loss-safe update.
  There is no automatic rollback, payload activation journal, healthy-first-launch
  gate or automatic relaunch. Older releases are refused; same-version repair
  requires the exact registered payload identity.
- Held-file verification reduces substitution races. It is not an atomic
  handle-to-execution primitive or protection against arbitrary malware already
  running as the same Windows user.
- Native trust/revocation calls and OS name resolution are not instantaneously
  cancellable. Failure to settle safely defers installation rather than forcing
  worker, CAD or application termination for an update.
- Private GitHub CI artifacts expire after 30 days and require repository access.
  They are tester downloads, not durable public distribution. No source repository
  credentials may be shipped to users. A separately approved distribution service
  and retained signed releases are still required.

## Verification and remaining release gates

Local tests cover real TUF metadata processing with isolated fixtures, transport
and compatibility failures, Qt lifecycle/cancellation, Windows-verifier boundaries,
and controller/helper shutdown authorization. UI tests use actual offscreen Qt
and synthetic services; they do not exercise live signing or accounts.

`packaging/windows/test_upgrade.py` and the Windows workflow add native N→N+1,
exact-payload repair, refused downgrade and exclusion tests with disposable
profiles, synthetic CAD bytes and real same-user DPAPI. The older CI installer is
a synthetic prior-version build of the current source. This checks upgrade
mechanics; it is not evidence for every historical installation. The harness runs
unsigned installer fixtures explicitly and does not relax production update trust.

For any release, inspect the CI result for that exact commit and final artifact.
Added tests and previous build results are not a pass for unrun Windows tests.
Still required before enabling distribution: final signed app/helper/installer
end-to-end tests, approved-root and publisher rotation/recovery drills, native
trust/revocation and interruption scenarios, and a reviewed distribution process.
Live Google authorization and real CAD/project validation remain separate release
readiness gates.
