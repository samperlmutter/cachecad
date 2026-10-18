def format_file_size(size_bytes: int) -> str:
    """
    Converts a file size in bytes into a human-readable string (e.g. KB, MB, GB).
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    units = ['KB', 'MB', 'GB', 'TB']
    size = size_bytes / 1024
    for unit in units:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"
