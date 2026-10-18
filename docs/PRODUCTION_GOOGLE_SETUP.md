# Production Google access: owner setup and release gates

Reviewed against official Google and Microsoft documentation on **2026-10-18**.
This records the implemented production path and remaining setup/release gates,
not evidence of Google approval, a working real-account integration, or permission
to publish this reconstruction.
No Cloud Console changes, clients, credentials, consent grants, live-account tests,
or public releases were performed for this document.

## The intended end-user experience

The application owner configures Google once for each deployment tier. An end user
installs the application, clicks **Sign in with Google**, chooses their Google
account in their ordinary browser, reviews the permissions, and returns to the app.
They do not create a Cloud project, create an OAuth client, or select a client JSON
file. Persistent access is disclosed and saved securely for that Windows user.

`SIGNIN_SETUP.md` describes an earlier, identity-only developer experiment. Its
per-user client-file chooser, session-only credentials, and disabled Drive features
are not the production onboarding path. Existing offline smoke tests and an
email displayed after sign-in cannot establish working project sync.

### Implemented now, still awaiting live validation

- `maintained/core/desktop_signin.py` provides real Desktop authorization with
  the configured Drive scopes, PKCE/state/nonce checks and verified identity.
  `maintained/core/google_session.py` wraps actual Google SDK credentials with
  secure persistence, restore, refresh, cancellation, sign-out and revocation.
- `maintained/utils/windows_token_store.py` implements current-user DPAPI with
  encrypted temporary files and atomic replacement. `profile_lock.py` in the same
  directory prevents concurrent processes using one application profile.
- `maintained/core/auth_manager.py`, the controller and
  `maintained/gui/application_account.py` expose actual account lifecycle states.
  Successful verified credentials construct the real `DriveClient`; missing
  owner configuration produces **Setup required**, not a fabricated user.
- `application/launch.py` is the normal application entrypoint. It verifies the
  source manifest, uses an isolated persistent profile, blocks retired vendor
  services and excludes development backends. `--startup-check` is a separate,
  network-denied render of the real signed-out UI, with account restoration off.
- Production assembly and the Windows packaging recipe target `CacheCAD.exe`
  and `CacheCAD-Setup.exe`. They include the real Google libraries, CA bundle and
  Drive discovery document, with an optional owner-supplied Desktop configuration.
  These source/build changes do not prove a freshly built installer passed Windows
  validation; the matching artifact and its installed tests must be checked.

A focused offline rerun at this document refresh passed for Google sessions,
token storage, profile locking, production authentication review, and provider
lifecycle using the installed Google runtime. Native-Windows DPAPI coverage was
skipped on Linux. The broader source/aggregate gate and separate real-Qt
startup/lifecycle checks also passed. Synthetic test tokens and intercepted
network calls test behavior without providing a real Google grant. These results
do not establish a new installed-Windows artifact's behavior or live integration.

## 1. Scope decision based on the real operations

Inspection of `maintained/core/drive_client.py` found these actual operations:

| Feature | Google API operation in the current implementation |
| --- | --- |
| Resolve an existing project root | `files.get`; optionally `drives.get` |
| Scan the complete existing project tree | Paginated, recursive `files.list` by parent |
| Download CAD/binary content | `files.get_media` |
| Create remote folders and files | `files.create` |
| Upload replacement revisions | `files.update` with media |
| Delete a cloud item from the project | `files.update(trashed=True)`, followed by verification |

The recovered authentication manager requests `openid`,
`https://www.googleapis.com/auth/userinfo.email`, and
`https://www.googleapis.com/auth/drive`. The separate maintained identity-only
flow still requests only the first two; the production
`DesktopAuthorization`/`GoogleSession` defaults request all three.

**Recommended production target for the existing feature set:** retain those
identity scopes plus `https://www.googleapis.com/auth/drive`, subject to owner
approval and Google's review. This is an engineering recommendation for the
current recursive, bidirectional sync contract; Google makes the final approval
decision. Do not silently enable the scope or pretend it has been approved.

