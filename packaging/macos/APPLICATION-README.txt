CacheCAD reconstructed application: Apple Silicon macOS tryout

This bundle contains the real reviewed application, Qt UI and Google Drive SDK.
It is not the offline prototype. Requires an Apple Silicon Mac (M1/M2 or newer)
and macOS 12 or newer. Native startup is tested separately in the build workflow.

Open CacheCAD.app from Finder. It uses an isolated application profile and does
not import the original application's credentials or project list. Google sign-in
requires owner-supplied Desktop client configuration. The macOS application uses
Keychain for persisted sessions; it does not import plaintext legacy credentials.
Live Google access is not a CI test.

This test build is ad-hoc signed for Apple Silicon execution. It has no Developer
ID signature and has not been notarized. Gatekeeper may block downloaded copies.
No build step disables Gatekeeper, removes quarantine, changes trust settings,
creates signing accounts, or installs an updater. Ask the distributor for a
Developer ID signed and notarized build if macOS will not open this one.

The macOS build has no automatic installer/updater. To replace it, quit the app
and replace the .app manually. Removing the .app does not remove its profile.
Third-party license notices and distribution metadata are retained in the bundle.
