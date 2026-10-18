"""Best-effort, local-only crash reports without exception text, locals or paths.

The native handler writes to an anonymous pipe. A separate local process keeps
only allowlisted metadata from that stream, so raw faulthandler output never
becomes a file. This is intentionally independent of the application's logger.
"""
import ast
from collections import deque
from datetime import datetime, timezone
import faulthandler
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid

APP_VERSION = '0.1.0'
MAX_REPORTS = 8
MAX_REPORT_BYTES = 32768
MAX_FRAMES = 64
MAX_BREADCRUMBS = 16
EVENTS = frozenset((
    'bootstrap_verified', 'profile_ready', 'native_capture_ready',
    'application_configured', 'qt_started', 'window_shown', 'shutdown_started',
    'shutdown_finished', 'shutdown_timeout', 'diagnostic_test',
))
KINDS = frozenset(('unhandled-main', 'unhandled-thread', 'qt-callback',
                   'startup-failure', 'shutdown-timeout', 'native-fatal'))
_REPORT_NAME = re.compile(r'crash-[0-9a-f]{32}\.json\Z')
_TEMP_NAME = re.compile(r'\.crash-[0-9a-f]{32}\.json\.tmp\Z')
TEMP_MAX_AGE_SECONDS = 3600
_FRAME = re.compile(r'^  File "([^"\r\n]+)", line ([0-9]{1,9}) in ([^\r\n]*)$')
# Only literal built-in exception class names can be persisted. Custom class
# names can contain provider/user data, so those are classified generically.
_BUILTIN_ERRORS = frozenset(name for name, item in vars(__import__('builtins')).items()
                           if isinstance(item, type) and issubclass(item, BaseException))


