"""Comparable path/handle metadata without discarding mutation checks.

CPython 3.12 on Windows reports creation time for path-stat ``st_ctime_ns``
but change time for fstat. Both expose the same ``st_birthtime_ns``. Compare
birth time across those APIs, and separately compare raw ctime between two
samples of the SAME open handle. POSIX continues to use metadata change time.
See https://github.com/python/cpython/issues/157671.

These are observed-version guards, not filesystem compare-and-swap or locks.
"""


def file_identity(info):
    """Cross-path/handle identity; retain POSIX ctime and require Windows birthtime."""
    if hasattr(info, 'st_file_attributes'):
        timestamp = getattr(info, 'st_birthtime_ns', None)
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError('Windows file identity requires a valid creation timestamp')
    else:
        timestamp = info.st_ctime_ns
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, timestamp)


def handle_identity(info):
    """Same-handle version, including raw change time even on Windows."""
    return file_identity(info) + (info.st_ctime_ns,)
