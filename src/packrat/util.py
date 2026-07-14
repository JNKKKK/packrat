"""Small shared helpers."""

from __future__ import annotations

import datetime as _dt


def now_iso() -> str:
    """UTC timestamp in ISO-8601, used for all ``*_at`` columns."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> _dt.datetime | None:
    """Parse an ISO timestamp back to a datetime (or None)."""
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value)
    except ValueError:
        return None
