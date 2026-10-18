# CacheCAD source recovery and maintenance

The `recovered/` directory contains source reconstructed from the original
CacheCAD 1.2.0 beta (2025-09-28) Python 3.12 code objects. Each claimed exact
module is compiled and compared recursively against the pinned original binary's
code, constants, names, signatures, flags and exception tables. Original source
locations, formatting and comments are not recovered. The reviewed
[recovery manifest](recovered/recovery-manifest.json) is the authoritative status
for all 49 first-party PYZ modules; the separate CArchive entrypoint is tracked
under `recovered/entrypoint/`.

Embedded credentials are intentionally removed from one recovered module. That
module is explicitly non-matching. No original credentials are included or used.
Original behavior and bugs remain in the recovery baseline.
The separate [`maintained/` patch layer](maintained/) contains narrowly reviewed
corrections with runtime regression tests against actual recovered functions. Matching compiled
structure is evidence of faithful recovery, not runtime validation or a claim
that the app is fixed.

The earlier `src/cachecad/` code is a new, evidence-informed offline replacement
prototype with mock services and safety tests. Its demo and packaged wheel are
**not the recovered original application**. It remains here as experimental
improvement work; its experimental fixes have not been merged into the recovered baseline.
The maintained layer patches original functions directly and records its intentional
bytecode differences separately.

## Assemble the maintained application source

```sh
python tooling/assemble_app.py /path/to/new-output-directory
```

This combines the immutable recovery with reviewed maintenance overlays and
new helpers, producing a hash-manifested application source tree and byte-reproducible
ZIP. See [assembly instructions](docs/ASSEMBLY.md) and [maintained changes and limits](maintained/README.md).
It includes scan failure/retry isolation, OneDrive path warnings and bounded
new-file transfer protection. Normal revision sync still has unresolved overwrite
races; this source artifact is not an executable installer.

## Run the actual application offline

The [offline development launcher](development/README.md) runs the actual assembled
GUI with real Qt and disposable synthetic scan fixtures. A separate hash-locked
Linux runtime and generated neutral placeholder assets are included. Authentication,
live services and transfers are disabled in this development mode. Hosted CI tests
startup, failure/retry/stale-result isolation and closing a pending scan.

## Windows offline-development installer

The Windows pipeline builds a self-contained per-user installer from the same
verified application source. It installs and exercises the installed executable
on a standard Windows 2022 runner before publishing a private CI artifact.
See [Windows tryout instructions](docs/WINDOWS_TRYOUT.md) for the exact artifact,
unsigned-installer warning, test scope and build prerequisites. This development
installer uses placeholder artwork and disables login and real transfers.

## Maintenance checks

Use Python 3.12 and the hash-locked Linux tools:

```sh
python -m pip install --require-hashes -r requirements/linux-build.lock
python tooling/check.py --require-recovery-manifest --require-maintained-manifest --ruff
```

This runs source-only compiled-fingerprint checks, comparator regression tests,
synthetic prototype tests, extraction-tool tests and lint. It never imports recovered application modules. Selected inspected function bodies
are executed only by isolated mocked regression tests, without top-level
initialization, real credentials, network services or user project files. Reports and source hashes are bound to
the reviewed recovery manifest; reducing the verified module set fails the gate.

For independent original-byte revalidation, privately extract the pinned release
as documented in [recovery tooling](tools/recovery/), then run
`recovered/compare_bytecode.py` against its PYZ. Do not publish the installer,
extracted binaries, original bytecode, disassemblies or credential-bearing data.
The fingerprint gate trusts the reviewed manifest; changing both source and its
expected fingerprint is not independent evidence.

The earlier offline demo is available with:

```sh
PYTHONPATH=src python -m cachecad --demo --json
```

It uses disposable fixtures and fake services. Do not use live projects or
credentials for development tests. No executable installer has been rebuilt.
The offline runtime lock and neutral Qt resources now support the actual
development launcher. Production resources, Windows/SolidWorks validation,
installer packaging, signing and live Google Drive integration remain open.

See [development instructions](docs/DEVELOPMENT.md) and
[the recovery baseline notes](recovered/README.md).
