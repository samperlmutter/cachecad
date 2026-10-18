from datetime import datetime
from zoneinfo import ZoneInfo
from tzlocal import get_localzone_name
import time
import logging


def string_to_timestamp(s: str) -> float:
    """
    Converts a string like 'Jul 15, 25 10:23 AM' to a UNIX timestamp.
    Assumes local timezone and no seconds.
    """
    local_tz = ZoneInfo(get_localzone_name())
    dt = datetime.strptime(s, '%b %d, %y %I:%M %p').replace(tzinfo=local_tz)
    return dt.timestamp()


def timestamp_to_string(ts: float | str) -> str:
    try:
        ts = float(ts)
        local_tz = ZoneInfo(get_localzone_name())
        dt = datetime.fromtimestamp(ts, tz=local_tz)
        return dt.strftime('%b %d, %y %I:%M %p')
    except Exception as e:
        logging.error(f'Error converting timestamp {str(ts)} to readable string: {e}')


def drive_time_to_str(iso_string: str) -> str:
    """
    Converts a Google Drive timestamp string (RFC 3339 format) into a readable
    local datetime string like 'Apr 12, 25 10:48 AM'.
    """
    dt_utc = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
    local_tz = ZoneInfo(get_localzone_name())
    dt_local = dt_utc.astimezone(local_tz)
    return dt_local.strftime('%b %d, %y %I:%M %p')


def drive_time_to_unix(iso_string: str) -> int:
    """
    Convert Google Drive timestamp string (RFC 3339 format) to Unix timestamp (int).
    """
    dt_utc = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
    return int(dt_utc.timestamp())


def current_unix_timestamp() -> int:
    return int(time.time())
