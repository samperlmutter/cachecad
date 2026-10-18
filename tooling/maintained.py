"""Validate immutable baseline, reviewed patch hashes and explicit AST exceptions."""
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def masked_ast(path, allowed, *, changed_signatures=(), added_nodes=(), added_imports=(), removed_imports=(), candidate=False):
    """Mask only reviewed edits; require each exception exactly once on its side."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    seen, removed, imports, deleted_imports = set(), set(), set(), set()
    allowed, signatures, additions = set(allowed), set(changed_signatures), set(added_nodes)
    if not signatures <= allowed:
        raise ValueError('Signature exception must also name a changed function')
    if allowed & additions:
        raise ValueError('Added node cannot also be a changed function')
    expected_imports = {ast.dump(ast.parse(text).body[0], include_attributes=False)
                        for text in added_imports}
    if len(expected_imports) != len(added_imports):
        raise ValueError('Duplicate import exception')
    expected_removed = {ast.dump(ast.parse(text).body[0], include_attributes=False)
                        for text in removed_imports}
    if len(expected_removed) != len(removed_imports) or expected_removed & expected_imports:
        raise ValueError('Duplicate or contradictory removed import exception')
    for text in (*added_imports, *removed_imports):
        nodes = ast.parse(text).body
        if len(nodes) != 1 or not isinstance(nodes[0], (ast.Import, ast.ImportFrom)):
            raise ValueError('Import exception must be exactly one import')

    def visit(body, prefix=''):
        result = []
        for node in body:
            name = None
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = prefix + node.targets[0].id
            if name in additions:
                if not candidate or name in removed:
                    raise ValueError('Added node already exists or is duplicated')
                if not isinstance(node, (ast.FunctionDef, ast.Assign)):
                    raise ValueError('Only methods/functions and single-name assignments can be added')
                removed.add(name)
                continue
            if not prefix and isinstance(node, (ast.Import, ast.ImportFrom)):
                key = ast.dump(node, include_attributes=False)
                if key in expected_removed:
                    if candidate or key in deleted_imports:
                        raise ValueError('Removed import remains or is duplicated')
                    deleted_imports.add(key)
                    continue
                if key in expected_imports:
                    if not candidate or key in imports:
                        raise ValueError('Added import already exists or is duplicated')
                    imports.add(key)
                    continue
            if name in allowed:
                if not isinstance(node, ast.FunctionDef) or name in seen:
                    raise ValueError('Only distinct function bodies can be patched')
                seen.add(name)
                node.body = [ast.Pass()]
                if name in signatures:
                    node.args = ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[])
                    node.returns = None
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                node.body = visit(node.body, name + '.')
            result.append(node)
        return result
    tree.body = visit(tree.body)
    if seen != allowed:
        raise ValueError('Patch function missing from source')
    if candidate and (removed != additions or imports != expected_imports):
        raise ValueError('Declared added node/import missing from source')
    if not candidate and deleted_imports != expected_removed:
        raise ValueError('Declared removed import absent from baseline')
    return ast.dump(tree, include_attributes=False)


def safe_file(root, relative, prefix):
    rel = PurePosixPath(relative)
    if rel.is_absolute() or '..' in rel.parts or '\\' in relative or rel.parts[0] != prefix or rel.suffix != '.py':
        raise ValueError('Unsafe maintained provenance path')
    path = root / relative
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink():
            raise ValueError('Symlink provenance path')
    return path


def manifest_relative_path(path, root):
    """Manifest names use POSIX separators on every host, including Windows."""
    return path.relative_to(root).as_posix()


def validate(root):
    manifest = json.loads((root / 'maintained/patch-manifest.json').read_text())
    failures = []
    seen_modules, expected = set(), set()
    for entry in manifest['modules'] + manifest.get('new_helpers', []):
        module = entry['module']
        if module in seen_modules or entry['maintained'] in expected:
            failures.append('Duplicate maintained module/path')
        seen_modules.add(module)
        expected.add(entry['maintained'])
        candidate = safe_file(root, entry['maintained'], 'maintained')
        if digest(candidate) != entry['maintained_sha256']:
            failures.append(module + ': maintained source changed')
        ast.parse(candidate.read_text(encoding='utf-8'))
    for entry in manifest['modules']:
        baseline = safe_file(root, entry['baseline'], 'recovered')
        candidate = safe_file(root, entry['maintained'], 'maintained')
        allowed = entry['changed_functions']
        if digest(baseline) != entry['baseline_sha256']:
            failures.append(entry['module'] + ': original baseline changed')
        exceptions = dict(changed_signatures=entry.get('changed_signatures', []),
                          added_nodes=entry.get('added_nodes', []), added_imports=entry.get('added_imports', []),
                          removed_imports=entry.get('removed_imports', []))
        if masked_ast(baseline, allowed, **exceptions) != masked_ast(candidate, allowed, candidate=True, **exceptions):
            failures.append(entry['module'] + ': changes outside reviewed AST exceptions')
    for entry in manifest.get('new_helpers', []):
        if 'baseline' in entry or entry.get('status') != 'new_reviewed_helper':
            failures.append(entry['module'] + ': invalid new-helper provenance')
    actual = {manifest_relative_path(path, root) for path in (root / 'maintained').rglob('*.py')}
    if actual != expected:
        failures.append('Maintained source set differs from reviewed patch manifest')
    return failures


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    errors = validate(root)
    if errors:
        raise SystemExit('\n'.join(errors))
    print('Maintained patch hashes and explicit AST scope verified')
