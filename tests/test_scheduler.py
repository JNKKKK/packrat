"""Periodic scheduler (§3) — probe-all fan-out + PeriodicScheduler wiring (§8 A2b).

Two layers, tested independently:
- the probe-all task's ``submit`` is a plain ``(queue, db) -> None`` — tested with a
  spy queue + a real DB, no APScheduler, no real-time wait;
- :class:`PeriodicScheduler` registers each enabled task and, when its func fires,
  enqueues through the queue — tested by invoking the registered func directly (no
  real-time wait) with a spy queue.
"""

from __future__ import annotations

from packrat import db
from packrat.config import Config, ScheduleConfig
from packrat.jobs.scheduler import (
    PERIODIC_TASKS, PROBE_ALL_TASK, PeriodicScheduler, PeriodicTask, submit_probe_all,
)
from packrat.roots import register


class _SpyQueue:
    """Records ``submit(type, params)`` calls; returns a fake incrementing job id."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def submit(self, job_type: str, params: dict) -> int:
        self.calls.append((job_type, params))
        return len(self.calls)


def _db(packrat_home):
    db.init_db().close()
    conn = db.connect(check_same_thread=False)
    return db.Database(conn)


# ---------------------------------------------------------------------------
# probe-all fan-out (a plain function — no APScheduler)
# ---------------------------------------------------------------------------
def test_probe_all_submits_one_probe_per_library_root(packrat_home, tmp_path):
    d = _db(packrat_home)
    try:
        ids = []
        for n in ("a", "b", "c"):
            p = tmp_path / n
            p.mkdir()
            ids.append(register(d, str(p))["id"])
        spy = _SpyQueue()
        submit_probe_all(spy, d)
        assert [c[0] for c in spy.calls] == ["probe", "probe", "probe"]
        assert sorted(c[1]["root_id"] for c in spy.calls) == sorted(ids)
    finally:
        d.close()


def test_probe_all_skips_trash_and_disabled_roots(packrat_home, tmp_path):
    d = _db(packrat_home)
    try:
        lib = tmp_path / "lib"
        lib.mkdir()
        libid = register(d, str(lib))["id"]
        trash = tmp_path / "trash"
        trash.mkdir()
        register(d, str(trash), kind="trash")               # trash → never probed (§6.1)
        disabled = tmp_path / "off"
        disabled.mkdir()
        offid = register(d, str(disabled))["id"]
        d.execute("UPDATE roots SET enabled=0 WHERE id=?", (offid,))  # disabled → skipped
        spy = _SpyQueue()
        submit_probe_all(spy, d)
        assert [c[1]["root_id"] for c in spy.calls] == [libid]
    finally:
        d.close()


# ---------------------------------------------------------------------------
# PeriodicScheduler wiring (fire the registered func directly — no real-time wait)
# ---------------------------------------------------------------------------
def test_scheduler_fires_task_through_the_queue(packrat_home, tmp_path):
    """The scheduler registers the task and, when fired, enqueues through the queue —
    asserted by driving the registered func directly (no APScheduler real-time wait)."""
    d = _db(packrat_home)
    try:
        lib = tmp_path / "lib"
        lib.mkdir()
        libid = register(d, str(lib))["id"]
        spy = _SpyQueue()
        sched = PeriodicScheduler(spy, d, Config())
        sched.start()
        try:
            job = sched._scheduler.get_job("probe-all")
            assert job is not None, "the enabled probe-all task must be registered"
            # Fire the job's func exactly as APScheduler would (its args carry the task).
            job.func(*job.args)
            assert [c[0] for c in spy.calls] == ["probe"]
            assert spy.calls[0][1]["root_id"] == libid
        finally:
            sched.shutdown()
    finally:
        d.close()


def test_scheduler_respects_the_enabled_gate(packrat_home):
    """A task whose config gate is off is NOT registered (probe_enabled=false)."""
    d = _db(packrat_home)
    try:
        cfg = Config(schedule=ScheduleConfig(probe_enabled=False))
        sched = PeriodicScheduler(_SpyQueue(), d, cfg)
        sched.start()
        try:
            assert sched._scheduler.get_job("probe-all") is None
        finally:
            sched.shutdown()
    finally:
        d.close()


def test_scheduler_task_error_does_not_propagate(packrat_home):
    """A task whose submit raises must not kill the scheduler thread (logged + swallowed)."""
    d = _db(packrat_home)
    try:
        def _boom(queue, db_):
            raise RuntimeError("nope")

        task = PeriodicTask(name="boom", submit=_boom,
                            trigger=PROBE_ALL_TASK.trigger)
        sched = PeriodicScheduler(_SpyQueue(), d, Config(), tasks=[task])
        sched.start()
        try:
            job = sched._scheduler.get_job("boom")
            job.func(*job.args)   # must NOT raise
        finally:
            sched.shutdown()
    finally:
        d.close()


def test_probe_all_is_the_registered_task():
    """The registry ships the probe-all task (its first client — §8 A2b)."""
    assert PROBE_ALL_TASK in PERIODIC_TASKS
    assert PROBE_ALL_TASK.name == "probe-all"
