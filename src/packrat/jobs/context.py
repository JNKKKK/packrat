"""Job execution context — progress + cooperative cancellation (§3, §9).

A job handler receives a :class:`JobContext`. It reports progress by calling
:meth:`JobContext.progress`, and cooperatively checks for cancellation at its
existing checkpoints via :meth:`JobContext.check_cancelled` (or the cheaper
:attr:`JobContext.cancelled` flag). The context also holds the frozen
:class:`~packrat.config.Config` snapshot taken at job start (§9.2).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import Config


class CancelledError(Exception):
    """Raised inside a job when it observes its cancel flag at a checkpoint."""


@dataclass
class ProgressEvent:
    """A single progress/state push, streamed to clients over SSE (§3)."""

    job_id: int
    type: str  # one of: progress|state|log|done|error
    status: str | None = None
    total: int | None = None
    done: int | None = None
    message: str | None = None
    eta_s: float | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"job_id": self.job_id, "type": self.type}
        for k in ("status", "total", "done", "message", "eta_s"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.extra:
            d.update(self.extra)
        return d


class JobContext:
    """Handed to a job handler; the job's only channel to the runtime."""

    def __init__(
        self,
        job_id: int,
        job_type: str,
        params: dict,
        config: Config,
        db,
        *,
        emit: Callable[[ProgressEvent], None],
        set_progress: Callable[[int, int | None], None],
        cancel_event: threading.Event,
    ):
        self.job_id = job_id
        self.job_type = job_type
        self.params = params
        self.config = config
        self.db = db
        self._emit = emit
        self._set_progress = set_progress
        self._cancel = cancel_event
        self._total: int | None = None
        self._done = 0
        self._result: dict | None = None

    # -- result ----------------------------------------------------------
    def set_result(self, result: dict) -> None:
        """Record this job's uniform, human-showable outcome summary (§4/§12).

        Persisted to ``jobs.result_json`` by the queue at terminal time — the single
        surface the TUI renders as a job's result card without joining per-op tables.
        Call it as the job's outcome becomes known (e.g. at the end of a successful
        run, or with a partial tally before an early return); the LAST value set wins.
        A job may set nothing (result stays NULL) — its ``status``/``error`` still
        record the outcome, so every job is show-able.
        """
        self._result = result

    @property
    def result(self) -> dict | None:
        return self._result

    # -- cancellation ----------------------------------------------------
    @property
    def cancelled(self) -> bool:
        """True once a cancel has been requested (cheap, non-raising)."""
        return self._cancel.is_set()

    def check_cancelled(self) -> None:
        """Raise :class:`CancelledError` if cancel was requested (§9 checkpoints)."""
        if self._cancel.is_set():
            raise CancelledError()

    # -- progress --------------------------------------------------------
    def set_total(self, total: int) -> None:
        self._total = total
        self._set_progress(self._done, total)
        self._emit(ProgressEvent(self.job_id, "progress", total=total, done=self._done))

    def progress(self, done: int | None = None, *, message: str | None = None) -> None:
        """Report progress. ``done`` defaults to incrementing by one.

        No ETA is emitted here — the daemon leaves ``ProgressEvent.eta_s`` unset and
        the TUI derives ETA client-side from the progress stream (:class:`EtaEstimator`).
        """
        if done is None:
            self._done += 1
        else:
            self._done = done
        # Persist the counter (progress-display only — §4), then push an event.
        self._set_progress(self._done, self._total)
        self._emit(
            ProgressEvent(
                self.job_id, "progress",
                total=self._total, done=self._done,
                message=message,
            )
        )

    def log(self, message: str) -> None:
        """Emit a log line to attached clients (also captured server-side)."""
        self._emit(ProgressEvent(self.job_id, "log", message=message))
