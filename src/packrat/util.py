"""Small shared helpers."""

from __future__ import annotations

import datetime as _dt


def now_iso() -> str:
    """UTC timestamp in ISO-8601, used for all ``*_at`` columns."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
