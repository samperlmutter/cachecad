# Application updates: implementation status and design rationale

Updated 2026-10-18 after implementing the maintained update flow. The original
proposal inspected `c6c78a8478da234e08c54e8918d46753138b0fd0` plus then-current
working files; its inventory below is retained as historical evidence, not a
statement about today's implementation. See [current setup and operating
instructions](APPLICATION_UPDATES.md) for the implemented contract.

## Current implementation status

- Added real TUF discovery/download (`maintained/core/application_updates.py`),
  manual Qt consent (`maintained/gui/application_updates.py`), Windows held-file
  publisher verification and staged helper handoff. The retired vendor updater
  remains excluded.
- Added a generated maintained-product identity from `version.json`, coordinated
  install/run exclusion, installer downgrade/exact-payload repair checks and a
  native Windows upgrade/preservation harness wired into CI.
- The flow is **Check → Download → Verify → Install and close**. There is no
  automatic relaunch. Helper READY is not install authorization: the launcher
  sends COMMIT only after clean shutdown, and the helper waits for process exit.
- Default trust is unconfigured. No production TUF root, publisher pins, signing
  keys, hosting or publication has been set up. Unsigned CI fixtures cannot pass
  the production publisher gate.
- Local fixture/Qt tests provide implementation evidence. A signed end-to-end
  update, final-artifact Windows release validation, healthy-first-launch
  protocol and transactional rollback are not established by this work. Live
  Google authorization and CAD/project validation remain separate open gates.

Sections below retain the architecture rationale and outstanding release gates.
A requirement or proposed future behavior is not evidence that it is implemented
or has passed on a production-signed Windows artifact.

## Recommendation

Keep the source repository private. Ship owner-approved, signed **full Inno Setup installers** through a separately approved distribution channel. The explicit **Check → Download → Verify → Install and close** flow is implemented but must remain unconfigured until its distribution, signing and final-artifact validation gates pass. Keep the retired vendor updater excluded. Do not promise silent updates, automatic relaunch or automatic rollback from the current installer.

An application update replaces application files. It must preserve the user's settings, project list, project folders, and Windows-protected Google session. It must never start a sync, grant Google access, change Drive files, or force-close CAD software.

## 1. Historical pre-implementation inventory

The following table records the original proposal baseline. Its disabled update
button, missing mutex/version identity and missing upgrade tests have since been
addressed as described above; distribution/signing readiness has not.

