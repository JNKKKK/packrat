"""Data & liveness — when the dict changes and who pushes it (component-plan §Data).

Widgets are pure ``dict → frame``; this module holds the pure, unit-testable
time/ETA helpers the app uses when rendering live data (no Textual, no clock calls):

- :func:`reltime` — an ISO timestamp → the compact "2h ago" / "today 11:31" /
  "Jul 12" / "now" strings the mockups use, given an explicit ``now`` (never the
  wall clock, so golden tests stay deterministic).
- :class:`EtaEstimator` — the TUI-side ETA (§ cross-cutting "ETA is computed
  TUI-side"): ``(total − done) / rate`` over a short trailing window of SSE
  progress samples, blank until enough has streamed. The daemon leaves
  ``ProgressEvent.eta_s`` unset; this fills it.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from .tokens import ETA_WINDOW_S


def same_day(ts: str | None, now: str | None) -> bool:
    """True if two ISO timestamps fall on the same calendar day (``YYYY-MM-DD``).

    Drives the ``reltime(..., clock=)`` "today HH:MM" affordance. A missing/empty
    ``ts`` is never "today" (its blank prefix can't match a real ``now``).
    """
    return bool(ts) and (ts or "")[:10] == (now or "")[:10]


def result_of(job: dict) -> dict:
    """Parse a job row's ``result_json`` to a dict (``{}`` if absent/malformed).

    The one place the TUI decodes the daemon's compact outcome summary (§4/§12) —
    job cards, the queue history line, and the offline demo all read it through here
    instead of re-writing the ``json.loads(... or "{}")`` guard.
    """
    try:
        return json.loads(job.get("result_json") or "{}")
    except (ValueError, TypeError):
        return {}


# --- relative time ---------------------------------------------------------
def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts[:19])
    except (ValueError, TypeError):
        return None


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def reltime(ts: str | None, now: str | datetime, *, clock: bool = False) -> str:
    """Compact relative time for a list/detail row (the mockup vocabulary).

    ``now`` is passed explicitly (an ISO string or ``datetime``) so rendering is
    deterministic in tests. Rules, matching the mockups:
    - < 90 s          → ``now``
    - < 60 min        → ``Nm ago``
    - < 24 h          → ``Nh ago``
    - same year       → ``Jul 12`` (``+ HH:MM`` when ``clock`` and it's *today*)
    - otherwise       → ``2025 Jul 12``
    ``None`` → ``never``.
    """
    t = _parse(ts)
    if t is None:
        return "never"
    n = now if isinstance(now, datetime) else _parse(now)
    if n is None:
        return "never"
    delta = (n - t).total_seconds()
    if 0 <= delta < 90:
        return "now"
    if 0 <= delta < 3600:
        return f"{int(delta // 60)}m ago"
    if 0 <= delta < 86400 and t.date() == n.date():
        return f"today {t:%H:%M}" if clock else f"{int(delta // 3600)}h ago"
    if 0 <= delta < 86400:
        return f"{int(delta // 3600)}h ago"
    label = f"{_MONTHS[t.month - 1]} {t.day:02d}"
    if t.year != n.year:
        label = f"{t.year} {label}"
    return label


# --- TUI-side ETA ----------------------------------------------------------
@dataclass
class EtaEstimator:
    """Derive an ``ETA`` from a trailing window of ``(t, done)`` progress samples.

    Rate = Δdone/Δt over samples within :data:`ETA_WINDOW_S`; ETA = remaining/rate.
    Degrades to ``None`` (blank) until ≥2 samples span enough time — a pure
    presentation estimate, never authoritative (§ cross-cutting).
    """

    window_s: float = ETA_WINDOW_S
    _samples: deque = field(default_factory=deque)

    def observe(self, t: float, done: int) -> None:
        """Record a progress sample at monotonic time ``t`` with count ``done``."""
        self._samples.append((t, done))
        cutoff = t - self.window_s
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def eta_s(self, total: int | None) -> float | None:
        """Estimated seconds remaining to reach ``total`` (None if not yet derivable)."""
        if not total or len(self._samples) < 2:
            return None
        (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
        dt, dd = t1 - t0, d1 - d0
        if dt <= 0 or dd <= 0:
            return None
        rate = dd / dt
        remaining = total - d1
        if remaining <= 0:
            return 0.0
        return remaining / rate

    def reset(self) -> None:
        self._samples.clear()


def fmt_eta(seconds: float | None) -> str:
    """Format an ETA in the mockup's compact style: ``ETA 4m`` / ``ETA 45s`` / ``""``."""
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"ETA {seconds}s"
    if seconds < 3600:
        return f"ETA {seconds // 60}m"
    h, m = divmod(seconds // 60, 60)
    return f"ETA {h}h{m:02d}m"
