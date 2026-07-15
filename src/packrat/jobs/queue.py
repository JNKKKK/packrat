"""The single-worker job queue (§3) — packrat's serialization point.

Enforces both concurrency guarantees:
1. **Global: one mutating job at a time.** A second submission while one runs is
   *rejected* with :class:`BusyError` naming the in-flight job — no backlog queue.
2. **Per-root: one active op per owned root.** Submitting a job whose owned root
   already has an active op is rejected. (A job may own no root — e.g. ``scan
   --all`` or ``untrash`` — and is then only bound by guarantee 1; the real
   per-root holders are pending review_runs / open merge_runs, checked here.)

The worker slot is **in-memory** (a live daemon has at most one running job, in
this process). That is what makes startup reconciliation correct: any ``running``
row found at boot is stale (§3) — see :mod:`packrat.jobs.reconcile`.

Progress/state is pushed to subscribers as :class:`ProgressEvent`s over an SSE
fan-out; ``jobs.done`` is persisted purely as the progress-display counter (§4).
"""

from __future__ import annotations

import json
import logging
import queue as _q
import threading
from collections.abc import Callable

from ..config import Config, load_config
from ..db import Database
from ..util import now_iso
from .context import CancelledError, JobContext, ProgressEvent
from .registry import get_job_spec

log = logging.getLogger("packrat.jobs")


