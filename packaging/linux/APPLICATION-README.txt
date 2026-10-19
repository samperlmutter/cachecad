CacheCAD maintained application for Linux x86_64

Extract the complete archive into a user-owned directory. Run ./CacheCAD/CacheCAD
from a terminal in your graphical desktop. Keep the executable and _internal
directory together. No Python installation, administrator access or system-wide
installation is needed. This package does not register autostart or file handlers.

This is the real maintained application, assembled from reviewed recovered source
and fixes. The separate src/cachecad prototype and offline fixture backend are not
used. Normal startup uses an isolated application profile; legacy credentials and
projects are never imported automatically. The first launch is signed out.

Google sign-in and Drive sync are unavailable on Linux until protected credential
storage is implemented. The app fails closed rather than writing plaintext tokens;
providing a Desktop client configuration does not remove this limitation. No
OAuth configuration, account credentials, sign-in or CAD files are included or
used in packaging/startup tests.

Read build-provenance.json alongside the download for the measured GLIBC floor.
A build made on a recent distribution may not run on an older one. The GLIBC
floor is only one compatibility condition. An x86_64 graphical Linux desktop with
a working X11 display (or XWayland) and host graphics drivers is required.
Qt/X11 libraries, including libxcb-shape.so.0, are collected in _internal; GLIBC
and graphics drivers remain host dependencies. If startup fails, from the folder
containing CacheCAD run:
  LD_LIBRARY_PATH="$PWD/CacheCAD/_internal:$PWD/CacheCAD/_internal/PySide6/Qt/lib" ldd CacheCAD/_internal/PySide6/Qt/plugins/platforms/libqxcb.so
Resolve any "not found" prerequisites with your distribution's package manager.
Wayland-native behavior and other desktop/driver combinations are not tested. The archive has no publisher signature and no Linux automatic installer.

For a safe signed-out startup check in a new disposable profile:
  ./CacheCAD/CacheCAD --startup-check --platform offscreen --profile-dir /tmp/cachecad-new-test
The check renders the actual window and exits. It blocks Python network, browser
and child-process operations; it is not an OS-level network sandbox. It never
claims to test live Google authentication or real CAD uploads/downloads.

Third-party distribution metadata and license notices are under _internal.
