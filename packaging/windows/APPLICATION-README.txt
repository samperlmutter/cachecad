CacheCAD Reconstructed 0.1.0

This is an unsigned reconstruction of the maintained CacheCAD application.
It is not the original vendor installer or a production-certified release.

The normal shortcut opens the real Qt application, real Google sign-in and Drive
integration. No development launcher, synthetic remote or fake account backend
is shipped. Neutral authored artwork replaces unavailable vendor resources.

The app owner must provide a Google Desktop OAuth client configuration. The build
can include one explicitly with --client-config, or the owner can configure a
local file with CACHECAD_GOOGLE_CLIENT_CONFIG or the application's --client-config
argument. Client metadata is public desktop-app configuration, not an end user's
Google password or token. End users do not create their own OAuth project.
If configuration is absent, the application shows a genuine setup-required state.
No original vendor credentials, service account, user token or real project is
included in the default CI artifact. Sign-in requires the user's browser consent.
Persistent Google credentials use Windows user-bound DPAPI protected storage.
App sign-out does not sign out the browser or revoke Google's previous consent.

The installer does not launch the app automatically. It installs for the current
user, supplies a Start Menu shortcut and uninstaller, and requires no elevation.
No service, startup task, file association or network rule is added. Uninstall
removes application files, not the user's separate profile or project data.

Automated installed-app checks render the actual signed-out GUI using disposable
profiles and network disabled. They verify missing-configuration setup, real SDK
availability and native Windows Qt loading. They never perform a Google login,
write Google Drive files, or validate live CAD projects. Live authentication,
Drive transfer correctness, SolidWorks behavior and production readiness require
separate authorized validation. Existing sync overwrite-race limits still apply.

The console is intentional: it exposes diagnostics and startup-check results.
Source hashes detect unexpected changes; they are not a signed trust chain.
Startup-check tripwires are defense in depth, not an OS sandbox.

CPython's license notice is under _internal/third-party-notices/Python. PySide6,
Qt, shiboken6, Send2Trash, tzlocal, tzdata, PyInstaller, Google API/auth libraries,
requests and their bundled dependency notices are preserved in distribution
metadata/license directories. Qt DLLs remain separate in the onedir bundle.
App and launcher source are under _internal/app-source. Requests' CA bundle and
the official Drive v3 discovery document are included.
This notice grants no rights to upstream recovered code or vendor branding;
check relevant rights and third-party license obligations before redistribution.