class BusyError(Exception):
    """Submission rejected: the queue or the owned root is busy (§3).

    ``kind`` distinguishes the two guarantees so the client can phrase the
    message ("busy: <job>" vs "root <name> busy: <holder>").
    """

    def __init__(self, message: str, *, kind: str = "global", holder: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.holder = holder or {}


class _Subscriber:
    """A blocking queue of events for one attached SSE client."""

    def __init__(self, job_id: int):
        self.job_id = job_id
        self.q: _q.Queue[ProgressEvent | None] = _q.Queue(maxsize=1000)

    def push(self, ev: ProgressEvent) -> None:
        try:
            self.q.put_nowait(ev)
        except _q.Full:
            # Slow client: drop rather than block the worker. State is durable
            # in the jobs table, so a reconnect recovers (§3 SSE degrades).
            pass

    def close(self) -> None:
        try:
            self.q.put_nowait(None)
        except _q.Full:
            pass


class JobQueue:
    """Owns the worker thread and the (at most one) running mutating job."""

    def __init__(self, db: Database, config_loader: Callable[[], Config] = load_config):
        self.db = db
        self._config_loader = config_loader
        self._lock = threading.RLock()
        self._running_job_id: int | None = None
        self._running_type: str | None = None
        self._cancel_event: threading.Event | None = None
        self._worker: threading.Thread | None = None
        self._subscribers: dict[int, list[_Subscriber]] = {}
        self._sub_lock = threading.Lock()

    # -- submission ------------------------------------------------------
    def submit(self, job_type: str, params: dict) -> int:
        """Validate + start a job, returning its job id. Raises :class:`BusyError`.

        Runs synchronously up to creating the ``jobs`` row and launching the
        worker thread; the work itself runs on that thread.
        """
        spec = get_job_spec(job_type)
        if spec is None:
            raise ValueError(f"unknown job type: {job_type}")

        with self._lock:
            # Guarantee 1: global single mutating slot.
            if spec.mutating and self._running_job_id is not None:
                running = self._describe_running()
                raise BusyError(
                    f"busy: {running['type']} started {running['started_at']}",
                    kind="global",
                    holder=running,
                )

            # Guarantee 2: per-root exclusivity (owned root already active).
            if spec.owned_root is not None:
                root_id = spec.owned_root(params)
                if root_id is not None:
                    holder = self._root_holder(root_id)
                    if holder is not None:
                        raise BusyError(
                            f"root busy: {holder['what']}",
                            kind="root",
                            holder=holder,
                        )

            # Create the jobs row (running) and take the in-memory slot.
            job_id = self._create_job_row(job_type, params)
            cancel_event = threading.Event()
            self._running_job_id = job_id
            self._running_type = job_type
            self._cancel_event = cancel_event

        worker = threading.Thread(
            target=self._run_job,
            args=(job_id, spec, params, cancel_event),
            name=f"packrat-job-{job_id}",
            daemon=True,
        )
        self._worker = worker
        worker.start()
        return job_id

    def _create_job_row(self, job_type: str, params: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO jobs(type, status, total, done, started_at, params_json) "
            "VALUES (?, 'running', NULL, 0, ?, ?)",
            (job_type, now_iso(), json.dumps(params)),
        )
        return int(cur.lastrowid)

    # -- worker ----------------------------------------------------------
    def _run_job(self, job_id: int, spec, params: dict, cancel_event: threading.Event) -> None:
        # Snapshot config at job start (§9.2 per-job reload). A parse error here
        # rejects the job cleanly rather than crashing the worker.
        try:
            config = self._config_loader()
        except Exception as exc:  # ConfigError and friends
            log.warning("job %d rejected: bad config: %s", job_id, exc)
            self._finish(job_id, "error", error=f"config error: {exc}")
            self._release(job_id)
            return

        ctx = JobContext(
            job_id=job_id,
            job_type=spec.type,
            params=params,
            config=config,
            db=self.db,
            emit=lambda ev: self._broadcast(ev),
            set_progress=lambda done, total: self._persist_progress(job_id, done, total),
            cancel_event=cancel_event,
        )
        self._broadcast(ProgressEvent(job_id, "state", status="running"))
        try:
            spec.handler(ctx)
        except CancelledError:
            log.info("job %d cancelled", job_id)
            self._finish(job_id, "cancelled", error=None)
            self._broadcast(ProgressEvent(job_id, "state", status="cancelled"))
        except Exception as exc:  # noqa: BLE001 - jobs must never crash the daemon
            log.exception("job %d failed", job_id)
            self._finish(job_id, "error", error=str(exc))
            self._broadcast(ProgressEvent(job_id, "error", status="error", message=str(exc)))
        else:
            self._finish(job_id, "done", error=None)
            self._broadcast(ProgressEvent(job_id, "done", status="done"))
        finally:
            self._close_subscribers(job_id)
            self._release(job_id)

    def _persist_progress(self, job_id: int, done: int, total: int | None) -> None:
        if total is None:
            self._safe_write("UPDATE jobs SET done=? WHERE id=?", (done, job_id))
        else:
            self._safe_write(
                "UPDATE jobs SET done=?, total=? WHERE id=?", (done, total, job_id)
            )

    def _finish(self, job_id: int, status: str, *, error: str | None) -> None:
        self._safe_write(
            "UPDATE jobs SET status=?, finished_at=?, error=? WHERE id=?",
            (status, now_iso(), error, job_id),
        )

    def _safe_write(self, sql: str, params: tuple) -> None:
        """Write, tolerating a DB closed out from under us during shutdown.

        If the daemon is exiting, its shared connection may already be gone when
        a still-running worker tries to persist. That is harmless — startup
        reconciliation (§3) flips a stale ``running`` row to ``interrupted`` — so
        we swallow the closed-DB error rather than crash the worker thread.
        """
        import sqlite3

        try:
            self.db.execute(sql, params)
        except sqlite3.ProgrammingError as exc:
            if "closed database" in str(exc).lower():
                log.debug("db closed during shutdown; dropping worker write")
                return
            raise

    def _release(self, job_id: int) -> None:
        with self._lock:
            if self._running_job_id == job_id:
                self._running_job_id = None
                self._running_type = None
                self._cancel_event = None

    # -- cancellation ----------------------------------------------------
    def cancel(self, job_id: int) -> bool:
        """Request cooperative cancellation of ``job_id`` (§3).

        Returns True if that job is the running one and the flag was set. The
        worker observes it at its next checkpoint and lands the job in
        ``cancelled`` (terminal) — distinct from ``interrupted`` (§3).
        """
        with self._lock:
            if self._running_job_id == job_id and self._cancel_event is not None:
                self._cancel_event.set()
                self._broadcast(ProgressEvent(job_id, "log", message="cancel requested"))
                return True
        return False

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Cancel any running job and join the worker (clean daemon/test teardown).

        A graceful ``daemon stop`` signals cancel and lets the worker checkpoint;
        the resulting row is reconciled to ``interrupted`` on next start (§3).
        """
        with self._lock:
            if self._cancel_event is not None:
                self._cancel_event.set()
            worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)

    # -- introspection ---------------------------------------------------
    def running_job_id(self) -> int | None:
        with self._lock:
            return self._running_job_id

    def _describe_running(self) -> dict:
        row = self.db.query_one(
            "SELECT id, type, started_at FROM jobs WHERE id=?",
            (self._running_job_id,),
        )
        if row is None:
            return {"id": self._running_job_id, "type": self._running_type, "started_at": "?"}
        return {"id": row["id"], "type": row["type"], "started_at": row["started_at"]}

    def _root_holder(self, root_id: int) -> dict | None:
        """Return a description of the op currently owning ``root_id``, or None (§3).

        The owned-root holders are a pending review_run or an open merge_run
        (per §4 partial-unique indexes). Checked here so M1+ ops are gated by the
        queue, not just by DB constraints.
        """
        rr = self.db.query_one(
            "SELECT id, run_type, created_at FROM review_runs "
            "WHERE root_id=? AND status='pending'",
            (root_id,),
        )
        if rr is not None:
            return {
                "type": "review_run",
                "run_type": rr["run_type"],
                "since": rr["created_at"],
                "what": f"{rr['run_type']} pending since {rr['created_at']}",
            }
        mr = self.db.query_one(
            "SELECT id, status, created_at FROM merge_runs "
            "WHERE dest_root_id=? AND status IN ('planning','copying')",
            (root_id,),
        )
        if mr is not None:
            return {
                "type": "merge_run",
                "status": mr["status"],
                "since": mr["created_at"],
                "what": f"merge {mr['status']} since {mr['created_at']}",
            }
        return None

    # -- SSE fan-out -----------------------------------------------------
    def subscribe(self, job_id: int) -> _Subscriber:
        sub = _Subscriber(job_id)
        with self._sub_lock:
            self._subscribers.setdefault(job_id, []).append(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        with self._sub_lock:
            subs = self._subscribers.get(sub.job_id)
            if subs and sub in subs:
                subs.remove(sub)
                if not subs:
                    self._subscribers.pop(sub.job_id, None)

    def _broadcast(self, ev: ProgressEvent) -> None:
        with self._sub_lock:
            for sub in self._subscribers.get(ev.job_id, []):
                sub.push(ev)

    def _close_subscribers(self, job_id: int) -> None:
        with self._sub_lock:
            for sub in self._subscribers.get(job_id, []):
                sub.close()
