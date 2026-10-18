"""Offline SHA-256 inventories. Reads bytes only; never imports recovered code."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SCHEMA = 1
CATEGORIES = {"static-evidence", "handwritten-reconstruction"}
SKIP = {"__pycache__", ".git", ".venv", "build", "dist", ".ruff_cache"}


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts or not raw.parts:
        raise ValueError(f"Unsafe manifest path: {relative!r}")
    path = root / raw
    if any(part.is_symlink() for part in [path, *path.parents] if part != root.parent):
        raise ValueError(f"Symlinks are not accepted: {relative!r}")
    path.resolve().relative_to(root.resolve())
    return path


def create(root: Path, paths: list[str], category: str, output: Path) -> dict:
    if category not in CATEGORIES:
        raise ValueError("Unknown provenance category")
    files = set()
    for relative in paths:
        path = safe_path(root, relative)
        if not path.exists():
            raise ValueError(f"Missing input: {relative}")
        candidates = path.rglob("*") if path.is_dir() else [path]
        for candidate in candidates:
            rel = candidate.relative_to(root)
            if any(part in SKIP for part in rel.parts):
                continue
            safe_path(root, rel.as_posix())
            if candidate.is_file() and candidate.resolve() != output.resolve():
                files.add(rel.as_posix())
    if not files:
        raise ValueError("Refusing empty inventory")
    return {
        "schema_version": SCHEMA,
        "category": category,
        "claim": "Byte identity only; does not establish authorship or behavioral equivalence.",
        "files": [
            {"path": name, "bytes": (root / name).stat().st_size,
             "sha256": digest(root / name)}
            for name in sorted(files)
        ],
    }


def verify(root: Path, manifest: dict) -> list[str]:
    if manifest.get("schema_version") != SCHEMA or manifest.get("category") not in CATEGORIES:
        raise ValueError("Unsupported manifest schema/category")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Manifest must contain files")
    errors = []
    seen = set()
    for entry in entries:
        name = entry["path"]
        if name in seen:
            raise ValueError(f"Duplicate manifest path: {name}")
        seen.add(name)
        path = safe_path(root, name)
        if not path.is_file():
            errors.append(f"Missing: {name}")
        elif path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]:
            errors.append(f"Changed: {name}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["create", "verify"]:
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--manifest", type=Path, required=True)
        if name == "create":
            command.add_argument("--category", choices=sorted(CATEGORIES), required=True)
            command.add_argument("paths", nargs="+")
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        if args.command == "create":
            payload = create(root, args.paths, args.category, args.manifest)
            args.manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            print(f"Recorded {len(payload['files'])} files: {args.manifest}")
        else:
            payload = json.loads(args.manifest.read_text(encoding="utf-8"))
            errors = verify(root, payload)
            if errors:
                print("\n".join(errors))
                return 1
            print(f"Verified {len(payload['files'])} files ({payload['category']})")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Integrity error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
