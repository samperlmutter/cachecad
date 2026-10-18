# Identity-only development sign-in (historical test mode)

**This is not the end-user setup guide.** The maintained application uses one
owner-configured Desktop OAuth client and a persistent Drive-authorized session.
See [production Google setup](PRODUCTION_GOOGLE_SETUP.md) for the current
architecture, owner approvals and release requirements. The instructions below
apply only to the separately retained identity-only development test mode.

Checked against Google's official documentation on **2026-10-18**. This guide is
for the reconstructed app's identity-only sign-in milestone. It does not authorize
Drive access or establish that a real account has successfully signed in.

## What this milestone means

The intended result is a validated Google identity displayed in CacheCAD for the
current app session. This milestone requests only `openid email`. These scopes
do not authorize Drive file access; `profile` is deliberately unnecessary here.
Use the verified `sub` claim as the account identifier, rather than an email
address. Validate the ID token's signature, issuer, audience, expiry and the nonce
sent for this attempt before displaying success. Profile fields can be absent.
[Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect)

Drive scans, transfers, project access, vendor account validation, telemetry and
updater activity must stay disabled in this mode. Do not send identity-only
credentials into the recovered production sign-in handler: that handler creates
a Drive client and enables sync. Successful authentication does not establish
transfer safety, vendor licensing, CAD compatibility or production readiness.

## User-managed Google Cloud setup

Perform these account changes yourself. This research did not create a project,
client, token, consent grant or publishing change.