| Historical evidence | Consequence |
| --- | --- |
| `tooling/assemble_app.py:PRODUCTION_EXCLUDED_MODULES` omits `core.github_updater`; `application/launch.py:BLOCKED_MODULES` also rejects it. `maintained/app/app_controller.py:_configure_owned_ui` disables the update button, and `on_download_app_update` reports no owner configuration. | Installed builds do not currently contact the original update service. Implement a new owner-controlled component; do not reconnect the recovered one. |
| `.github/workflows/linux-ci.yml` uploads `CacheCAD-Windows-Application-<commit>` with `retention-days: 30`, from a read-only-permission build workflow. `docs/WINDOWS_TRYOUT.md` describes private-repository downloads. | These are test artifacts, not a durable user update service. GitHub requires sign-in and repository read access to download workflow artifacts. [GitHub artifact access](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts) |
| `packaging/windows/cachecad.iss` uses AppId `{D24539B1-3941-4273-9C44-A62A05A91623}`, per-user `{localappdata}\Programs\CacheCAD Reconstructed`, and `PrivilegesRequired=lowest`. It has `CloseApplications=no`, `RestartApplications=no`, no `AppMutex`, and no signing configuration. | Preserve the identity and per-user install model for this product line. Add an install/run exclusion protocol before upgrades. The current file does not establish safe upgrade or downgrade handling. Inno uses AppId for uninstall-log continuity and uninstall registration. [Inno AppId](https://jrsoftware.org/ishelp/topic_setup_appid.htm) |
| `packaging/windows/test_installer.py` tests fresh installation, isolated startup/faults, and uninstall, not existing-user upgrades. | A passing current build is not an upgrade, migration, or rollback test. |
| `recovered/app/app_version.py` still supplies `1.2.0` to the maintained Settings page; installer, Python package, and crash diagnostics use `0.1.0`. | First fix version identity. Do not compare the original vendor's version series with this derivative's release series. |

### Recovered updater defects, not production behavior

Static evidence in `recovered/core/github_updater.py`:

- Lines 15–31: hard-coded original owner's release repository; no request timeout; release response cached indefinitely for the object's lifetime. The `owner`/`repo` download arguments are not forwarded to the lookup.
- Lines 34–39: constructs the destination from `self.tag` before fetching it. A direct first download can use an empty version suffix. The release tag is not validated as a safe filename component.
- Lines 55–65: writes directly to a final `.exe`; streaming and filesystem exceptions occur outside the request exception handler. No partial-file protocol, size bound, cancellation, or progress contract.
- Lines 46–50 and 68–73: compares a GitHub-provided SHA-256, then attempts `.delete()` on a string on mismatch. The maintained historical copy changes only that cleanup to `Path.unlink()`. SHA-256 is useful integrity evidence, but a digest obtained from the same replaceable release record is not independent authorization from this application's owner. GitHub does provide asset digests; that API feature itself is not the defect. [Release asset API](https://docs.github.com/en/rest/releases/assets)
- Lines 76–109: the unused signature helper parses English `certutil -dump` text and a subject string rather than performing a defined Authenticode trust check; it also lacks `self` or `@staticmethod`. The download path never calls it.
- `recovered/gui/pages/settings_page.py:update_latest_version` treats any non-equal value, including an older version or a failed lookup, as an update. Recovered controller handlers perform the network operation synchronously and merely open Downloads after success; they do not implement safe installation.

These findings are source inspection, not execution of the original updater.

## 2. Distribution and release ownership

**Preferred initial channel:** a distinct owner-controlled public distribution repository containing only reviewed release notes, public metadata, license notices, and signed installer assets. Keep private source and internal CI reports in the private source repository. A release on a private repository retains repository access restrictions; a private code repository does not become a public download service by adding Releases. GitHub also provides source archives for the repository holding a release, so the public distribution repository must never contain the private source. [GitHub releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)

The owner must approve public binary distribution first. Public downloads disclose the program and packaged assets; packaging is not source-code secrecy. If downloads must remain restricted, agree on a separate authenticated distribution design or keep the existing authorized tester workflow. Never ship a personal access token, shared GitHub credential, or source-repository read permission to end users. Public release assets can be fetched without API authentication. [Release asset API](https://docs.github.com/en/rest/releases/assets)

Release procedure after approval:

1. Select a reviewed commit and unique derivative version. Build with locked tools/dependencies; retain commit, inputs, and provenance privately.
2. Authenticode-sign the application/helper and installer/uninstaller with the owner's approved identity, SHA-256, and a trusted timestamp. Recompute installer digests **after signing** and run installed/upgrade tests on those exact final bytes. Inno supports signing Setup, Uninstall, and selected source files. Signing is not a guarantee that reputation warnings disappear. [Inno SignTool](https://jrsoftware.org/ishelp/topic_setup_signtool.htm), [SignedUninstaller](https://jrsoftware.org/ishelp/topic_setup_signeduninstaller.htm), [Microsoft SignTool](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool)
3. Owner reviews release contents and test evidence. Publish only a sanitized public manifest and installer, never internal logs, profiles, tokens, or private source archives.
4. Upload all assets to a draft, verify public download bytes against the final tested digest, then publish. Enable immutable releases only with owner approval; immutable assets/tags cannot be replaced, while release notes and the “latest” designation can still change. A bad release gets a new corrected version, not replaced bytes. [GitHub immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)
5. Advance the signed channel metadata last. Retain at least the current and prior approved installers under stable versioned URLs, with an owner-defined support/retention policy. Keep CI artifact expiry separate from release retention.

Initially the owner can publish manually. A later automated publisher should use a separately approved, narrowly scoped release identity behind an explicit release approval gate. Do not add publishing or signing credentials to ordinary pull-request builds.

## 3. Trust contract for the new updater

The implemented client uses the Python implementation of **The Update Framework (TUF)** rather than a custom signature/rotation protocol around an unsigned JSON file. Bundle the initial owner root trust metadata, pin and review the dependency, and use its authenticated target length/SHA-256 plus versioned, expiring metadata. TUF addresses update discovery/download trust; CacheCAD still owns installer execution and data safety. [TUF design](https://theupdateframework.io/docs/overview/), [Python implementation](https://theupdateframework.readthedocs.io/en/stable/)

The authenticated release description must bind product ID, channel, semantic version, monotonic release/build number, architecture, supported Windows range, minimum updater version, profile read/write schema range, exact immutable installer URL, byte length, SHA-256, and release-note reference. The approved channel endpoint and allowed HTTPS redirect hosts must be explicit; never accept arbitrary feed URLs, executable names/arguments, shell commands, or certificate identities from unsigned network data.

Root/release keys need approved custody, backup, rotation, recovery, and expiry-renewal responsibilities. Ship public verification material only. Test root-key rotation before first public use. Expired metadata, rollback/replay, unsupported schemas, bad system time, and unknown keys must produce a safe “cannot verify this update” result; they must not disable the installed app or permit an unverified installer. A published higher-sequence withdrawal can stop future offers, but cannot recall an installer already downloaded offline.

**Installer verification is an additional gate:** after trusted metadata and exact length/SHA-256 pass, call Windows `WinVerifyTrust` using Authenticode policy (`WINTRUST_ACTION_GENERIC_VERIFY_V2`), require the success return value, and validate the actual trusted signer against owner-approved certificate/public-key identities. Do not match a display-name substring. Require an appropriate timestamp/chain/revocation policy; if necessary trust checks cannot complete, defer installation rather than silently weakening verification. Handle publisher certificate renewal through pre-approved trust rotation. Use `signtool verify /pa /all /v` as a CI check, not as a runtime dependency or text-parsing substitute. [WinVerifyTrust](https://learn.microsoft.com/en-us/windows/win32/api/wintrust/nf-wintrust-winverifytrust), [Windows trust options](https://learn.microsoft.com/en-us/windows/win32/api/wintrust/ns-wintrust-wintrust_data), [SignTool](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool)

## 4. User flow and failure behavior

The current flow is described below; final production-signed Windows validation
remains a release gate. Stronger recovery/health behavior is explicitly future work.

1. **Check:** manual by default. Show installed/proposed versions, channel, publisher, download size, release notes, and profile compatibility and close/reopen implications. No background-check setting is implemented; any future background checks require a clear user choice. Never install silently or change stable/beta channels implicitly.
2. **Download:** only after the user's choice. Use a worker, bounded connect/read timeouts and total duration, response/redirect/size limits, no implicit retries, and a unique `.part` file in an app-owned user-local update cache outside the executable directory. Handle offline/404/rate limits/proxy failures/disk full without touching the installed version. Cancel is cooperative and removes incomplete download bytes when the worker settles. Native trust checks and OS DNS may not stop immediately. Initially restart interrupted downloads; add resumable downloads only with validator/range tests and final full-file verification.
3. **Verify:** authenticate metadata, length, hash, and Windows publisher trust. Fail closed; incomplete or integrity-invalid downloads are removed. Cached TUF-verified bytes are never execution authorization after a publisher-check failure. Atomically cache only TUF/hash-verified complete bytes; keep Windows publisher authorization as an additional required gate before execution. Reverify immediately before execution, reject symlinks/reparse paths, and retain file/directory handles to reduce replacement races between verification and launch. This is not atomic handle-based execution. No shell interpolation. These defenses do not claim protection from arbitrary malware already running as the same user.
4. **Install and close:** separate explicit confirmation; “Later” remains available. Disable new transfers/scans/account changes, invalidate pending sync plans, and wait for existing transfers, analysis, token writes, and worker/helper processes to settle. A cancellation signal is not evidence of completion. If work will not stop safely, leave installation deferred; never kill a transfer, CAD process, or the app to force progress.
5. **Handoff:** preserve an exclusive install/run gate across app exit and installer start, and wait for the old process to exit. The implementation uses matching application mutexes and Inno `AppMutex`, plus coordinated launch/install exclusion for the handoff race. Existing per-profile locks alone do not cover multiple custom profiles using the same install. Keep automatic application closing/restarting disabled. Inno's AppMutex checks running apps; `CloseApplications=force` can lose unsaved work. [AppMutex](https://jrsoftware.org/ishelp/topic_setup_appmutex.htm), [CloseApplications](https://jrsoftware.org/ishelp/topic_setup_closeapplications.htm)
6. **Finish:** the helper records installer completion, cancellation, failure or deferral locally. Reopen CacheCAD normally; the current helper does not relaunch it or claim healthy application activation. A future automatic relaunch must bind the verified installed payload and same non-elevated user. A future healthy-first-launch protocol must distinguish installer completion from successful activation. Never resume a prior sync plan automatically.

## 5. Preserve data and make recovery honest

The current separation is a useful baseline:

- Program: `%LOCALAPPDATA%\Programs\CacheCAD Reconstructed`.
- Default profile: `%LOCALAPPDATA%\CacheCADReconstructed\v1` (`maintained/utils/application_mode.py`). Keep this path across compatible application versions; `v1` is the profile format generation, not an app-release number. Preserve an explicit `--profile-dir` too.
- Profile marker: `application-profile.json`, currently exact schema 1. Settings and project index: `app_data/settings.json` and `app_data/projects.csv`. User project folders may be elsewhere; do not move, overwrite, or delete them during an update. Inventory project-local `.cachecad` metadata before any future format change.
- Session: `app_data/google-session.dpapi` passed explicitly by the production auth manager. The token store's alternate standalone default path is not the production profile path. Preserve the DPAPI magic, optional entropy, record schema, client ID, scopes, and owner OAuth configuration for compatible upgrades. Normal installation requires no new Google grant.

DPAPI data normally decrypts for the same Windows user on the same computer, and decryption requires matching optional entropy. It is not a portable login backup. Never decrypt tokens in the installer, change to machine-wide protection, or copy plaintext credentials. A changed client ID/scope/schema may require sign-in again; disclose that rather than silently discarding the old cache. [Microsoft DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)

**First update releases:** preserve schema 1 and keep a tested prior signed installer. The installer supports exact-payload same-version repair and refuses older releases. A controlled compatible rollback path remains future work; retaining an older installer does not make a downgrade supported. Do not describe the existing in-place file copy as a transactional, power-loss-safe rollback mechanism. Block unsupported downgrades in both the app and standalone installer; allow same-version repair only for the exact approved build. One version/build identifies one installer digest. Use one generated version source for UI, package, installer/PE metadata, diagnostics, provenance, and update comparisons; compare parsed versions, never strings or equality alone. Inno PE version metadata accepts up to four numeric components. [VersionInfoVersion](https://jrsoftware.org/ishelp/topic_setup_versioninfoversion.htm)

**Before automatic rollback or schema migrations:** implement and test a recovery transaction. Prefer versioned payload directories behind a small signed launcher, with the previous payload retained and a journaled/atomic active-version switch; Inno remains the full installer. Keep bootstrap replacement and its recovery path explicitly tested. Under exclusive profile access, snapshot only mutable app metadata (DPAPI bytes stay encrypted), validate a migration on a copy, then commit with a recoverable journal. Do not snapshot or restore user CAD files as part of an app upgrade.

Require supported read/write schema ranges. First launch must validate profile loading/migrations before enabling authentication refresh or sync. Failed activation may revert the payload and metadata only before post-update writes begin and only when the old reader supports the restored schema. After user activity or token rotation, blindly restoring an old profile can lose work or resurrect a stale refresh token: preserve the current profile and use an explicit supported recovery/forward-fix path instead. Never run older code against an unsupported new schema, restore an old cloud state, or downgrade below a known security floor without a specifically reviewed recovery decision. A controlled code rollback is distinct from accepting replayed update metadata; metadata anti-rollback state must stay monotonic.

## 6. Delivery phases and remaining acceptance gates

P0/P2 implementation and fixture tests now exist. The table retains the original
acceptance criteria; native final-artifact and signed end-to-end evidence must be
checked for each release rather than inferred from code or synthetic CI fixtures.

| Priority | Work | Exit evidence |
| --- | --- | --- |
| P0: local foundations | Unify product/version metadata; specify stable AppId/profile/client ownership; design install exclusion; add N→N+1, repair, unsupported downgrade, and settings/project-index/session persistence tests using synthetic data. | Windows results prove the exact upgrade paths and unchanged project bytes. No hosting/signing dependency is required for these local tests. |
| P1: first distributable updates | Obtain owner distribution/signing approvals; sign and timestamp final bytes; run upgrade tests; publish manually to approved durable downloads. Keep schema unchanged. | Clean-machine and existing-install tests of final signed artifacts; prior-version repair/rollback instructions and verified downloads. Live OAuth/CAD validation remains a separate release-readiness gate, not supplied by this plan. |
| P2: user-approved in-app flow | Add new TUF-backed discovery/download service, Windows trust verifier, cancellation UI, and install handoff. Keep old updater excluded. | Tests reject tampering, wrong signer/product/channel/architecture, stale/expired metadata, replay, downgrade, untrusted redirects, truncated/oversize files, signature revocation failures, and file substitution. Test offline, timeout, disk full, Cancel at every phase, repeat clicks, active/unsettled sync, tray-open app, competing profiles/installers, and relaunch failures. |
| P3: migration/recovery hardening | Add versioned payload activation, health gate, metadata snapshots/journal, tested schema migrations, and root/publisher key rotation/recovery. | Crash/power-loss injection at every boundary, migration rollback before writes, safe refusal after writes, cleanup limits, and restoration drills. Do not advertise unattended recovery before these pass. |

For P0/P2, use synthetic Google sessions protected by real Windows DPAPI in disposable profiles, mock update servers, and test-only signing fixtures isolated from production trust. No real account or CAD project is needed. The Windows workflow now includes an unsigned synthetic-prior-version upgrade harness. Its results, when run successfully, establish only the covered mechanics and preservation checks. They do not validate production signing, all historical versions, public distribution or automatic recovery.

## 7. Approval questions for the owner, when those phases begin

These are future decisions, not actions authorized by this document:

1. **Publication:** “May we publish the signed CacheCAD installers, release notes, checksums, and public update metadata to a separate public distribution repository under [verified owner/name], while keeping the source repository private?” Confirm the exact repository and released files before creating or publishing anything.
2. **Signing:** “Which legal publisher identity and signing provider should we use, and what spending limit and key-custody arrangement do you approve?” Present the actual provider, price/commitment, terms, and required identity data before purchasing/enrolling. Key creation, persistent credentials/grants, and security settings need separate approval at the action; private keys must not enter chat or source control.
3. **Release authority:** “Who may approve and publish releases, and may an approved publishing identity write only to [distribution repository] after that approval?” Specify credential scope and storage before creating persistent access. Do not infer publishing permission from permission to build.
4. **Product/channel policy:** “Should this derivative keep its current product/install identity, and should users receive only stable releases unless they explicitly opt into beta?” Confirm the public product name, minimum Windows version, support window, and rollback security floor before freezing release metadata.
5. **Hosting alternative, only if needed:** “If a public distribution repository does not meet your access requirements, which exact download service, audience, cost limit, and retention policy do you approve?” No new service or paid hosting has been created.

End users separately choose whether to download and whether to close CacheCAD and install. Signing, public availability, or an owner's release approval does not authorize interruption of an end user's work.
