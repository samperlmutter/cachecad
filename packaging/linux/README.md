# Linux x86_64 application distribution

This builds the actual maintained Qt application from the reviewed production
assembly. It does not freeze the `src/cachecad` replacement scaffold or ship the
offline-development backend. The archive contains `CacheCAD/CacheCAD`, its Python
runtime, real Google/TUF SDKs, Qt xcb/offscreen plugins, license metadata, and the
hash-verified first-party source under `_internal/app-source`.

## Build

Use native Linux x86_64, CPython 3.12 and `readelf` (binutils). Install the reviewed
runtime and packaging locks into a fresh virtual environment:

```sh
python3.12 -m venv .venv-linux
.venv-linux/bin/python -m pip install --require-hashes -r requirements/runtime-linux.lock -r requirements/linux-package.lock
.venv-linux/bin/python packaging/linux/build.py --output dist/linux-application
```

On Ubuntu 24.04 the tested build/runtime prerequisites are:

```sh
sudo apt-get install libegl1 libgl1 libopengl0 libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-shape0
```

The package collects the resolved Qt/X11 libraries, including `libxcb-shape.so.0`;
builds now reject a missing copy. GLIBC, a working X11 display (or compatible
XWayland), and host graphics drivers still come from the target computer. CI checks
`ldd` on the Qt xcb plugin before building. If another machine fails at startup,
check unresolved shared libraries from the extracted folder with:

```sh
LD_LIBRARY_PATH="$PWD/CacheCAD/_internal:$PWD/CacheCAD/_internal/PySide6/Qt/lib" ldd CacheCAD/_internal/PySide6/Qt/plugins/platforms/libqxcb.so
```

Any `not found` entry is an unresolved prerequisite, not a verified application
startup. Use the package manager for your distribution; do not download arbitrary
shared libraries. Hardware/driver and Wayland desktop behavior remain untested.

The build directory must be new. Linux system libraries required by Qt must be
installed on the build host. For a cloud environment with reviewed distribution
libraries extracted rather than installed, `--system-library-dir /absolute/path`
explicitly supplies that one directory for PyInstaller dependency collection.
Inherited `LD_LIBRARY_PATH` and Python/Qt injection variables are discarded. The
supplied library inventory is recorded in build provenance. No library directory
is normally required to run the extracted app.

The packaging-only lock uses official PyPI wheel hashes. Its evidence is in
`dependency-artifacts.json`; the existing Linux runtime lock remains unchanged.
The system OS/interpreter are not hash-locked. Build provenance records the
actual interpreter, platform, native ELF architectures and required GLIBC symbol
versions. It does not claim bit-for-bit binary reproducibility or universal Linux
compatibility. Build on the oldest supported distribution and test there: a
Debian 13 artifact may be unsuitable for Ubuntu 24.04 or older distributions.
[PyInstaller's Linux compatibility guidance](https://pyinstaller.org/en/stable/usage.html#making-gnu-linux-apps-forward-compatible)
explains why the build host matters.

Output: `CacheCAD-Linux-x86_64.tar.gz`, `archive.sha256`, build logs, and
`build-provenance.json`. Tar metadata is normalized while executable permissions
and internal library links are retained. The distribution is an unsigned,
user-owned extracted directory, with no package-manager or autostart changes.

## Test and retain an installation

Run the packaged executable, not the source launcher, under a disposable Xvfb:

```sh
xvfb-run -a -s '-screen 0 1280x900x24' .venv-linux/bin/python packaging/linux/test_app.py \
  --archive dist/linux-application/CacheCAD-Linux-x86_64.tar.gz \
  --report-dir dist/linux-application/test-results
```

The default gate requires both offscreen and native xcb rendering. It also tests
repeated settings clicks, Back navigation, missing configuration rejection,
the directory dialog's Escape flow, ordinary Close/reopen, and repeated real Exit
actions. Two actual-window screenshots and structured results are retained.
Separate synthetic faults test main-thread, worker-thread, Qt callback and fatal
native crash diagnostics. These are deliberately induced failures, clearly
labeled in the report, not crashes observed during ordinary interaction.
Three hostile startup cases reject foreign platforms, a nonempty unrelated
profile and tampered source. Bundle hashes and file modes must remain unchanged.

`--install-dir /absolute/new/directory` retains the tested extracted application;
the directory must not exist or have symlink ancestors. Without it the install
is temporary. All test profiles are disposable and removed after evidence is
copied. `--offscreen-only` is an explicit early diagnostic, marks native GUI and
interactions unverified, and must not substitute for the default release gate.
`--display` and `--xauthority` can explicitly name a disposable authenticated
local X server; inherited account, proxy, loader and Qt plugin variables are
discarded. No OS network/firewall/security setting is changed.

`linux-runtime.json` binds runtime evidence to the archive SHA-256. Startup
checks use the launcher's Python network/browser/process tripwires, no real
account, absent Desktop-client configuration and fresh state. They do not claim
OS-level network denial or test live OAuth/CAD operations. Linux sign-in and Drive
sync currently fail closed because protected Linux credential storage is not
implemented. A client configuration alone does not enable those features.
