# Linux package validation

Validated on 2026-10-18 with CPython 3.12.14, Linux x86_64.

- Build/lint dependencies resolved from official PyPI, wheel hashes recorded in
  requirements/linux-build.lock, then reinstalled offline with --require-hashes
  into a separate isolated build environment.
- sdist and pure-Python wheel built with pinned setuptools/wheel via
  python -m build --no-isolation. Build derives the wheel from the sdist.
- Fresh temporary venv installs wheel with --no-index --no-deps.
- Import provenance checked against site-packages, offline demo succeeds, and
  tests run from copies outside the source tree.
- 96 main tests and 8 static-recovery tests pass; 53 static evidence hashes
  verify; optional Ruff correctness rules pass.
- Wheel includes all four cachecad/safety Python modules.

This validates this Linux fixture-based package pipeline. It does not establish
original-app parity, live cloud sync, Windows packaging, SolidWorks integration,
or production readiness. No code or artifacts were published externally.