Google classifies `drive` as **restricted** and `drive.file` as **non-sensitive**.
The latter covers files created by the app or specifically opened/shared with it.
Backup/sync is among Google's permitted categories for restricted Drive access.
`drive.readonly` and broad metadata scopes are also restricted; they do not replace
the write capability needed here.
[Google Drive scope definitions](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)

### Why `drive.file` is not a drop-in replacement

An existing project folder ID is an identifier, not an OAuth authorization grant.
Selecting a folder must not be assumed to authorize all pre-existing descendants,
future collaborator files, or an entire shared drive. This conclusion follows
from the per-file authorization model above; no live test of descendant behavior
was performed. A successful limited file listing must not be treated as proof of
a complete sync snapshot.

There is also an explicit endpoint mismatch: the current shared-drive-name lookup
uses `drives.get`, which accepts `drive` or `drive.readonly`, not `drive.file`.
[Google `drives.get` reference](https://developers.google.com/workspace/drive/api/reference/rest/v3/drives/get)

A `drive.file` edition would need a different, explicitly approved product
contract: app-created or individually authorized files, supported selection
onboarding, and comprehensive tests for collaborators, nested folders, newly
added files, lost access, and missing items. Google Picker is a web selection UI;
adding it is integration work, not changing a string in the current folder dialog.
[Google Picker overview](https://developers.google.com/workspace/drive/picker/guides/overview)

Do not choose that edition merely to avoid verification while still promising
the original whole-project functionality. The present target does not require
a separate Drive API key, a shared service-account identity, or domain-wide
delegation.

### Permissions and shared drives remain independent checks

OAuth scope does not override a user's file permissions or organization policy.
Read `capabilities` and handle read-only, missing, moved, inaccessible, and
non-trashable items explicitly. Folder identity should come from MIME type;
`canAddChildren` describes an operation permission, not whether an item is a
folder. The inspected scanner uses that capability to classify folders, so this
is a code-review gate for restricted project access.
[Drive permissions and capabilities](https://developers.google.com/workspace/drive/api/guides/manage-sharing)

The existing scan uses `supportsAllDrives`, `includeItemsFromAllDrives`, and
`corpora='allDrives'`, and now fails closed on `incompleteSearch`. Review every
relevant operation, including download, for shared-drive support. Prefer a known
drive corpus and `driveId` when appropriate. Any failed/incomplete traversal must
block destructive sync decisions rather than represent missing files as deleted.
[Shared-drive integration](https://developers.google.com/workspace/drive/api/guides/enable-shareddrives)

## 2. One-time application-owner configuration

These are owner actions still to be completed, not instructions for end users.
Use an owner-controlled project and an honest application identity, without
claiming to be the upstream vendor. Keep production and development projects
separate. Keep owner/support contacts current; do not commit downloaded client
configuration or user tokens to source control.
[Google OAuth policies](https://developers.google.com/identity/protocols/oauth2/policies)

1. **Choose the accountable owner and audience.** Record the project, responsible
   organization/person, support contact, application name, website, and privacy
   policy. A general real-user product needs an External audience. Internal is
   appropriate only if access is intentionally restricted to the project's
   eligible organization; it is not a route for serving unrelated customers.
2. **Enable the Google Drive API** in the owner-controlled project. Configure
   **Google Auth platform → Branding, Audience, and Data Access**. Declare the
   exact implemented scope set. The consent setup includes acceptance of Google's
   User Data Policy; the owner must review the terms before accepting them.
   [Consent configuration](https://developers.google.com/workspace/guides/configure-oauth-consent)
3. **Create one Desktop app OAuth client** for the Windows application under
   **Google Auth platform → Clients → Create Client → Desktop app**. A Web client,
   API key, or service-account key is not the replacement for this installed-app
   login. Use separate clients where another platform requires them.
   [Desktop client creation](https://developers.google.com/workspace/guides/create-credentials#desktop-app)
4. **Supply the approved Desktop client metadata to the private build pipeline.**
   The app, not the end user, receives the owner-configured client ID and any
   library-required Desktop metadata. Validate client type and fixed Google
   endpoints at build/startup. Reject absent, placeholder, unexpected or upstream
   configuration with a clear owner-setup error, never synthetic login success.
5. **Complete branding/data-access review before general availability.** Verify
   domain ownership, use a public app homepage and a same-domain privacy policy,
   and link the policy consistently. Explain local caches, account retention,
   sync activity, deletion, diagnostics, and any other recipients accurately.
   Publish approved branding, then follow the Verification Center's data-access
   review. Do not interpret a published audience setting as scope approval.
   [Brand verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification)

Desktop clients cannot protect a bundled client secret as a confidential server
credential. Google currently documents the token endpoint's `client_secret` as
optional for this flow. Confirm the pinned library's actual compatibility; never
substitute a Web-client secret or ship a service-account private key. A client ID
identifies this app; each user's authorization still produces separate user tokens.
[Google Desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)

### Implemented configuration routes

The Windows builder accepts `--client-config /path/to/owner-client.json`. It
validates and sanitizes that explicitly supplied Desktop metadata, then includes
`config/google-desktop-client.json` in the hashed assembly. Without it, the build
contains no usable owner OAuth configuration and opens the setup-required state.

For an owner-managed local deployment, `application/launch.py` also accepts
`--client-config` or the `CACHECAD_GOOGLE_CLIENT_CONFIG` file-path setting. These
refer to a local configuration file, not a token or credential value in command
arguments. The default is the packaged configuration path above. Keep developer
and end-user instructions separate: this is deployment configuration, not a client
JSON chooser that each end user must operate. An externally supplied configuration
is validated at runtime; it does not become part of the packaged source manifest.

## 3. Publishing, verification, and assessment

For Drive access, External **Testing** is limited to listed test users, up to 100;
authorization and refresh tokens normally expire after seven days. The
identity-only exception does not apply after a Drive scope is added. Moving to
**In production** does not remove the unverified-app warning or unapproved-scope
user cap. Google documents a lifetime cap of 100 new users for apps showing that
warning. Workspace administrator restrictions can still block an approved app.
[Audience and publishing rules](https://support.google.com/cloud/answer/15549945)

A public product requesting restricted Drive access must complete the relevant
scope verification unless a genuine exception applies. Prepare the exact scope
justification and an end-to-end demonstration of real consent and each feature
that uses the scope. Include why per-file authorization cannot fulfill the
intended existing-project synchronization workflow. Google can request narrower
access or reject an insufficient justification.
[Verification requirements](https://support.google.com/cloud/answer/13464321)

**Security assessment is an architecture question, not simply a scope label.**
Google's detailed restricted-scope guidance requires an annual assessment when
the app can access restricted data from or through a third-party server. A truly
local desktop flow may avoid that server-side trigger, but this does not waive
restricted-scope verification. Do not promise an exemption without a complete
data-flow inventory and confirmation during Google's review.
[Restricted-scope review and assessment](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)

Google's assessment help describes CASA and says Trust & Safety contacts the
developer when to start the assessment after other verification steps. Follow
the review determination for this app; do not budget an invented fixed price,
guaranteed exemption, or guaranteed approval date.
[Security assessment process](https://support.google.com/cloud/answer/13465431)

### Current data-flow status and remaining audit

The maintained sync executor's vendor telemetry invocation has been removed.
Production assembly excludes the recovered cloud logger, bug reporter,
service-credential manager, user validation, updater, old OAuth callback/importer,
file logger, legacy entrypoint and development sign-in dialog. The normal launcher
also blocks the retired vendor modules and development imports. Successful
production sign-in uses the verified token identity rather than the recovered
profile-image/user-info request. These are real source/payload exclusions, not
services replaced by fake success responses.

The production launcher suppresses the recovered raw stdout/stderr streams and
disables Python logging rather than uploading them or enabling a file logger.
Local project/database/sync records still need a complete data-content, retention
and diagnostic-export audit. The Drive client retains logging/profile helper
code even though the production controller does not invoke its profile-fetch
path at sign-in. Any future logger or support-export feature must be reviewed
before it is enabled.

The faithful `recovered/` baseline still contains the original vendor reporting
implementations for analysis; their presence there is not evidence they ship in
the production payload. No vendor transfer was performed during this work.
The exclusions above improve the architecture but do not by themselves establish
a complete local-only data-flow audit or a Google security-assessment exemption.
Google approval does not confer vendor licensing, brand rights, distribution
rights, or authority to use vendor backends. Any future support service must be
owner-operated, accurately disclosed, implemented genuinely, and reviewed before
use; upstream credentials and service-account keys must not be restored.

## 4. Desktop authentication and Windows persistence contract

This section describes the implemented production contract and review criteria.
The identity-only development flow remains separate. Source inspection and offline
tests do not establish acceptance by Google or correct native-Windows behavior.

### Authorization

- Use the system browser, authorization-code flow, fresh PKCE (`S256`) and state.
  Google's installed-app guide does not support incremental authorization; do
  not assume identity-only grants can silently grow into Drive grants.
- Use the Desktop-supported loopback callback. Bind `127.0.0.1` to an available
  port before opening the browser; accept only the expected method/path, one
  active attempt, and a matching state. Never bind the callback to a LAN address.
  [Desktop loopback support](https://developers.google.com/identity/protocols/oauth2/resources/loopback-migration)
- Verify OIDC signature, issuer, audience, expiry and per-attempt nonce. Associate
  account storage with verified `sub` and the client/environment, not mutable
  email text. Check granted scopes before constructing a Drive client. Missing
  Drive permission must leave sync disabled with a genuine authorization error.
  [Google OIDC validation](https://developers.google.com/identity/openid-connect/openid-connect)
- Handle denial, browser failure, timeout, repeat clicks, app closure and stale
  callbacks without persisting partial credentials or emitting stale success.
  Redact codes, tokens, callback queries, identities and provider exceptions from
  logs and crash reports. HTTPS token operations need bounded timeouts.

### Persistent token storage

Google requires secure token storage and recommends platform credential storage,
including Windows Credential Locker. A plaintext `token.json`, a JSON file in
AppData with an innocuous name, or a fixed bundled encryption key is insufficient.
[Google token-storage guidance](https://developers.google.com/identity/protocols/oauth2/resources/best-practices)

This Windows app now implements a **current-user DPAPI** encrypted token record
through `maintained/utils/windows_token_store.py`.
DPAPI normally binds decryption to the same user and computer; using
`CRYPTPROTECT_LOCAL_MACHINE` allows other users on that machine to decrypt it and
must not be used here. DPAPI is not protection from malware already running as
that user, and recovery/password-reset scenarios can make records unreadable.
[Microsoft DPAPI documentation](https://learn.microsoft.com/en-us/windows/win32/seccrypto/example-c-program-using-cryptprotectdata)

Current application-specific storage behavior and review criteria:

- The default Windows profile is `%LOCALAPPDATA%/CacheCADReconstructed/v1`;
  the encrypted cache is `app_data/google-session.dpapi` below it. An explicit
  `--profile-dir` must be independently suitable and non-synced. The versioned
  record contains the client ID, exact scopes, verified identity (`sub`/email),
  refresh token, access token and expiry. ID tokens are not persisted. The saved
  access token allows SDK use after restart until it needs refresh.
- Cache restoration rejects mismatched schema, client, scopes or malformed
  identity/token records. Different deployment tiers need separate owner clients
  and profiles. The process-lifetime profile lock is held before reading state;
  its lock file must not be deleted while an instance is running.
- Writes encrypt before opening a temporary file, then replace atomically.
  Refresh is serialized and persists a rotated refresh token before exposing the
  new access token. A response without a new refresh token retains the old one;
  malformed/partial responses do not replace a valid record.
- Storage errors fail closed with no plaintext fallback. The current production
  flow does not offer a session-only fallback. Explicit fresh authorization can
  replace an unreadable old cache; a failed cache deletion exposes a retryable
  cleanup-required state rather than claiming cleanup succeeded.
- Local sign-out invalidates retained credential objects. The controller blocks
  sign-out/disconnect during unsettled transfers and retires old sync clients.
  Terminal credential invalidation during a transfer can require restart before
  reauthorization. Cancellation and late-result handling are tested offline.
- The launcher uses a marked isolated profile, rejects legacy `token.json`,
  `creds.json` and `temp.json`, and does not import old profiles automatically.
  The current installer preserves separate profile/project data on uninstall;
  uninstall is not a promise of Google revocation or token removal.

### Refresh, sign-out, and disconnect

Refresh tokens are not permanent: revocation, prolonged non-use, token-count
limits, time-limited grants, or policy changes can invalidate them. Handle terminal
`invalid_grant` by stopping dependent work and requesting a new login; transient
network/server failures require bounded retry without discarding a still-valid
record. Never create an unlimited consent/retry loop.
[Google refresh-token lifecycle](https://developers.google.com/identity/protocols/oauth2#expiration)

Expose distinct actions with accurate effects:

1. **Sign out on this computer:** cancel local activity, clear memory and the
   local secure token record. Explain that Google-side authorization remains.
2. **Disconnect Google access:** request revocation, then clear local credentials.
   Google documents `https://oauth2.googleapis.com/revoke`; revocation can affect
   all grants/tokens for that user's project, including its other OAuth clients,
   and propagation can take time. Disclose that consequence before proceeding.
   [Google revocation semantics](https://developers.google.com/identity/protocols/oauth2/native-app#tokenrevoke)
3. If revocation fails, the current implementation reports failure and preserves
   the remembered session for a retry; it does not claim Google access was
   disconnected. The user can separately choose local sign-out or visit
   [Google Account connections](https://myaccount.google.com/connections) to
   remove access themselves. Never label local deletion as successful revocation.

The implementation sends the token as an HTTPS form POST body, with fixed Google
endpoints, TLS checks, redirects disabled and bounded responses. No token belongs
in a support message or chat. A new release still needs real revocation and
failure-path validation using an explicitly authorized account.

## 5. Exact blockers and required approvals

### Owner decisions/setup still needed

- Confirm ownership and authority for the application identity, Google project,
  privacy/support domain, and intended real-user audience.
- Approve the existing-project full-Drive scope contract, or explicitly choose
  the materially different per-file product before live access is enabled.
- Authorize the named project/client setup and persistent-access configuration
  before anyone creates credentials or changes security/access settings. The
  owner supplies approved Desktop metadata through the agreed build/config route;
  end users do not supply developer credentials.
- Review and accept applicable Google terms and submit the owner-approved brand,
  policy, scope explanation, and demo when ready. Account changes, domain changes,
  verification submissions, and publishing are separate actions from drafting
  this plan. Public app release remains unauthorized.
- Resolve legitimate vendor licensing/backend dependencies. No fake license
  response, vendor-account bypass, reused upstream keys, or service-account key
  bundled into a client is an acceptable substitute.

### Engineering and verification gates still needed

- Supply the approved owner configuration and verify the implemented real
  authorization, refresh, local sign-out, revocation and error paths against the
  intended accounts. Current source wiring and offline tests do not complete this.
- Complete the outbound/local data-retention audit, including sync records and
  any future diagnostics, and establish the final assessment/privacy posture with
  Google where required. Retired vendor modules must remain excluded.
- Run installed-Windows tests for secure restart, account switching, expired and
  revoked grants, storage failures, cancellation races, and uninstall behavior.
- After explicit account and test-data authorization, validate real Google login,
  refresh after restart, My Drive and shared-drive trees, collaborator changes,
  complete scans, permissions, upload/download/revisions, and safe trash behavior
  using designated disposable project data. Do not test destructive operations
  on working CAD projects.
- Confirm completed verification/status, the final packaged client's identity,
  and applicable organization policy before claiming general-user readiness.
  Mock protocol tests, a green build, and this document are useful prerequisites,
  not substitutes for those checks.
