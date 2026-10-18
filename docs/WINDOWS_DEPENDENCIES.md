# Windows dependency locks

## Verified dependency result

On 2026-10-18, the Windows x86_64 / CPython 3.12 dependency set was resolved
from official PyPI and downloaded as wheels:

| Lock | Distributions | Downloaded bytes |
| --- | ---: | ---: |
| `requirements/runtime-windows.lock` | 40 | 257,097,937 (245.2 MiB) |
| `requirements/windows-build.lock` | 13 | 13,275,050 (12.7 MiB) |

Every wheel's local SHA-256 and byte count was checked against both the
resolver's registry hashes and the matching filename in official PyPI's
`https://pypi.org/pypi/{distribution}/{version}/json` metadata. No selected
artifact was yanked. Wheel filenames, distribution metadata, CPython 3.12
compatibility tags and `Requires-Python` were also checked. Both final locks
successfully replayed together with pip's offline, hash-required, wheel-only
installation **dry run**, targeting `win_amd64`.

A separate metadata walk checked the complete dependency closure using a
Windows marker environment (`os_name=nt`, `sys_platform=win32`,
`platform_system=Windows`, `platform_machine=AMD64`, Python 3.12.14), including
transitive extras such as `google-api-core[grpc]`. This check is important:
cross-platform wheel selection alone must not be taken as proof that every
Windows-specific environment marker was evaluated correctly.

These checks did not execute Windows code, install the Windows wheels on
Linux, import the recovered application, run a vendor executable, read real
account credentials, or access user projects. They establish artifact
availability, integrity and declared dependency compatibility. They do not
establish successful Windows installation, Qt startup, freezing, or app
behavior. The Windows workflow supplies those separate execution checks;
see [Windows tryout](WINDOWS_TRYOUT.md) for their actual status and scope.

## Runtime choices

`runtime-windows.in` preserves all 14 observed version pins documented in
[RUNTIME_DEPENDENCIES.md](RUNTIME_DEPENDENCIES.md) and
[RUNTIME_BUILD_PLAN.md](RUNTIME_BUILD_PLAN.md), including PySide6, its matching
Addons/Essentials/shiboken packages at **6.9.2**, Google client libraries, and
protobuf. All 39 distribution versions from the existing Linux runtime lock
were retained. Their artifact hashes are replaced where Windows needs different
wheels; Linux wheel hashes are not reused for Windows native extensions.

The sole additional runtime distribution is **tzdata 2026.5**, required by
`tzlocal` on Windows. It is a newly selected compatible dependency, not
original-bundle evidence. The Linux runtime lock remains unchanged.

The Google libraries are installed because the recovered imports need them.
Their presence does not enable authentication or permit live operations in the
offline development application. Windows does not change that safety boundary.

The Qt wheels use `cp39-abi3-win_amd64` tags, compatible with the CPython 3.12
x64 target. Native Windows wheels are also selected for `charset-normalizer`,
`grpcio`, and `protobuf`. The full official PySide6 distribution is retained;
unused Addons are not evidence that all those modules are frozen into the app.

## Packaging choices

`windows-build.in` retains the maintenance pins in `dev-pins.txt` and shared
`packaging`/`pyproject-hooks` versions. It selects **PyInstaller 6.22.3** with
**pyinstaller-hooks-contrib 2026.8**, verified as the current stable releases
from official PyPI on 2026-10-18. These are new packaging-tool choices, not
claims about the original installer.

This supersedes the old unvalidated PyInstaller 6.11.1 template. Upstream's
[installation guidance](https://pyinstaller.org/en/stable/installation.html)
recommends keeping PyInstaller and hooks-contrib releases reasonably close
and pinning both. PyInstaller 6.22.3 requires hooks-contrib >=2026.7 and supports
Python >=3.8,<3.16 in its wheel metadata. See the
[6.22.3 release notes](https://pyinstaller.org/en/v6.22.3/CHANGES.html).

The complete build-tool closure contains:

- altgraph 0.17.5
- build 1.2.2.post1
- colorama 0.4.6
- packaging 26.3
- pefile 2023.2.7
- pyinstaller 6.22.3
- pyinstaller-hooks-contrib 2026.8
- pyproject-hooks 1.3.3
- pywin32-ctypes 0.2.3
- PyYAML 6.0.3
- ruff 0.8.2
- setuptools 75.6.0
- wheel 0.45.1

Windows-conditioned build requirements include `pefile`, `pywin32-ctypes`,
and `colorama`. The deprecated `windows-build-pins.txt` input now delegates to
`windows-build.in`; installations should use the complete `.lock`, not either
input file. Build tools and runtime dependencies remain separately locked.

## Install on Windows

Use a clean CPython 3.12 x64 virtual environment. The workflow pins its Python
patch separately. From the repository root in PowerShell:

```powershell
py -3.12 -m venv .venv-windows
.venv-windows\Scripts\python.exe -m pip --isolated --disable-pip-version-check --no-input install --index-url https://pypi.org/simple --only-binary=:all: --require-hashes -r requirements/runtime-windows.lock -r requirements/windows-build.lock
.venv-windows\Scripts\python.exe -m pip --isolated --disable-pip-version-check check
```

Do not add `--no-deps`, alternate indexes, `--trusted-host`, or unpinned
packages. Native installation must keep dependency checking enabled; it can
catch additional platform-conditioned requirements or registry incompatibility.
Do not call a Windows install or `pip check` successful merely because the
cross-platform dry run above succeeded.

To keep a wheelhouse for offline rebuilding, first download on Windows:

```powershell
.venv-windows\Scripts\python.exe -m pip --isolated --disable-pip-version-check --no-input download --no-cache-dir --index-url https://pypi.org/simple --only-binary=:all: --require-hashes --dest wheelhouse/windows -r requirements/runtime-windows.lock -r requirements/windows-build.lock
```

Then install into a separate clean Windows venv with `--no-index
--find-links wheelhouse/windows`, retaining `--only-binary=:all:` and
`--require-hashes`. The wheelhouse is a local build artifact, not part of the
source repository or the end-user installer upload.

## Lock derivation and limits

Resolution used uv 0.12.23, CPython 3.12, target
`x86_64-pc-windows-msvc`, only `https://pypi.org/simple`, no repository uv
configuration or source overrides, wheel-only resolution, no prereleases, and
an upload cutoff of **2026-10-18T18:50:00Z**. The runtime candidate was
constrained to the existing Linux lock's exact package versions, without its
platform-specific hashes. The packaging candidate preserved the selected
shared maintenance versions and pins listed above.

The final selection used pip 26.2.1 with `--platform win_amd64
--python-version 312 --implementation cp --abi cp312 --abi abi3 --abi none`,
`--only-binary=:all:` and `--require-hashes`. The final locks retain exactly one
verified wheel hash per distribution; comments preserve its filename and byte
count. No source distributions or arbitrary upstream build scripts were used.
The wheel sets exactly match the resolved distribution/version sets, with no
duplicate distributions. A fresh resolution is an intentional dependency
update, not a required step for using the existing locks.

These files do not lock the Python interpreter, pip bootstrap, uv resolver,
Windows runner image, system DLLs, installer compiler, application source,
code-signing identity, or installer bytes. The Windows build workflow and
provenance report must account for the other build inputs. Dependency hash
verification is not a vulnerability audit, license approval, reproducible
installer guarantee, or SolidWorks compatibility test.

[PyInstaller is not a cross-compiler](https://pyinstaller.org/en/stable/index.html):
Windows freezing, installed-application startup and uninstall checks must run
on Windows. Nothing in this dependency preparation substitutes for those tests.
