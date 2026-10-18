# Repeatable, offline-first recovery

This directory contains newly written recovery tooling, not CacheCAD source. `recover.py` hardens the small standard-library extractor used during the initial static investigation (`extract_pyi_static.py`), preserving its PyInstaller CArchive layout interpretation. It parses/decompresses bytes only. It does not import, unmarshal, disassemble, or execute recovered code. The source archive must not include vendor installers, executables, recovered bytecode, credentials, or customer data.

The reviewed target is CacheCAD beta 1.2.0, September 28, 2025. Its app is a **PyInstaller-packaged Python 3.12 program**, not a .NET assembly. Extracting its Python bytecode is not recovery of the original Python source, comments, names lost to compilation, or build environment. The accompanying reconstructed application is a separately authored approximation with explicit limitations.

## Pinned provenance

- Official site: https://www.cachecad.com/download
- Official-linked release repository: https://github.com/Schreimr/CacheCAD-Releases
- Release: https://github.com/Schreimr/CacheCAD-Releases/releases/tag/1.2.0_beta_25-09-28
- Installer URL: https://github.com/Schreimr/CacheCAD-Releases/releases/download/1.2.0_beta_25-09-28/CacheCAD_Setup.exe
- Installer size: 72,577,635 bytes
- Installer SHA-256: `3d9da1785b1ba3288a60cf0771f7b0189037957947e25348f4fe2bf783d431bd`
- Extracted `app/CacheCAD.exe` size: 71,287,361 bytes
- App SHA-256: `38ef57dd6dacd33c2e831bd380ca2a07756510248debc32d1722a73df8779ce6`
- Terms: https://www.cachecad.com/ToS (personal/educational/nonprofit use; commercial use requires consent)
- Original static tooling: innoextract 1.13 from official Debian packages, Python standard library, REA 6.7.0 from npm. Do not substitute similarly named repositories.

## Download → verify → extract → inventory

Run from this source repository root. Supply an empty work directory outside the distributable source tree and a trusted, previously installed `innoextract` 1.13 executable. Obtain tools from official distributions; these commands never launch CacheCAD. A download is optional if the pinned installer is already available locally. Read applicable vendor terms first.

```sh
WORK=/absolute/path/to/new/private-analysis
mkdir "$WORK"
INSTALLER="$WORK/CacheCAD_Setup.exe"
curl --fail --location --proto '=https' --proto-redir '=https' \
  'https://github.com/Schreimr/CacheCAD-Releases/releases/download/1.2.0_beta_25-09-28/CacheCAD_Setup.exe' \
  --output "$INSTALLER"
python tools/recovery/recover.py verify-installer "$INSTALLER"
innoextract --list "$INSTALLER"
innoextract --extract --output-dir "$WORK/installer" "$INSTALLER"
python tools/recovery/recover.py extract-pyinstaller \
  "$WORK/installer/app/CacheCAD.exe" "$WORK/pyinstaller"
```

Review the installer listing first; the pinned installer contains `app/CacheCAD.exe`. The outer Inno extraction relies on the trusted `innoextract` tool, and is not implemented by this Python parser. Stop on any failure or mismatched digest. Never run the installer to unpack it. The Python extraction creates a new output directory and refuses existing directories or symlink roots. Use a trusted work parent that other users cannot modify; this is not a defense against a concurrent attacker rewriting parent directories.

`manifest.json` records source digest and size, Python version/library, member type, offset, compressed/uncompressed size and SHA-256. It has sorted members/keys, no wall-clock timestamps or absolute source paths, and is byte-identical for the same bytes across output locations. Files are ordinary data, with no executable permission promotion. The parser rejects traversal/absolute/Windows-special paths, collisions, links/unknown types, malformed bounds, malformed compression, and excessive sizes. It validates all entries before writing. Filesystem/disk failures can leave partial output: delete that private work directory and rerun with a new destination. The manifest is written last; its absence means extraction did not complete.

For a deliberately selected different build, obtain and independently verify its digest before using `--expected-sha256 HASH`. That override does not establish provenance. Do not call a new build the reviewed version, and do not silently change the pinned defaults.

## REA 6.7.0: static PE resources

REA is https://github.com/morluto/rea, official npm package `rea-agents`. Use an existing pinned CLI; its supported Node versions are 22.x >=22.19, 24.x >=24.11, or 26+. No MCP registration, `setup`, Hopper, Ghidra, or target execution is needed for this command:

```sh
/absolute/path/to/rea-tool/node_modules/.bin/rea inspect-pe-resources \
  "$WORK/installer/app/CacheCAD.exe" --json > "$WORK/rea-pe-resources.json"
```

If installation is separately authorized, a workspace-local official-registry install can use `npm install --prefix /absolute/path/to/rea-tool --ignore-scripts --no-audit --no-fund rea-agents@6.7.0`. This disables lifecycle scripts; do not enable them reflexively if another feature fails. The PE resource result is corroborating metadata, not Python source reconstruction or runtime validation. REA managed-code inspection is inappropriate for this confirmed Python payload.

## Validation

```sh
python -m unittest discover -s tools/recovery/tests -v
```

Synthetic fixtures are built entirely in memory. Windows-invalid characters and reserved device aliases (including superscript COM/LPT digits) are explicitly rejected. Tests cover deterministic round trips, path traversal/Windows aliases, collision/link rejection, bad digest, existing/symlink output roots, malformed bounds and decompression budgets. The actual pinned app was additionally parsed without executing it: 1,428 members, Python 312. Vendor bytes are not test fixtures and are not needed by CI.
