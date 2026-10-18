"""Select the offline default or explicit identity-only sign-in test launcher.

The frozen bootstrap compiles launcher bytes from the verified source payload;
application and launcher modules are never shadowed by a PyInstaller archive.
This unsigned hash check detects changes, but is not a signed trust chain.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types


LAUNCHERS = ('launch', 'signin')


def launcher_sources(root, *, verify):
    """Read launcher snapshots without importing them or using adjacent bytecode."""
    root = Path(root).resolve(strict=True)
    expected = {}
    if verify:
        manifest_path = root / 'assembly-manifest.json'
        if manifest_path.is_symlink():
            raise ValueError('Symlink launcher manifest rejected')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        for entry in manifest['files']:
            if entry['path'] in expected:
                raise ValueError('Duplicate launcher manifest path')
            expected[entry['path']] = entry['sha256']
    result = {}
    for name in LAUNCHERS:
        relative = f'development/{name}.py'
        path = root / relative
        for parent in (path, *path.parents):
            if parent == root:
                break
            if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
                raise ValueError('Unsafe launcher source path')
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError('Launcher source escapes application root')
        raw = path.read_bytes()
        if verify and (relative not in expected or hashlib.sha256(raw).hexdigest() != expected[relative]):
            raise ValueError('Launcher source differs from reviewed assembly: ' + relative)
        result[name] = (path, raw)
    return result


def load_launcher(name, sources):
    """Load a preverified snapshot into a closed development namespace."""
    if name not in LAUNCHERS:
        raise ValueError('Unsupported development launcher')
    fullname = 'development.' + name
    if fullname in sys.modules:
        raise RuntimeError('Launcher loaded before source verification: ' + fullname)
    if 'development' not in sys.modules:
        package = types.ModuleType('development')
        package.__path__ = []
        package.__package__ = 'development'
        sys.modules['development'] = package
    package = sys.modules['development']
    # No ambient development modules may be found alongside the reviewed payload.
    package.__path__ = []
    path, raw = sources[name]
    spec = importlib.util.spec_from_loader(fullname, loader=None, origin=str(path))
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(path)
    sys.modules[fullname] = module
    setattr(package, name, module)
    try:
        exec(compile(raw, str(path), 'exec'), module.__dict__)
    except BaseException:
        sys.modules.pop(fullname, None)
        delattr(package, name)
        raise
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--signin', action='store_true')
    mode, remaining = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    frozen = bool(getattr(sys, 'frozen', False))
    root = Path(sys._MEIPASS) / 'app-source' if frozen else Path(__file__).resolve().parents[1]
    sources = launcher_sources(root, verify=frozen or (root / 'assembly-manifest.json').is_file())
    sys.dont_write_bytecode = True
    offline = load_launcher('launch', sources)
    selected = load_launcher('signin', sources) if mode.signin else offline
    original = sys.argv
    sys.argv = [original[0], *remaining]
    try:
        return selected.main()
    finally:
        sys.argv = original


if __name__ == '__main__':
    raise SystemExit(main())
