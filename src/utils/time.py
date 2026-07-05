"""UTC time helpers."""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return naive UTC datetime for SQLite DateTime compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_now_iso_z(timespec: str = "seconds") -> str:
    """Return an ISO-8601 UTC timestamp with a trailing Z."""
    return datetime.now(timezone.utc).isoformat(timespec=timespec).replace("+00:00", "Z")
