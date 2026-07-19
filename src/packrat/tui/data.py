"""Data & liveness — when the dict changes and who pushes it (component-plan §Data).

Widgets are pure ``dict → frame``; this module is the only thing that knows the
dict can change. Two pure, unit-testable pieces first (no Textual, no clock calls):

- :func:`reltime` — an ISO timestamp → the compact "2h ago" / "today 11:31" /
  "Jul 12" / "now" strings the mockups use, given an explicit ``now`` (never the
  wall clock, so golden tests stay deterministic).
- :class:`EtaEstimator` — the TUI-side ETA (§ cross-cutting "ETA is computed
  TUI-side"): ``(total − done) / rate`` over a short trailing window of SSE
  progress samples, blank until enough has streamed. The daemon leaves
  ``ProgressEvent.eta_s`` unset; this fills it.

Then :class:`DataSource`, the subscription seam over a ``queries``/daemon-client
call that a screen subscribes to (refresh triggers: SSE push, job-finished
refetch, light poll backstop). It touches the daemon **only through**
:class:`~packrat.daemon.client.DaemonClient` — no widget calls the daemon directly.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from .tokens import ETA_WINDOW_S


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


# --- DataSource seam -------------------------------------------------------
class DataSource:
    """A subscription over one read-model query (component-plan §Data & liveness).

    Wraps a ``queries``/daemon call; a screen ``subscribe()``s and gets pushed the
    new value whenever :meth:`refresh` re-fetches (the caller drives *when* — on an
    SSE ``done``/``error`` event, on the poll timer, or on demand). Keeping the
    fetch behind this seam means widgets never call the daemon and stay pure
    ``dict → frame``; the app wires a Textual reactive to :meth:`subscribe`.

    ``fetch`` is any zero-arg callable returning the payload (e.g.
    ``lambda: client.status()`` or, for an offline/demo mode,
    ``fixtures.status_snapshot``). Exceptions from ``fetch`` are captured on
    :attr:`error` (so a daemon-down degrades to a "waiting…" state, not a crash)
    and the last good :attr:`value` is retained.
    """

    def __init__(self, fetch):
        self._fetch = fetch
        self.value = None
        self.error: Exception | None = None
        self._subs: list = []

    def subscribe(self, callback) -> None:
        """Register ``callback(value)``; called on every successful refresh."""
        self._subs.append(callback)

    def refresh(self):
        """Re-fetch and push to subscribers. Returns the new value (or last good)."""
        try:
            self.value = self._fetch()
            self.error = None
        except Exception as exc:  # daemon down / transient — degrade, keep last good
            self.error = exc
            return self.value
        for cb in self._subs:
            cb(self.value)
        return self.value

    @property
    def healthy(self) -> bool:
        """False after a failed refresh — drives the ``daemon ○ down`` TitleBar state."""
        return self.error is None
