# CacheCAD reconstruction maintenance workspace

This is a handwritten, evidence-informed replacement scaffold. It is not the
original author's recovered source, a verified behavioral clone, or a release of
CacheCAD. The static analysis inventory contains 49 first-party modules; an
inventoried/disassembled module is not necessarily implemented here. Consult the
reconstruction status/provenance documents for the implemented subset.

## Offline development

Python 3.12 is required. Core checks use the standard library only:

```sh
python tooling/check.py --evidence-root ..
PYTHONPATH=src python -m cachecad --demo --json
```

On Windows PowerShell, set `$env:PYTHONPATH = "src"` before invoking the demo.
The demo uses disposable local data. Do not point development tests at live
projects, credentials, or cloud accounts. No original executable or recovered
bytecode is loaded or run by the maintenance gate.

Use `python tooling/check.py` when the separate static evidence directory is
unavailable; it explicitly reports that evidence verification was skipped.
A source-only test pass does not establish provenance or original-app parity.

See [development and provenance instructions](docs/DEVELOPMENT.md),
[recovery tooling](tools/recovery/), and the inert
[Windows packaging template](templates/windows-build.yml).

Windows/SolidWorks validation, installer construction, signing, live cloud sync,
and publication are deferred. Nothing in the pipeline pushes or publishes code.
