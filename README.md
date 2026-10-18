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
new-file transfer protection. Existing overwrite/delete selections now require
[explicit destructive consent and observed-version checks](docs/DESTRUCTIVE_SYNC_SAFETY.md).
Revision sync remains available, with a documented residual check/write race.
[Durable transfer recovery](docs/TRANSFER_RECOVERY.md) retains local evidence, distinguishes
unknown commits, and prevents blind retries of unresolved writes.
[Local crash reports](docs/CRASH_DIAGNOSTICS.md) record sanitized diagnostics without uploads.
[Crash hardening evidence](docs/CRASH_HARDENING_AUDIT.md) separates reproduced defects
from unconfirmed causes of reported crashes. This source artifact is not an installer.

## Run the actual application offline

The [offline development launcher](development/README.md) runs the actual assembled
GUI with real Qt and disposable synthetic scan fixtures. A separate hash-locked
Linux runtime and generated neutral placeholder assets are included. Authentication,
live services and transfers are disabled in this development mode. Hosted CI tests
startup, failure/retry/stale-result isolation and closing a pending scan.

## Windows application and Google sign-in

The existing verified Windows artifact is an **offline-development build** with
placeholder artwork. The maintained production application path is being wired to
real Google Desktop OAuth, Windows-protected persistent credentials, refresh and
the actual Drive client. End users will sign in using the application owner's
client configuration; they will not create their own OAuth clients.

See [production Google setup](docs/PRODUCTION_GOOGLE_SETUP.md) for owner setup,
consent/scope requirements and the remaining release gates. A successful signed-out
installer startup test does not validate a live Google grant, real sync, or CAD
compatibility. The separate identity-only development launcher is retained for
tests; it is not the production authentication architecture.

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
credentials for development tests. The actual application installer is a separate
artifact from this earlier demo. Windows CI has verified building, installing,
starting the installed application with the native Windows Qt backend, and
uninstalling the offline development build. Production artwork, signing,
SolidWorks validation and live Google Drive integration remain open.

See [development instructions](docs/DEVELOPMENT.md) and
[the recovery baseline notes](recovered/README.md).
