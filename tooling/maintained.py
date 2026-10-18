"""Validate maintained patch provenance and restrict changes to reviewed bodies."""
import ast
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def masked_ast(path, allowed):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    seen = set()
    def visit(body, prefix=''):
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                if name in allowed:
                    if not isinstance(node, ast.FunctionDef):
                        raise ValueError('Only function bodies can be patched')
                    seen.add(name)
                    node.body = [ast.Pass()]
                else:
                    visit(node.body, name + '.')
    visit(tree.body)
    if seen != set(allowed):
        raise ValueError('Patch function missing from source')
    return ast.dump(tree, include_attributes=False)


def validate(root):
    manifest = json.loads((root / 'maintained/patch-manifest.json').read_text())
    failures = []
    for entry in manifest['modules']:
        baseline = root / entry['baseline']
        candidate = root / entry['maintained']
        allowed = entry['changed_functions']
        if digest(baseline) != entry['baseline_sha256']:
            failures.append(entry['module'] + ': original baseline changed')
        if digest(candidate) != entry['maintained_sha256']:
            failures.append(entry['module'] + ': maintained source changed')
        if masked_ast(baseline, allowed) != masked_ast(candidate, allowed):
            failures.append(entry['module'] + ': changes outside reviewed function bodies')
    expected = {entry['maintained'] for entry in manifest['modules']}
    actual = {str(path.relative_to(root)) for path in (root / 'maintained').rglob('*.py')}
    if actual != expected:
        failures.append('Maintained source set differs from reviewed patch manifest')
    return failures


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    errors = validate(root)
    if errors:
        raise SystemExit('\n'.join(errors))
    print('Maintained patch hashes and function-body scope verified')
