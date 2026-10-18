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
    parser.add_argument("--require-recovery-manifest", action="store_true",
                        help="Fail if the reviewed recovery manifest is absent")
    parser.add_argument("--require-maintained-manifest", action="store_true",
                        help="Fail if the reviewed maintained patch manifest is absent")
    args = parser.parse_args()
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
               PYTHONDONTWRITEBYTECODE="1")
    if sys.version_info[:2] != (3, 12):
        print("Gate requires Python 3.12", file=sys.stderr)
        return 2
    try:
        import yaml
    except ImportError:
        print("Workflow validation requires hash-locked PyYAML; install requirements/linux-build.lock", file=sys.stderr)
        return 2
    workflow = yaml.safe_load((ROOT / ".github/workflows/linux-ci.yml").read_text(encoding="utf-8"))
    if not isinstance(workflow, dict) or "on" not in workflow or "jobs" not in workflow:
        print("Invalid workflow structure", file=sys.stderr)
        return 1
    print("Workflow YAML parsed successfully", flush=True)
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
    for directory in ["src", "tests", "tooling", "tools/recovery", "recovered", "maintained", "development", "application", "packaging/windows"]:
        for path in sorted((ROOT / directory).rglob("*.py")):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            sources.append(path)
    print(f"Syntax checked: {len(sources)} handwritten Python files", flush=True)
    for directory in ["tests", "tools/recovery/tests", "recovered"]:
        if not (ROOT / directory).is_dir():
            continue
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", directory, "-v"],
                                cwd=ROOT, env=env, check=False)
        if result.returncode:
            return result.returncode
    recovery_manifest = ROOT / "recovered/recovery-manifest.json"
    if recovery_manifest.is_file():
        result = subprocess.run([sys.executable, "recovered/check_reports.py"], cwd=ROOT,
                                env=env, check=False)
        if result.returncode:
            return result.returncode
        result = subprocess.run([sys.executable, "recovered/check_entrypoint.py"], cwd=ROOT,
                                env=env, check=False)
        if result.returncode:
            return result.returncode
    elif args.require_recovery_manifest:
        print("Required recovery manifest is absent", file=sys.stderr)
        return 1
    else:
        print("Recovery fingerprint gate NOT RUN: reviewed manifest not present", flush=True)
    maintained_manifest = ROOT / "maintained/patch-manifest.json"
    if maintained_manifest.is_file():
        result = subprocess.run([sys.executable, "tooling/maintained.py"], cwd=ROOT,
                                env=env, check=False)
        if result.returncode:
            return result.returncode
    elif args.require_maintained_manifest:
        print("Required maintained patch manifest is absent", file=sys.stderr)
        return 1
    else:
        print("Maintained scope gate NOT RUN: reviewed manifest not present", flush=True)
    if args.ruff:
        result = subprocess.run([sys.executable, "-m", "ruff", "check", "src", "tests", "tooling", "tools/recovery", "development", "application", "packaging/windows"], cwd=ROOT,
                                env=env, check=False)
        if result.returncode:
            return result.returncode
    print("Requested source/offline gates passed. Target-platform installer validation is a separate CI job; live integrations remain unverified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
