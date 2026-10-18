"""Execute selected inspected recovered function bodies with explicit test doubles.

This is a test harness, not a sandbox for hostile Python. It never imports a
recovered module or executes its top-level imports/initializers. Only the named
function AST is compiled, without changing its body. Runtime dependencies must
be supplied explicitly. Original bundled executable/bytecode is never executed.
"""
import ast
import builtins
from contextlib import ExitStack, contextmanager
import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_file_identity_module():
    """Load only the reviewed pure metadata helper, without application imports."""
    spec = importlib.util.spec_from_file_location(
        'core.file_identity', ROOT / 'maintained/core/file_identity.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def file_identity_imports():
    """Provide a scoped exact-name dependency for isolated maintained modules."""
    module = load_file_identity_module()
    with patch.dict(sys.modules, {'core.file_identity': module}):
        yield module


def denied(*args, **kwargs):
    raise AssertionError('Unexpected external effect denied by recovered-function test harness')


def load_function(module_path, qualname, namespace):
    """Load an unchanged FunctionDef body; namespace supplies all dependencies.

module_path is repo-relative or absolute source path. For nested class methods,
qualname is e.g. 'SyncExecutor._upload'. Class initialization is not executed.
Calling zero-argument super() is unsupported without its original class closure.
"""
    path = Path(module_path)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_relative_to(ROOT / 'recovered') and not path.is_relative_to(ROOT / 'maintained'):
        raise ValueError('Only reviewed recovered/maintained source paths are accepted')
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    node = tree
    for name in qualname.split('.'):
        matches = [item for item in node.body if isinstance(item, (ast.ClassDef, ast.FunctionDef)) and item.name == name]
        if len(matches) != 1:
            raise ValueError('Function path missing or ambiguous: ' + qualname)
        node = matches[0]
    if not isinstance(node, ast.FunctionDef):
        raise ValueError('Expected a function definition')
    if node.decorator_list:
        raise ValueError('Decorated functions require explicit reviewed handling')
    safe_builtins = dict(vars(builtins), __import__=denied, open=denied, input=denied, breakpoint=denied)
    globals_dict = dict(namespace)
    globals_dict['__builtins__'] = safe_builtins
    isolated = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(isolated, str(path), 'exec', dont_inherit=True), globals_dict)
    return globals_dict[node.name]


@contextmanager
def deny_external_effects():
    """Defense-in-depth tripwires for accidental network/process activity."""
    with ExitStack() as stack:
        for target in ('socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.socket.bind', 'socket.create_connection',
                       'subprocess.Popen', 'os.system'):
            stack.enter_context(patch(target, side_effect=denied))
        yield


def fixture_open(root):
    """Permit explicit open calls only below one disposable fixture directory."""
    root = Path(root).resolve()
    def open_inside(file, *args, **kwargs):
        path = Path(file).resolve()
        if not path.is_relative_to(root):
            denied()
        return builtins.open(path, *args, **kwargs)
    return open_inside
