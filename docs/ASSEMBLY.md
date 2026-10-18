# Deterministic application source assembly

Run with the pinned Python 3.12 maintenance environment:

```sh
python tooling/assemble_app.py /path/to/new-output-directory
```

The parent directory must exist; the output directory must not. Source and output
symlinks are rejected. The command never imports or launches the application.
It first runs the reviewed recovery fingerprint, entrypoint fingerprint, and
maintained hash/function-scope validators. It also checks every baseline source
hash, including the deliberately sanitized credential-bearing module.

The output contains:

- `main.py`: recovered CArchive entrypoint
- `src/app`, `src/core`, `src/gui`, `src/utils`: 49 inventoried first-party modules,
  with the reviewed maintained module overlay taking precedence; explicitly
  listed new helpers are included with `new-helper` provenance
- `assembly-manifest.json`: sorted payload records containing SHA-256, repository
  source path, recovered/maintained/sanitized origin and recovery baseline commit
- `app-source.zip`: the same source payload and manifest, sorted, uncompressed,
  with fixed ZIP timestamps and permissions

The manifest covers the 50 baseline/entrypoint source payload files plus any
explicitly reviewed new helper files. It excludes itself and its
containing ZIP to avoid circular hashes. The baseline commit identifies the
recovery baseline recorded by the patch manifest; it is not a claim that the
current maintained edits existed in that commit. File hashes identify the exact
assembled source. Identical validated inputs produce byte-identical ZIP output.

The source artifact also includes a separately reviewed offline development
launcher, runtime lock and generated neutral resources when development/manifest.json
is present. These files have their own payload hashes and offline-development origin.
See development/README.md for the real-Qt fixture workflow. This is not an installer;
production resources/account configuration and target-platform packaging remain open. The sanitized module remains non-equivalent to the original.
The earlier `cachecad` replacement package is not included. No credentials are
added, and no original executable or application code is run by assembly.

Linux CI assembles this artifact after required source gates. Regression tests
check repeated byte identity, all payload hashes/origins, sanitized-source
mutation rejection, existing-output refusal, traversal and symlink rejection.

## Actual application payload versus test launchers

`python tooling/assemble_app.py NEW_OUTPUT --production` requires the reviewed
`application/manifest.json` bootstrap, including the real launcher and neutral
resource module. It deliberately does not read or include `development/manifest.json`
or its synthetic scenario launchers. The production source tree still contains
reviewed maintained compatibility helpers used by the recovered graph; those do
not supply fake Drive results. Every emitted bootstrap byte is checked against
its declared hash and recorded in the assembly manifest.

This flag describes payload composition, not a production-readiness certification.
Owner OAuth configuration, consent verification, authorized live-account testing,
and unresolved revision-sync/CAD validation remain separate release gates.

The production payload also omits the original root `main.py`, original OAuth
callback server, credential-bearing sanitized module, vendor account-validation,
telemetry, bug-report and updater modules, and unused identity-test dialog. Their
baseline recovery remains preserved and validated in the repository. The manifest
records this explicit exclusion set; production starts only `application/launch.py`.
