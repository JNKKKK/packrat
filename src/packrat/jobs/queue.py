"""The single-worker job queue (§3) — packrat's serialization point.

Enforces both concurrency guarantees, but as a **durable queue**, not a reject:
1. **Global: one mutating job runs at a time; the rest wait in a durable FIFO
   backlog.** Every mutating submission is *enqueued* (a ``jobs`` row with
   ``status='queued'``) — never rejected. When the worker frees it dequeues the
   first **runnable** job in ``enqueued_at`` order and runs it.
2. **Per-root: one active op per owned root — enforced at DEQUEUE, not submit.** A
   job whose owned root is already held by a pending review / open merge is
   *skipped* (left ``queued``) and retried on a later pump, so the queue waits only
   on the worker, never on a human. A job may own no root (``scan --all``,
   ``untrash``, ``trash-refresh``) → never blocked on this account.

The worker slot is **in-memory** (a live daemon has at most one running job, in
this process). That is what makes startup reconciliation correct: any ``running``
row found at boot is stale (§3) — see :mod:`packrat.jobs.reconcile`. The *backlog*,
by contrast, is durable (``queued`` rows) and drains after a restart.

The queue is **pumped after every job finishes** (and on startup): a completing
job frees the worker slot AND may release a root-holder (a ``--confirm``/``--cancel``
is itself a job), so one pump both starts the next job and unblocks anything that
was waiting on that root. :meth:`submit` never rejects a mutating job — the only
submit-time error is an unknown job type (a ``ValueError``).

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
        self._cancel_event: threading.Event | None = None
        self._worker: threading.Thread | None = None
        self._subscribers: dict[int, list[_Subscriber]] = {}
        self._sub_lock = threading.Lock()

    # -- submission ------------------------------------------------------
    def submit(self, job_type: str, params: dict) -> int:
        """Enqueue a job, returning its job id; pump the queue.

        Every mutating submission is enqueued as a ``queued`` row (§3 guarantee 1) —
        it is **not** rejected if the worker is busy or the owned root is held (that
        is decided later, at dequeue). Only an *unknown job type* raises
        (:class:`ValueError`). Returns immediately; the work runs on the worker
        thread once :meth:`_pump` picks it up.
        """
        spec = get_job_spec(job_type)
        if spec is None:
            raise ValueError(f"unknown job type: {job_type}")

        with self._lock:
            job_id = self._create_job_row(job_type, params)
        self._pump()
        return job_id

    def _create_job_row(self, job_type: str, params: dict) -> int:
        # root_id is the root the job *concerns* (for the per-root history/TUI, §12) —
        # taken from params.root_id when present (scan <root>/dedup/cleanup/merge-dest);
        # NULL for scan --all / untrash / trash-refresh. Distinct from owned_root
        # (exclusivity), which is narrower (e.g. only the perceptual-cleanup analyze).
        root_id = params.get("root_id")
        cur = self.db.execute(
            "INSERT INTO jobs(type, root_id, status, total, done, enqueued_at, params_json) "
            "VALUES (?, ?, 'queued', NULL, 0, ?, ?)",
            (job_type, root_id, now_iso(), json.dumps(params)),
        )
        return int(cur.lastrowid)

    # -- scheduling (pump) ----------------------------------------------
    def _pump(self) -> None:
        """Start the first runnable queued job if the worker is free (§3).

        Runnable-first, FIFO by ``enqueued_at``: scan the backlog oldest-first and
        launch the first job whose owned root is free (or that owns no root); skip
        (leave ``queued``) any whose owned root is held by a pending review / open
        merge. Called on submit, after every job finishes, and on startup. A no-op
        while a job is already running (the finishing job re-pumps).
        """
        with self._lock:
            if self._running_job_id is not None:
                return
            queued = self._safe_query(
                "SELECT id, type, params_json FROM jobs WHERE status='queued' "
                "ORDER BY enqueued_at, id"
            )
            for row in queued:
                spec = get_job_spec(row["type"])
                if spec is None:
                    # Unknown type left in the backlog (code removed a job type):
                    # fail it terminally rather than wedge the pump.
                    self._finish(row["id"], "error", error=f"unknown job type: {row['type']}")
                    self._broadcast(ProgressEvent(row["id"], "error", status="error",
                                                  message=f"unknown job type: {row['type']}"))
                    self._close_subscribers(row["id"])
                    continue
                params = self._parse_params(row["params_json"])
                if not self._is_runnable(spec, params):
                    continue  # owned root held — skip, retry on a later pump
                self._start(row["id"], spec, params)
                return

    def _is_runnable(self, spec, params: dict) -> bool:
        """True if this job's owned root is free (or it owns none) — §3 guarantee 2."""
        if spec.owned_root is None:
            return True
        root_id = spec.owned_root(params)
        if root_id is None:
            return True
        return self._root_holder(root_id, ignore_merge=spec.ignore_merge_holder) is None

    def _start(self, job_id: int, spec, params: dict) -> None:
        """Take the in-memory slot, flip the row to ``running``, launch the worker.

        Caller holds ``self._lock``.
        """
        self.db.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE id=?",
            (now_iso(), job_id),
        )
        cancel_event = threading.Event()
        self._running_job_id = job_id
        self._cancel_event = cancel_event
        worker = threading.Thread(
            target=self._run_job,
            args=(job_id, spec, params, cancel_event),
            name=f"packrat-job-{job_id}",
            daemon=True,
        )
        self._worker = worker
        worker.start()

    @staticmethod
    def _parse_params(params_json) -> dict:
        try:
            return json.loads(params_json) if params_json else {}
        except (ValueError, TypeError):
            return {}

    # -- worker ----------------------------------------------------------
    def _run_job(self, job_id: int, spec, params: dict, cancel_event: threading.Event) -> None:
        # Snapshot config at job start (§9.2 per-job reload). A parse error here
        # rejects the job cleanly rather than crashing the worker.
        try:
            config = self._config_loader()
        except Exception as exc:  # ConfigError and friends
            log.warning("job %d rejected: bad config: %s", job_id, exc)
            self._finish(job_id, "error", error=f"config error: {exc}")
            self._broadcast(ProgressEvent(job_id, "error", status="error", message=str(exc)))
            self._close_subscribers(job_id)  # release any attached SSE stream
            self._release(job_id)
            self._pump()
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
            self._finish(job_id, "cancelled", error=None, result=ctx.result)
            self._broadcast(ProgressEvent(job_id, "state", status="cancelled"))
        except Exception as exc:  # noqa: BLE001 - jobs must never crash the daemon
            log.exception("job %d failed", job_id)
            self._finish(job_id, "error", error=str(exc), result=ctx.result)
            self._broadcast(ProgressEvent(job_id, "error", status="error", message=str(exc)))
        else:
            self._finish(job_id, "done", error=None, result=ctx.result)
            self._broadcast(ProgressEvent(job_id, "done", status="done"))
        finally:
            self._close_subscribers(job_id)
            self._release(job_id)
            # Pump: start the next runnable job AND unblock anything that was
            # waiting on a root this job just released (a --confirm/--cancel frees
            # its review_run here). One pump does both (§3).
            self._pump()

    def _persist_progress(self, job_id: int, done: int, total: int | None) -> None:
        if total is None:
            self._safe_write("UPDATE jobs SET done=? WHERE id=?", (done, job_id))
        else:
            self._safe_write(
                "UPDATE jobs SET done=?, total=? WHERE id=?", (done, total, job_id)
            )

    def _finish(self, job_id: int, status: str, *, error: str | None,
                result: dict | None = None) -> None:
        # result_json is the uniform outcome summary (§4) — written for EVERY terminal
        # status. NULL when the job set none (its status/error still record the outcome).
        self._safe_write(
            "UPDATE jobs SET status=?, finished_at=?, error=?, result_json=? WHERE id=?",
            (status, now_iso(), error,
             json.dumps(result) if result is not None else None, job_id),
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

    def _safe_query(self, sql: str, params: tuple = ()) -> list:
        """Read, tolerating a DB closed during shutdown (see :meth:`_safe_write`).

        The pump-on-finish fires as a job completes, which can race a daemon
        teardown that already closed the shared connection. Return an empty
        backlog rather than crash the worker thread.
        """
        import sqlite3

        try:
            return self.db.query(sql, params)
        except sqlite3.ProgrammingError as exc:
            if "closed database" in str(exc).lower():
                log.debug("db closed during shutdown; skipping pump")
                return []
            raise

    def _release(self, job_id: int) -> None:
        with self._lock:
            if self._running_job_id == job_id:
                self._running_job_id = None
                self._cancel_event = None

    # -- cancellation ----------------------------------------------------
    def cancel(self, job_id: int) -> bool:
        """Cancel ``job_id`` — the running job (cooperative) or a queued one (drop) (§3).

        - **Running** → set the cancel flag; the worker observes it at its next
          checkpoint and lands the job ``cancelled`` (terminal), distinct from
          ``interrupted``.
        - **Queued** (runnable or blocked) → drop it from the backlog immediately:
          it never ran, so mark it ``cancelled`` right here (nothing to checkpoint).

        Returns True if a job was actually cancelled/dropped.
        """
        with self._lock:
            if self._running_job_id == job_id and self._cancel_event is not None:
                self._cancel_event.set()
                self._broadcast(ProgressEvent(job_id, "log", message="cancel requested"))
                return True
            row = self.db.query_one("SELECT status FROM jobs WHERE id=?", (job_id,))
            if row is not None and row["status"] == "queued":
                self.db.execute(
                    "UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?",
                    (now_iso(), job_id),
                )
                self._broadcast(ProgressEvent(job_id, "state", status="cancelled"))
                self._close_subscribers(job_id)
                return True
        return False

    def cancel_all_queued(self) -> int:
        """Drop every ``queued`` job from the backlog (TUI ``[x]``, §12).

        Leaves the running job alone. Returns the number dropped.
        """
        with self._lock:
            rows = self.db.query("SELECT id FROM jobs WHERE status='queued'")
            for r in rows:
                self.db.execute(
                    "UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?",
                    (now_iso(), r["id"]),
                )
                self._broadcast(ProgressEvent(r["id"], "state", status="cancelled"))
                self._close_subscribers(r["id"])
            return len(rows)

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

    def pump(self) -> None:
        """Public pump — start the next runnable queued job if the worker is free.

        Called by the daemon on startup (after reconciliation drains stale state) so a
        durable backlog left by a previous run begins draining without waiting for a
        new submission (§3). Safe to call anytime; a no-op while a job runs.
        """
        self._pump()

    # -- introspection ---------------------------------------------------
    def running_job_id(self) -> int | None:
        with self._lock:
            return self._running_job_id

    def blocked_reason(self, job_type: str, params: dict) -> dict | None:
        """Why a queued job of ``(job_type, params)`` can't run yet, or None (§3/§12).

        None → runnable (``queued · waiting for worker``). Otherwise the returned
        holder dict (from :meth:`_root_holder`) drives the ``blocked: root R has a
        pending <run> …`` label. Pure read — used by the status/queue snapshots.
        """
        spec = get_job_spec(job_type)
        if spec is None or spec.owned_root is None:
            return None
        root_id = spec.owned_root(params)
        if root_id is None:
            return None
        return self._root_holder(root_id, ignore_merge=spec.ignore_merge_holder)

    def _root_holder(self, root_id: int, *, ignore_merge: bool = False) -> dict | None:
        """Description of the op currently owning ``root_id``, or None (§3 guarantee 2).

        Delegates to the shared :func:`packrat.roots.root_holder` (a pending
        review_run or open merge_run, per §4) so the dequeue gate and the
        ``scan --all`` skip-log speak identically. ``ignore_merge`` lets a resuming
        merge past its own open ``merge_runs`` row (§8 C). Imported lazily to avoid a cycle.
        """
        from ..roots import root_holder

        return root_holder(self.db, root_id, ignore_merge=ignore_merge)

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
