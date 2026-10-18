"""Portable manifest validation independent of private binary evidence."""
import re
from pathlib import PurePosixPath

HASH = re.compile(r"[0-9a-f]{64}\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def path_name(value):
    require(isinstance(value, str) and value, "Missing relative path")
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts and "\\" not in value
            and ":" not in value and str(path) == value, "Unsafe/noncanonical path")
    return value


def validate(root, evidence, reconstruction):
    require(evidence.get("schema_version") == 1, "Unknown evidence schema")
    require(evidence.get("category") == "static-evidence", "Invalid evidence category")
    entries = evidence.get("files")
    require(isinstance(entries, list) and entries, "Empty evidence manifest")
    indexed = {}
    for entry in entries:
        name = path_name(entry["path"])
        require(name not in indexed, "Duplicate evidence path")
        require(type(entry["bytes"]) is int and entry["bytes"] >= 0, "Invalid byte count")
        require(isinstance(entry["sha256"], str) and HASH.fullmatch(entry["sha256"]), "Invalid hash")
        indexed[name] = entry["sha256"]
    require(reconstruction.get("schema_version") == 1, "Unknown reconstruction schema")
    require(reconstruction.get("original_modules_executed") is False, "Unexpected execution claim")
    require(reconstruction.get("full_fidelity_modules_verified") == 0, "Unsupported fidelity claim")
    for field in ["installer_sha256", "application_sha256"]:
        require(isinstance(reconstruction.get(field), str) and HASH.fullmatch(reconstruction[field]),
                "Invalid acquisition hash")
    modules = reconstruction.get("entries")
    require(isinstance(modules, list) and modules, "Empty module manifest")
    seen = set()
    for entry in modules:
        name = entry["original_module"]
        require(isinstance(name, str) and re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+", name),
                "Invalid module name")
        require(name not in seen, "Duplicate module")
        seen.add(name)
        require(entry["status"] in {"partial_reconstruction", "disassembled_not_reconstructed", "headless_replacement", "offline_adapter_only", "sandbox_adapter_only"},
                "Unsupported reconstruction status")
        require(entry["equivalence_verified"] is False, "Unsupported equivalence claim")
        static = entry["static_evidence"]
        evidence_path = path_name(static["relative_to_evidence_root"])
        require(evidence_path == f"bytecode/{name}.dis.txt", "Module evidence mismatch")
        require(evidence_path in indexed and indexed[evidence_path] == static["sha256"],
                "Evidence hash disagrees across manifests")
        sources = entry["reconstructed_sources"]
        require(isinstance(sources, list), "Invalid source list")
        require(bool(sources) == (entry["status"] != "disassembled_not_reconstructed"), "Status/source mismatch")
        for source in sources:
            path_name(source)
            path = root / source
            require(source.startswith("src/cachecad/") and source.endswith(".py"), "Invalid source path")
            require(path.is_file() and not path.is_symlink(), "Missing or symlinked source")
            require(path.resolve().is_relative_to(root.resolve()), "Source escaped project root")
    return len(indexed), len(seen)
