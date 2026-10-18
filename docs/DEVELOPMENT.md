# Development, provenance, and regression gates

## What is reproducible today

Run from the project root with Python 3.12:

```sh
python tooling/check.py --evidence-root ..
```

This checks SHA-256 hashes against the separate static evidence directory, parses
handwritten Python sources, and runs standard-library unittest suites in `tests/`
and `tools/recovery/tests/` when present. Tests must use temporary directories and
fake services. The evidence checker only reads bytes. Tests run replacement code,
not extracted application code. The syntax pass is not a full linter/type checker.

Optional lint: install `requirements/dev-pins.txt` into an isolated environment,
then run `python tooling/check.py --ruff`. The Linux build/lint toolchain has been installed and lint checked; Windows
packaging is still unverified.

## Integrity and honest provenance

`evidence-manifest.json` inventories the static disassemblies, code map, recovered
interface inventory, acquisition provenance, and observed dependency inventory.
Paths are relative to the analysis directory (`..`), not this project. It excludes
vendor executable payloads; their acquisition hashes are in the original
`provenance.json`. A trusted, reviewed manifest is the anchor: changing both a file
and its expected hash can defeat integrity detection. Hashes prove byte identity,
not correctness, authorship, safety, or behavioral equivalence. The verifier
checks listed files; it does not reject unlisted additions.

Verify without running tests:

```sh
python tooling/integrity.py verify --root .. --manifest docs/evidence-manifest.json
```

After intentional reviewed evidence changes, regenerate explicitly and review the
diff. Never regenerate in CI merely to make a failed hash check pass:

```sh
python tooling/integrity.py create --root .. --manifest docs/evidence-manifest.json --category static-evidence bytecode code-map.json recovered-interface-inventory.json provenance.json prototype/dependency-inventory.json
```

To snapshot handwritten work separately after tests pass:

```sh
python tooling/integrity.py create --root . --manifest source-snapshot.json --category handwritten-reconstruction src tests tooling tools/recovery pyproject.toml README.md
python tooling/integrity.py verify --root . --manifest source-snapshot.json
```

Keep recovered interface evidence separate from inferred behavior and safety
changes. For each implementation, record evidence module/function, known gaps,
behavioral assumptions, and test references. Do not mark a module recovered just
because a stub with matching names exists. Re-extraction instructions and hardened
static tooling belong in `tools/recovery/`; never import or execute vendor bytecode.

## Dependencies and locking

Core runtime/tests have no third-party dependencies. `requires-python` fixes the
supported minor line to 3.12. The build backend and optional developer tools have
exact direct version pins selected for this project; these are not evidence of the
original application's versions. They are NOT a complete transitive hash lock.

Before activating packaging, resolve the full dependency graph on the intended
Windows/Python host from the official package registry. Record every transitive
version and artifact SHA-256 in a platform-specific lock; install with pip
`--require-hashes` from a reviewed wheelhouse. Preserve the wheelhouse manifest,
Python patch version, OS image, build tool versions, and lock with the build log.
Do not claim offline rebuilds or byte-for-byte reproducibility until independently
repeated and compared. Add optional UI/cloud dependencies only when implemented
and tested; do not install the binary's entire import inventory speculatively.

## Windows development packaging and production gates

The active `windows-installer` job in `.github/workflows/linux-ci.yml` builds the
actual recovered-plus-maintained app using the Windows runtime/build hash locks.
It installs the per-user offline-development installer, runs installed-executable
smoke scenarios, and uninstalls before publishing a private CI artifact. The old
prototype-only template is superseded. See [Windows tryout](WINDOWS_TRYOUT.md).

This does not certify production sync or SolidWorks behavior. Before production
use: test rollback/conflicts and CAD integration with disposable data, review
live authentication/cloud adapters, and obtain authorization for signing or real
accounts. No telemetry, live credentials or production deployment is configured.
The installer is unsigned; OS/runner dependencies are not a fully frozen image.

## Linux package build and clean-install verification

`requirements/linux-build.lock` pins the full resolved build/lint dependency
closure and downloaded wheel SHA-256 values for Linux x86_64 / Python 3.12.
It does not lock Python, pip bootstrap, OS libraries, or Windows artifacts.
The original direct-pin files remain inputs, not portable complete locks.

```sh
python -m venv .venv
.venv/bin/python -m pip download --only-binary=:all: --require-hashes -r requirements/linux-build.lock -d wheelhouse
.venv/bin/python -m pip install --no-index --find-links wheelhouse --require-hashes -r requirements/linux-build.lock
.venv/bin/python tooling/check.py --evidence-root .. --ruff
SOURCE_DATE_EPOCH=1792324800 .venv/bin/python -m build --no-isolation
python tooling/validate_wheel.py dist/cachecad_reconstructed-0.1.0-py3-none-any.whl
```

The validator creates a clean temporary venv, installs the wheel with
`--no-index --no-deps`, checks imports resolve to site-packages, runs the demo,
and copies tests/tooling into a temporary directory without `src` before running
them. Thus application imports resolve to the installed package. It never starts
the original application. Build artifacts and logs are local only. A fixed epoch
normalizes archive timestamps; this alone does not establish bit reproducibility.

## Hosted Linux CI

`.github/workflows/linux-ci.yml` runs source-only checks, lint, package build and
clean-wheel validation on Python 3.12.14/Linux. Official checkout v4.2.2 and
setup-python v5.6.0 tag references were resolved with `git ls-remote` and pinned
to full commit hashes. Checkout credentials are not persisted; job permissions
are read-only. No original binaries, private evidence, secrets, cloud accounts,
deployment or artifact publishing are required.

Every gate invocation validates both manifest schemas, cross-checks evidence
hash references, and checks that named reconstructed sources exist inside the
package. Without `--evidence-root`, this is structural provenance validation,
not verification of private evidence bytes. The optional evidence-root flag
still performs the full 53-file hash verification and fails on missing files.

## Faithful recovery gate (separate from replacement tests)

The complete maintenance gate requires hash-locked PyYAML and parses the actual
Linux workflow before testing. This closes the previous gap where plain YAML
scalars containing colon-space could pass Python tests but fail GitHub parsing.
Application runtime code still has no added PyYAML dependency.

`tooling/check.py` also runs synthetic tests from `recovered/test_*.py` and, when
`recovered/recovery-manifest.json` is present, invokes `recovered/check_reports.py`.
The latter compiles recovered text and verifies source/report hashes and original
structural fingerprints. It does not import or execute recovered modules.
Use `--require-recovery-manifest` for the finalized recovery publication so an
absent reviewed manifest fails rather than skipping this gate. A stale manifest
fails; refresh only after independent evidence-backed review.

Ruff targets only replacement sources and maintenance tools. Recovered original
source is deliberately excluded: stylistic or bug fixes would invalidate faithful
compiler-structure matching. Full evidence comparison still requires the pinned
original PYZ locally; CI validates the reviewed structural reference hashes.
