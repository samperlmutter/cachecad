"""Offline adapters only. MemoryDrive has atomic conditional mutation.

LocalWorkspace is for controlled disposable fixtures, not concurrent external
writers or production CAD folders. No cloud/authentication code exists here.
"""
from pathlib import Path, PurePosixPath
from hashlib import sha256
from threading import RLock
import os
import tempfile
from .models import FileSnapshot, Scan

class ConflictError(RuntimeError):pass

def safe_relative(path):
    if not isinstance(path,str) or not path or '\\' in path or '\x00' in path:
        raise ValueError('Expected canonical relative slash-separated path')
    p=PurePosixPath(path)
    if p.is_absolute() or '..' in p.parts or ':' in path or str(p)!=path:
        raise ValueError('Invalid project-relative path')
    if p.parts[0].casefold()=='.cachecad-reconstruction':
        raise ValueError('Reserved internal namespace')
    return path

def digest(data):return sha256(data).hexdigest()

class MemoryDrive:
    """Network-free versioned backend with CAS under one lock."""
    def __init__(self,files=None):
        self._lock=RLock();self._files={};self._generation=0
        self.scan_complete=True
        self.fail_paths=set()
        for path,data in (files or {}).items():self._put(path,data)
    def _put(self,path,data):
        safe_relative(path);self._generation+=1
        self._files[path]=(bytes(data),str(self._generation))
    def _snapshot(self,path):
        item=self._files.get(path)
        return None if item is None else FileSnapshot(path,digest(item[0]),len(item[0]),item[1])
    def scan(self):
        with self._lock:
            return Scan({p:self._snapshot(p) for p in self._files},self.scan_complete,
                        () if self.scan_complete else ('Remote listing incomplete',))
    def snapshot(self,path):
        safe_relative(path)
        with self._lock:return self._snapshot(path)
    def read(self,path,expected):
        with self._lock:
            if self._snapshot(path)!=expected or expected is None:raise ConflictError('Remote changed')
            return self._files[path][0]
    def put(self,path,data,expected):
        with self._lock:
            if path in self.fail_paths:raise OSError('Synthetic provider failure')
            if self._snapshot(path)!=expected:raise ConflictError('Remote changed before commit')
            self._put(path,data);return self._snapshot(path)
    def delete(self,path,expected):
        with self._lock:
            if path in self.fail_paths:return False
            if self._snapshot(path)!=expected or expected is None:raise ConflictError('Remote changed before delete')
            del self._files[path];return True

class LocalWorkspace:
    """Explicit sandbox directory; rejects symlinks and noncanonical paths.

    The instance lock serializes this adapter's writes, not external programs.
    Immutable staged upload bytes are read and fingerprinted under that lock.
    """
    def __init__(self,root):
        p=Path(root)
        if not p.is_dir() or p.is_symlink():raise ValueError('Existing nonsymlink sandbox directory required')
        self.root=p.resolve();self._lock=RLock()
    def _path(self,relative):
        safe_relative(relative);p=self.root
        for part in PurePosixPath(relative).parts:
            p=p/part
            if p.is_symlink():raise ValueError('Symlink paths are not supported')
        return p
    def _snapshot(self,path):
        p=self._path(path)
        if not p.exists():return None
        if not p.is_file():raise ValueError('Expected regular file')
        before=p.stat();data=p.read_bytes();after=p.stat()
        identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        if identity(before)!=identity(after):raise ConflictError('Local changed while reading')
        return FileSnapshot(path,digest(data),len(data),':'.join(map(str,identity(after))))
    def snapshot(self,path):
        with self._lock:return self._snapshot(path)
    def scan(self):
        files={};errors=[]
        with self._lock:
            for p in sorted(self.root.rglob('*')):
                rel=p.relative_to(self.root).as_posix()
                try:
                    if p.is_symlink():raise ValueError('Symlink encountered')
                    if rel=='.cachecad-reconstruction' or rel.startswith('.cachecad-reconstruction/'):continue
                    if p.is_file():files[rel]=self._snapshot(rel)
                except (OSError,ValueError,ConflictError) as exc:errors.append(f'{rel}: {type(exc).__name__}')
        return Scan(files,not errors,tuple(errors))
    def read_staged(self,path,expected):
        with self._lock:
            if expected is None or self._snapshot(path)!=expected:raise ConflictError('Local changed since analysis')
            data=bytes(self._path(path).read_bytes())
            if digest(data)!=expected.digest or self._snapshot(path)!=expected:
                raise ConflictError('Local changed during staging')
            return data
    def _backup(self,path,expected):
        directory=self.root
        for part in ('.cachecad-reconstruction','backups'):
            directory=directory/part
            if directory.is_symlink():raise ValueError('Symlink in internal backup path')
            directory.mkdir(exist_ok=True)
            if not directory.is_dir():raise ValueError('Invalid backup directory')
        backup=directory/expected.digest
        if backup.is_symlink():raise ValueError('Symlink backup target')
        data=self._path(path).read_bytes()
        if digest(data)!=expected.digest:raise ConflictError('Local changed before backup')
        if backup.exists():
            if not backup.is_file() or digest(backup.read_bytes())!=expected.digest:
                raise ConflictError('Existing backup is corrupt')
        else:
            with backup.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        return backup
    def put(self,path,data,expected):
        with self._lock:
            if self._snapshot(path)!=expected:raise ConflictError('Local changed before commit')
            p=self._path(path);p.parent.mkdir(parents=True,exist_ok=True)
            fd,temp=tempfile.mkstemp(prefix='.staged-',dir=p.parent)
            try:
                with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
                if self._snapshot(path)!=expected:raise ConflictError('Local changed before replace')
                if expected is None:
                    # Atomically publish completed bytes without replacing a target.
                    os.link(temp,p)
                else:
                    self._backup(path,expected)
                    if self._snapshot(path)!=expected:raise ConflictError('Local changed after backup')
                    os.replace(temp,p)
            finally:
                if os.path.exists(temp):os.unlink(temp)
            return self._snapshot(path)
    def delete(self,path,expected):
        with self._lock:
            if expected is None or self._snapshot(path)!=expected:raise ConflictError('Local changed before delete')
            self._backup(path,expected)
            if self._snapshot(path)!=expected:raise ConflictError('Local changed after backup')
            self._path(path).unlink();return True