def _linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _directory(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if _linked(item):
            raise ValueError('Linked diagnostics directory rejected')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError('Diagnostics directory unavailable')
    return path


def source_catalog(root, sources):
    """Use reviewed source labels, never a basename supplied by exception data."""
    catalog = {}
    for relative, raw in sources.items():
        if not relative.endswith('.py'):
            continue
        filename = Path(relative).name
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}\.py', filename):
            continue
        functions = {'<module>'}
        try:
            tree = ast.parse(raw)
            functions.update(node.name for node in ast.walk(tree)
                             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
            if any(isinstance(node, ast.Lambda) for node in ast.walk(tree)):
                functions.add('<lambda>')
        except (SyntaxError, ValueError):
            continue
        catalog[str(Path(root) / relative)] = (filename, frozenset(functions))
        if relative == 'application/launch.py' and getattr(sys, 'frozen', False):
            # PyInstaller strips the embedded entry script's compile filename.
            catalog['launch.py'] = (filename, frozenset(functions))
    return catalog


def _frame(catalog, filename, function, line):
    source, functions = catalog.get(filename, ('<external>', frozenset()))
    return {'source': source, 'function': function if function in functions else '<redacted>',
            'line': min(max(int(line), 0), 999999999)}


def _metadata(build):
    return {'schema': 1, 'application': 'CacheCAD', 'version': APP_VERSION,
            'build_sha256': build if re.fullmatch(r'[0-9a-f]{64}', build or '') else 'unknown',
            'platform': {'os': {'win32': 'windows', 'linux': 'linux', 'darwin': 'macos'}.get(sys.platform, 'other'),
                         'python': '.'.join(str(n) for n in sys.version_info[:3]),
                         'bits': struct.calcsize('P') * 8}}


def _lock_temporary(fd):
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _prune_temporary(path, cutoff):
    """Reclaim old interrupted writes, never a live writer's locked file."""
    fd = None
    remove = False
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
        _lock_temporary(fd)
        info = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        remove = (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                  and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino)
                  and info.st_mtime < cutoff)
    except OSError:
        pass
    finally:
        if fd is not None:
            os.close(fd)
    # Windows cannot unlink an open file. A writer never reuses an old UUID;
    # successful writes refresh mtime before releasing their lock/renaming.
    if remove:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _prune(directory):
    reports = []
    cutoff = time.time() - TEMP_MAX_AGE_SECONDS
    for item in directory.iterdir():
        if _TEMP_NAME.fullmatch(item.name) and not _linked(item):
            if item.stat(follow_symlinks=False).st_mtime < cutoff:
                _prune_temporary(item, cutoff)
            continue
        if not _REPORT_NAME.fullmatch(item.name) or _linked(item):
            continue
        info = item.stat(follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            reports.append((info.st_mtime_ns, item.name, item))
    for _, _, item in sorted(reports)[:-MAX_REPORTS]:
        try:
            item.unlink()
        except OSError:
            pass


def _write(directory, record):
    """No raw data goes through this sink. Atomic, fsynced, bounded JSON only."""
    directory = _directory(directory)
    raw = (json.dumps(record, sort_keys=True, ensure_ascii=True) + '\n').encode('ascii')
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError('Diagnostics report exceeds its size bound')
    name = 'crash-' + uuid.uuid4().hex + '.json'
    temporary = directory / ('.' + name + '.tmp')
    target = directory / name
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            _lock_temporary(stream.fileno())
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            os.utime(temporary, None)
        os.replace(temporary, target)
        _prune(directory)
        if os.name != 'nt':
            fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return target
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _record(metadata, kind, frames, breadcrumbs, exception_type=None):
    result = dict(metadata, timestamp_utc=datetime.now(timezone.utc).isoformat(),
                  kind=kind, frames=frames[-MAX_FRAMES:], breadcrumbs=list(breadcrumbs)[-MAX_BREADCRUMBS:])
    if exception_type is not None:
        result['exception_type'] = exception_type
    return result


def _stop_helper(process, timeout=5):
    """Close the private pipe, then reap even a stuck child within a bound."""
    try:
        process.stdin.close()
    except Exception:
        pass
    try:
        process.wait(timeout=timeout)
    except Exception:
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass
    finally:
        try:
            if process.poll() is not None and process.stdout is not None:
                process.stdout.close()
        except Exception:
            pass


class CrashDiagnostics:
    """Hooks are fail-safe; there is deliberately no previous/raw-hook chaining."""
    def __init__(self, catalog=None, build=''):
        self.catalog = catalog or {}
        self.metadata = _metadata(build)
        self.directory = None
        self.breadcrumbs = deque(maxlen=MAX_BREADCRUMBS)
        self.pending = deque(maxlen=MAX_REPORTS)
        self._lock = threading.RLock()
        self._previous = None
        self._native = None
        self._native_enabled = False
        self._callback = None
        self.previous_reports = False
        self.available = False

    def install(self):
        self._previous = (sys.excepthook, threading.excepthook)
        self._sys_hook = self._unhandled_main
        self._thread_hook = self._unhandled_thread
        sys.excepthook = self._sys_hook
        threading.excepthook = self._thread_hook
        self.mark('bootstrap_verified')
        return self

    def activate(self, directory):
        try:
            with self._lock:
                self.directory = _directory(directory)
                _prune(self.directory)
                self.previous_reports = any(_REPORT_NAME.fullmatch(item.name) and
                                            item.is_file() and not _linked(item)
                                            for item in self.directory.iterdir())
                self.available = True
                while self.pending:
                    _write(self.directory, self.pending.popleft())
            return True
        except Exception:
            self.available = False
            return False

    def mark(self, event):
        if type(event) is not str or event not in EVENTS:
            return
        try:
            with self._lock:
                self.breadcrumbs.append(event)
                if self._native is not None:
                    self._native.stdin.write(('CACHECAD_EVENT ' + event + '\n').encode('ascii'))
                    self._native.stdin.flush()
        except Exception:
            pass

    def _save(self, record):
        try:
            with self._lock:
                if self.directory is None:
                    self.pending.append(record)
                    return False
                _write(self.directory, record)
                return True
        except Exception:
            return False

    def record_exception(self, kind, exception_type, value, tb):
        # Do not inspect value, exception chains, locals, source lines, or repr.
        if kind not in KINDS or exception_type in (SystemExit, KeyboardInterrupt):
            return False
        try:
            name = type.__getattribute__(exception_type, '__name__')
            module = type.__getattribute__(exception_type, '__module__')
            name = name if module == 'builtins' and name in _BUILTIN_ERRORS else 'ApplicationError'
            frames = deque(maxlen=MAX_FRAMES)
            # A corrupt or deliberately huge traceback must not block the handler.
            count = 0
            while tb is not None and count < 4096:
                code = tb.tb_frame.f_code
                frames.append(_frame(self.catalog, code.co_filename, code.co_name, tb.tb_lineno))
                tb = tb.tb_next
                count += 1
            return self._save(_record(self.metadata, kind, list(frames), self.breadcrumbs, name))
        except Exception:
            return False

    def record_event(self, kind):
        if kind not in KINDS:
            return False
        try:
            return self._save(_record(self.metadata, kind, [], self.breadcrumbs))
        except Exception:
            return False

    def _unhandled_main(self, kind, value, tb):
        self.record_exception('qt-callback' if self._callback is not None else 'unhandled-main', kind, value, tb)
        if self._callback is not None:
            try:
                self._callback()
            except Exception:
                pass

    def _unhandled_thread(self, args):
        self.record_exception('unhandled-thread', args.exc_type, args.exc_value, args.exc_traceback)

    def set_qt_callback(self, callback):
        """Keep the real sys.excepthook active when Qt routes slot exceptions."""
        self._callback = callback

    def enable_native(self, command):
        if not self.available or self._native is not None:
            return False
        process = None
        try:
            # The helper receives no account data or raw output filename. Its only
            # raw input is this private pipe; its stdout/stderr are never persisted.
            process = subprocess.Popen(command, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            ready = threading.Event()
            result = []
            def read_ready():
                try:
                    result.append(process.stdout.readline(64))
                except Exception:
                    pass
                finally:
                    ready.set()
            threading.Thread(target=read_ready, daemon=True).start()
            if not ready.wait(10) or result != [b'CACHECAD_DIAGNOSTICS_READY\n']:
                raise RuntimeError('Native diagnostics helper unavailable')
            process.stdout.close()
            self._native = process
            for event in tuple(self.breadcrumbs):
                process.stdin.write(('CACHECAD_EVENT ' + event + '\n').encode('ascii'))
            process.stdin.flush()
            faulthandler.enable(file=process.stdin, all_threads=True)
            self._native_enabled = True
            self.mark('native_capture_ready')
            return True
        except Exception:
            if process is not None:
                _stop_helper(process, timeout=2)
            self._native = None
            return False

    def close(self):
        try:
            if self._native_enabled:
                faulthandler.disable()
                self._native_enabled = False
            if self._native is not None:
                process, self._native = self._native, None
                _stop_helper(process)
        except Exception:
            pass
        finally:
            if self._previous is not None:
                if sys.excepthook is self._sys_hook:
                    sys.excepthook = self._previous[0]
                if threading.excepthook is self._thread_hook:
                    threading.excepthook = self._previous[1]
                self._previous = None


def capture_native(directory, catalog, build, stream=None, *, announce=False):
    """Drain an anonymous pipe to EOF and persist only strictly parsed fields.

    This process survives a fatal parent exit. It does not import Qt, credentials
    or the application. Unrecognized lines are discarded, never copied to disk.
    """
    stream = sys.stdin.buffer if stream is None else stream
    frames = deque(maxlen=MAX_FRAMES)
    breadcrumbs = deque(maxlen=MAX_BREADCRUMBS)
    fatal = False
    try:
        directory = _directory(directory)
        if announce:
            print('CACHECAD_DIAGNOSTICS_READY', flush=True)
        while True:
            raw = stream.readline(2048)
            if not raw:
                break
            # Overlong lines are discarded in full, not treated as new records.
            if not raw.endswith(b'\n') and len(raw) == 2048:
                while raw and not raw.endswith(b'\n'):
                    raw = stream.readline(2048)
                continue
            line = raw.decode('ascii', errors='replace').rstrip('\r\n')
            if line.startswith(('Fatal Python error:', 'Windows fatal exception:')):
                fatal = True
            elif line.startswith('CACHECAD_EVENT '):
                event = line.removeprefix('CACHECAD_EVENT ')
                if event in EVENTS:
                    breadcrumbs.append(event)
            else:
                frame = _FRAME.fullmatch(line)
                if frame:
                    frames.append(_frame(catalog, frame[1], frame[3], int(frame[2])))
        if fatal:
            _write(directory, _record(_metadata(build), 'native-fatal', list(frames), breadcrumbs))
        return 0
    except Exception:
        return 1