1. Open the [Google Cloud console](https://console.cloud.google.com/), select a
   project you control, or create a separate personal project. A separate project
   avoids mixing this reconstruction with the vendor's app or unrelated grants.
2. Open **Google Auth platform → Branding** and complete the required app name,
   support email and developer contact fields. Use an honest personal-test name,
   such as “CacheCAD Personal Test.” In **Audience**, use **External** for a
   personal Google account; **Internal** is limited to an eligible organization.
3. Keep the app in **Testing** for initial personal use. In **Data Access**, review
   only the identity scopes above. Do not add any Drive scope or enable the Drive
   API for this milestone. Consent configuration does not replace the application's
   actual scope request: inspect the real permission screen before consenting.
   [Consent-screen setup](https://developers.google.com/workspace/guides/configure-oauth-consent)
4. In **Google Auth platform → Clients → Create Client**, choose **Desktop app**,
   name the client, create it, and download its Desktop client JSON for the local
   configuration chooser.
   Do not choose Web application, service account or API key. Desktop setup does
   not require the Web application's JavaScript-origin/redirect-URI form.
   [Desktop client creation](https://developers.google.com/workspace/guides/create-credentials#desktop-app)
5. Keep any downloaded client configuration outside this repository, build
   outputs, synced folders and bug reports. Do not paste it, tokens, authorization
   codes or full callback URLs into chat. Never restore the vendor's sanitized
   credentials. Follow the verified launcher interface described below rather
   than putting files into the original `creds.json` or `token.json` locations.

### Testing, test users and publishing

Google documents a specific exception for requests containing **only**
`openid`, `email`, `profile` or their equivalents: users do not have to be listed
as test users, do not receive the Testing warning, and authorization does not
automatically expire after seven days. Adding your own account as a test user is
optional for this identity-only milestone; the list is not an access-control
boundary for it.

With any additional OAuth scope, that exception no longer applies. Normal Testing
is limited to listed test users, up to 100, with seven-day authorizations and
corresponding refresh-token expiry. Publishing is not needed just to try this
identity-only flow, and does not itself satisfy verification requirements.
Branding, sensitive/restricted scopes and organizational controls can introduce
other requirements. Workspace policy or Advanced Protection may still block
authorization. Do not bypass such a block.
[Google's audience and publishing rules](https://support.google.com/cloud/answer/15549945)

## Required desktop flow

Use the system browser and authorization-code flow. Bind a listener to
`127.0.0.1` on port `0` first, then use the allocated port in
`http://127.0.0.1:<port>/`. Never bind to `0.0.0.0` or reuse the original fixed
port `8765`. HTTP is appropriate only for this local loopback callback; Google
authorization/token traffic remains HTTPS.
[Google's desktop loopback support](https://developers.google.com/identity/protocols/oauth2/resources/loopback-migration)

Generate fresh PKCE verifier/challenge (`S256`) and unpredictable `state` for every
attempt. Validate the returned state before exchanging a code; use the same
redirect URI and verifier in the exchange. Desktop clients are public clients;
their bundled client secret is not a security boundary. Google's current endpoint
documentation marks `client_secret` optional, but confirm compatibility with the
actual library used. The native-app guide also states that installed apps do not
support incremental authorization: future Drive integration needs separate design
and consent review.
[Google desktop OAuth guide](https://developers.google.com/identity/protocols/oauth2/native-app)

Additional implementation requirements for this reconstruction:

- Use a fresh OIDC nonce and verify it in the ID token.
- Accept only the expected callback path, method and one outstanding attempt.
  Reject duplicate/missing state or code, wrong-state callbacks and replay.
- Handle denial, timeout, browser-open failure, cancel, repeated clicks and app
  closure; always release the socket and discard partial credentials.
- Never log callback queries, codes, tokens, secrets or account-profile contents.
  Avoid loading remote content in the callback response.
- Do not report “signed in” in the browser before token validation finishes.
- Do not trust arbitrary token/auth endpoints supplied in a configuration file.
  Restrict network destinations to the reviewed Google identity endpoints.

## Session-only storage

For this milestone, keep the validated identity in memory and clear it on sign-out
or exit. Do not read or write the original token cache, retain refresh tokens,
silently refresh credentials, or add a “remember me” feature. Minimize retention
of the access/ID tokens after validation. Use explicit online access where
supported and verify the actual request; library defaults can request offline
access. Google may still issue a refresh token for an installed app: discard it.
[Desktop token behavior](https://developers.google.com/identity/protocols/oauth2/native-app#exchange-authorization-code)

Session-only means no app-persisted login. It does **not** promise that Google
creates no consent record, that browser cookies disappear, or that app sign-out
revokes Google authorization. Users can separately review/remove access in their
Google Account settings. Browser sign-in may make a later app login quicker.

If persistence is introduced later, require a separate deliberate design:
platform-backed credential storage, expiry/revocation handling, and explicit
disconnect semantics. A plaintext JSON token file is inappropriate. Google's
guidance recommends secure platform storage such as Windows Credential Locker
or macOS Keychain and checking state on every OAuth response.
[Google OAuth security practices](https://developers.google.com/identity/protocols/oauth2/resources/best-practices)

## Opt-in launch and verification boundary

The ordinary launch remains offline and rejects live credentials and account
login. Do not remove its guardrails or copy credentials into its temporary profile.

The implementation provides a separate `--signin` opt-in mode. From a verified
source assembly, launch it with:

```sh
python /path/to/app-source/development/entrypoint.py --app-source /path/to/app-source --signin
```

The packaged equivalent is `CacheCAD-Offline.exe --signin`; the optional **CacheCAD Sign In Test**
Start Menu shortcut supplies the flag. Use a newly built artifact that
explicitly includes this feature; older offline installers cannot sign in.

In this mode, click **Sign In**, choose the local Desktop client JSON in the
dialog, then continue in the system browser and review Google's permission
request. Cancel if it requests Drive access. Return to CacheCAD for the verified
email result. No credential values belong in command-line arguments or environment
variables; the app must not save a copy of the selected file or its contents.
Closing the app ends its local identity session, but the user-managed original
client JSON remains where the user downloaded it.

The code and offline protocol/GUI lifecycle tests are implemented. Require a
successful CI artifact with the matching installed sign-in smoke report before
trying the packaged mode. **Live Google login has not been verified.** A command,
synthetic successful identity, or green installer smoke test does not establish
a completed real Google authorization.

Before any live test, confirm that the opt-in build:

1. Has passed offline tests for callback validation, cancellation, repeated login,
   timeout, token-validation failures and cleanup.
2. Requests exactly identity scopes and never constructs the original Drive,
   vendor-validation or telemetry clients on successful sign-in.
3. Creates no token/config copies, account-bearing log files or credential cache.
4. Shows success only after validation, clears identity on sign-out, and starts
   signed out after restart.

A live test still requires the user's own Desktop client and browser consent on
the same computer as the app. Offline tests cannot verify Google's acceptance of
that client, organizational policy, browser return behavior or a real token
exchange. Complete that test before calling live sign-in verified.
