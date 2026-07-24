r"""Periodic-job scheduler (§3) — the daemon-owned APScheduler the plan slated.

§3 lists "Scheduler (APScheduler → interval scans)" as a daemon responsibility; this
realizes it — **general** enough for future periodic work (scheduled ``--full`` scans
per §13 M8, audit pruning §8.1, embedding backfills §7), with **probe** (§8 A2b) as
its first client.

Design — a thin :class:`PeriodicScheduler` wrapper + a declarative
:class:`PeriodicTask` registry **over** APScheduler's ``BackgroundScheduler`` (so tasks
stay declarative and the engine is swappable; the registry mirrors the ``JobSpec``
pattern):

- **A ``PeriodicTask``** declares its ``name``, a ``submit(queue, db)`` thunk that
  enqueues the work (the fan-out lives here), a ``trigger(config)`` builder (→ an
  APScheduler trigger, so cadence is config-tunable), and an ``enabled(config)`` gate.
- **The probe task's ``submit``** does the fan-out: query enabled *library* roots and
  ``queue.submit("probe", {"root_id": r})`` per root. The queue's submit-dedup (§8 A2b)
  makes re-firing before the last batch drained a no-op, so the scheduler stays generic
  — probe's "one job per root" policy lives in the thunk + the dedup, not in APScheduler.
- **The scheduler is just another queue *client*.** APScheduler's job func runs on *its*
  own thread and only calls ``queue.submit(...)`` (enqueue + pump); it never runs job
  work itself, so the "one mutating job at a time" invariant (§3) is untouched.
- **In-memory jobstore (APScheduler default), NOT persistent.** The schedule is re-armed
  from :data:`PERIODIC_TASKS` on every daemon start; a tick missed while the daemon was
  down just runs at the next fire. Probe is cheap + idempotent, so a missed/extra tick
  is harmless — durability lives in the *job queue* (§3), the schedule itself is
  disposable. ``coalesce=True`` + a ``misfire_grace_time`` collapse a backlog of missed
  fires to one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import Config

log = logging.getLogger("packrat.jobs.scheduler")

#: A missed fire (daemon was down) runs up to this many seconds late before being
#: dropped; with ``coalesce=True`` a backlog of missed fires collapses to one run.
_MISFIRE_GRACE_S = 3600
#: Small per-job jitter (seconds) so a fan-out doesn't thundering-herd the queue at
#: the exact same instant — matters once many roots each enqueue a probe.
_JITTER_S = 300


@dataclass(frozen=True)
class PeriodicTask:
    """One declarative periodic job spec (mirrors ``JobSpec``, §3).

    ``submit`` enqueues the work through the normal queue (fan-out lives here);
    ``trigger`` builds an APScheduler trigger from config (so cadence is tunable);
    ``enabled`` is the config off-switch. The scheduler registers each enabled task as
    an APScheduler job whose func calls ``submit(queue, db)``.
    """

    name: str
    submit: Callable[["object", "object"], None]      # (queue, db) -> None
    trigger: Callable[[Config], object]               # (config) -> APScheduler trigger
    enabled: Callable[[Config], bool] = field(default=lambda _c: True)


# ---------------------------------------------------------------------------
# the probe-all task (§8 A2b) — probe every enabled library root
# ---------------------------------------------------------------------------
def submit_probe_all(queue, db) -> None:
    """Fan-out: submit one ``probe <root>`` per enabled **library** root (§8 A2b).

    Skips ``kind='trash'`` roots (scan/probe never touch trash — §6.1). Each submission
    gets its own queue entry + dequeue gate; the queue's submit-dedup collapses a
    re-fire before the previous batch drained to a no-op (one queued probe per root).
    A plain function ``(queue, db) -> None`` so it is unit-testable without APScheduler.
    """
    rows = db.query("SELECT id, name FROM roots WHERE enabled=1 AND kind='library' ORDER BY id")
    for r in rows:
        queue.submit("probe", {"root_id": r["id"]})
    log.info("scheduled probe sweep: submitted %d per-root probe(s)", len(rows))


def _probe_trigger(config: Config):
    """An ``IntervalTrigger`` at ``schedule.probe_interval_hours`` (+ jitter), from config."""
    from apscheduler.triggers.interval import IntervalTrigger

    hours = max(0.001, float(config.schedule.probe_interval_hours))
    return IntervalTrigger(hours=hours, jitter=_JITTER_S)


PROBE_ALL_TASK = PeriodicTask(
    name="probe-all",
    submit=submit_probe_all,
    trigger=_probe_trigger,
    enabled=lambda c: c.schedule.probe_enabled,
)

#: The registry of periodic tasks (mirrors the JobSpec registry). Add future periodic
#: work (scheduled --full scans, audit pruning, embedding backfills) here — no scheduler
#: change, just a new PeriodicTask.
PERIODIC_TASKS: list[PeriodicTask] = [PROBE_ALL_TASK]


class PeriodicScheduler:
    """Daemon-owned wrapper over APScheduler's ``BackgroundScheduler`` (§3).

    Registers each *enabled* :class:`PeriodicTask` as an APScheduler job whose func calls
    ``task.submit(queue, db)`` through the normal queue. Owns its own daemon thread (the
    ``BackgroundScheduler``), so it slots into the daemon lifecycle exactly where
    ``JobQueue``'s thread does — ``start()`` in the startup hook, ``shutdown()`` in the
    shutdown hook (:mod:`packrat.daemon.server`).
    """

    def __init__(self, queue, db, config, tasks=PERIODIC_TASKS):
        self._queue = queue
        self._db = db
        self._config = config
        self._tasks = tasks
        self._scheduler = None   # lazily built in start() so import stays cheap

    def _make_scheduler(self):
        # In-memory jobstore (the default) — the schedule is disposable + re-armed each
        # start (§3). coalesce=True collapses missed fires; misfire grace tolerates a
        # daemon that was down over a fire instant.
        from apscheduler.schedulers.background import BackgroundScheduler

        return BackgroundScheduler(
            job_defaults={"coalesce": True, "misfire_grace_time": _MISFIRE_GRACE_S},
        )

    def _run_task(self, task: PeriodicTask) -> None:
        """APScheduler job func — runs on the scheduler's thread; only enqueues (never
        runs job work), so §3's single-worker invariant is untouched."""
        try:
            task.submit(self._queue, self._db)
        except Exception:  # noqa: BLE001 - a task error must not kill the scheduler thread
            log.exception("periodic task %r failed to submit", task.name)

    def start(self) -> None:
        """Register each enabled task + start the background scheduler (§3 startup hook).

        A task whose ``enabled(config)`` is false is simply not registered (its
        off-switch). Safe if APScheduler is unavailable — logs and no-ops rather than
        blocking daemon startup."""
        try:
            self._scheduler = self._make_scheduler()
        except Exception:  # noqa: BLE001 - never block daemon startup on the scheduler
            log.exception("could not build the periodic scheduler; periodic jobs disabled")
            self._scheduler = None
            return
        armed = 0
        for task in self._tasks:
            if not task.enabled(self._config):
                log.info("periodic task %r disabled by config — not scheduled", task.name)
                continue
            self._scheduler.add_job(
                self._run_task, trigger=task.trigger(self._config),
                args=(task,), id=task.name, replace_existing=True,
            )
            armed += 1
        self._scheduler.start()
        log.info("periodic scheduler started (%d task(s) armed)", armed)

    def shutdown(self) -> None:
        """Stop the background scheduler (§3 shutdown hook) — symmetric with
        ``JobQueue.shutdown()``. ``wait=False`` so a graceful daemon stop isn't held up."""
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001 - already stopped / never started
                log.debug("periodic scheduler shutdown was a no-op")
            self._scheduler = None
