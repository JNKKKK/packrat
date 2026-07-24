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
from packrat.jobs import scheduler as sched_mod
from packrat.jobs.scheduler import (
    PERIODIC_TASKS, PROBE_ALL_TASK, PeriodicScheduler, PeriodicTask, _probe_trigger,
    submit_probe_all,
)
from packrat.roots import enabled_library_root_ids, register


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


def test_scheduler_start_swallows_a_bad_trigger(packrat_home):
    """A task whose trigger(config) raises must NOT propagate out of start() (which runs
    in the FastAPI startup hook — a raise there crashes daemon boot). Regression: only
    _make_scheduler() was guarded; arming ran unguarded."""
    d = _db(packrat_home)
    try:
        def _bad_trigger(config):
            raise ValueError("bad cadence")

        task = PeriodicTask(name="boom-trigger", submit=submit_probe_all,
                            trigger=_bad_trigger)
        sched = PeriodicScheduler(_SpyQueue(), d, Config(), tasks=[task])
        sched.start()      # must NOT raise
        try:
            # The bad task is skipped, but the scheduler still started up.
            assert sched._scheduler is not None
            assert sched._scheduler.get_job("boom-trigger") is None
        finally:
            sched.shutdown()
    finally:
        d.close()


def test_scheduler_start_skips_one_bad_task_but_arms_the_rest(packrat_home, tmp_path):
    """One task that fails to arm must not drop a healthy sibling (independent guards)."""
    d = _db(packrat_home)
    try:
        lib = tmp_path / "lib"
        lib.mkdir()
        register(d, str(lib))

        def _bad_enabled(config):
            raise RuntimeError("gate blew up")

        bad = PeriodicTask(name="bad", submit=submit_probe_all,
                           trigger=PROBE_ALL_TASK.trigger, enabled=_bad_enabled)
        sched = PeriodicScheduler(_SpyQueue(), d, Config(), tasks=[bad, PROBE_ALL_TASK])
        sched.start()      # must NOT raise
        try:
            assert sched._scheduler.get_job("bad") is None        # skipped
            assert sched._scheduler.get_job("probe-all") is not None  # sibling still armed
        finally:
            sched.shutdown()
    finally:
        d.close()


def test_probe_all_uses_the_shared_sweep_set(packrat_home, tmp_path):
    """The scheduler fan-out targets EXACTLY roots.enabled_library_root_ids — the same set
    the daemon's /probe --all endpoint uses (§8 A2b), so scheduled + manual sweeps can't
    drift. Asserts the thunk's submitted ids equal the shared helper's output."""
    d = _db(packrat_home)
    try:
        for n in ("a", "b"):
            p = tmp_path / n
            p.mkdir()
            register(d, str(p))
        trash = tmp_path / "t"
        trash.mkdir()
        register(d, str(trash), kind="trash")     # excluded by the shared helper
        spy = _SpyQueue()
        submit_probe_all(spy, d)
        assert [c[1]["root_id"] for c in spy.calls] == enabled_library_root_ids(d)
    finally:
        d.close()


def test_probe_trigger_floors_near_zero_interval():
    """A misconfigured near-zero probe_interval_hours (0 meant as 'off') is clamped to the
    minimum interval, so it can't fire a fan-out every few seconds (§6 footgun fix)."""
    cfg = Config(schedule=ScheduleConfig(probe_interval_hours=0.0))
    trig = _probe_trigger(cfg)
    # IntervalTrigger stores its period as a timedelta; assert it hit the floor.
    assert trig.interval.total_seconds() == sched_mod._MIN_INTERVAL_S
    # And the jitter never exceeds the (clamped) interval — no window overrun.
    assert trig.jitter <= sched_mod._MIN_INTERVAL_S


def test_probe_trigger_respects_a_sane_interval():
    """A normal cadence passes through as configured (hours → seconds), jitter uncapped."""
    cfg = Config(schedule=ScheduleConfig(probe_interval_hours=24.0))
    trig = _probe_trigger(cfg)
    assert trig.interval.total_seconds() == 24 * 3600
    assert trig.jitter == sched_mod._JITTER_S


def test_probe_all_is_the_registered_task():
    """The registry ships the probe-all task (its first client — §8 A2b)."""
    assert PROBE_ALL_TASK in PERIODIC_TASKS
    assert PROBE_ALL_TASK.name == "probe-all"
