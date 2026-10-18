"""Portable, offline gates for handwritten sources. No original bytecode execution."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

from integrity import verify
from manifests import validate

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, help="Directory containing static analysis files")
    parser.add_argument("--ruff", action="store_true", help="Also run installed, pinned optional linter")
    args = parser.parse_args()
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
               PYTHONDONTWRITEBYTECODE="1")
    if sys.version_info[:2] != (3, 12):
        print("Gate requires Python 3.12", file=sys.stderr)
        return 2
    manifest = json.loads((ROOT / "docs/evidence-manifest.json").read_text(encoding="utf-8"))
    reconstruction = json.loads((ROOT / "reconstruction-manifest.json").read_text(encoding="utf-8"))
    evidence_count, module_count = validate(ROOT, manifest, reconstruction)
    print(f"Portable manifests valid: {evidence_count} evidence records, {module_count} modules", flush=True)
    if args.evidence_root:
        errors = verify(args.evidence_root.resolve(), manifest)
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print(f"Evidence hashes verified: {len(manifest['files'])}", flush=True)
    else:
        print("Evidence check NOT RUN; supply --evidence-root for provenance gate.", flush=True)
    sources = []
    for directory in ["src", "tests", "tooling", "tools/recovery"]:
        for path in sorted((ROOT / directory).rglob("*.py")):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            sources.append(path)
    print(f"Syntax checked: {len(sources)} handwritten Python files", flush=True)
    for directory in ["tests", "tools/recovery/tests"]:
        if not (ROOT / directory).is_dir():
            continue
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", directory, "-v"],
                                cwd=ROOT, env=env, check=False)
        if result.returncode:
            return result.returncode
    if args.ruff:
        result = subprocess.run([sys.executable, "-m", "ruff", "check", "."], cwd=ROOT,
                                env=env, check=False)
        if result.returncode:
            return result.returncode
    print("Requested offline gates passed. Windows packaging and live integrations are unverified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
