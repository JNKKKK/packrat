"""Job queue: submit, progress, durable FIFO queue, cancel, reconciliation (§3)."""

from __future__ import annotations

import time

import pytest

from packrat import db
from packrat.jobs import JobQueue
from packrat.jobs.reconcile import reconcile_on_startup
from packrat.util import now_iso

# The test-only 'sleeper' job is registered in conftest.py.


@pytest.fixture()
def queue_and_db(packrat_home):
    db.init_db().close()
    conn = db.connect(check_same_thread=False)
    d = db.Database(conn)
    q = JobQueue(d)
    yield q, d
    q.shutdown()  # cancel + join any running worker before closing the DB
    d.close()


@pytest.fixture()
def database(queue_and_db):
    return queue_and_db[1]


def _wait_terminal(database, job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = database.query_one("SELECT status FROM jobs WHERE id=?", (job_id,))
        if row and row["status"] not in ("queued", "running"):
            return row["status"]
        time.sleep(0.02)
    raise AssertionError("job did not terminate")


def test_submit_runs_and_reports_progress(queue_and_db):
    q, database = queue_and_db
    sub_id = q.submit("sleeper", {"steps": 4, "delay_s": 0.01})
    sub = q.subscribe(sub_id)
    progress = 0
    while True:
        ev = sub.q.get(timeout=5)
        if ev is None:
            break
        if ev.type == "progress":
            progress += 1
    assert progress >= 4
    row = database.query_one("SELECT status, done, total FROM jobs WHERE id=?", (sub_id,))
    assert row["status"] == "done"
    assert row["done"] == 4 and row["total"] == 4


def test_second_submit_enqueues(queue_and_db):
    """§3 guarantee 1: a submit while busy is QUEUED (durable backlog), not rejected."""
    q, database = queue_and_db
    q.submit("sleeper", {"steps": 50, "delay_s": 0.05})
    jid2 = q.submit("sleeper", {"steps": 2})
    # jid2 waits in the backlog behind the running job.
    assert database.query_one("SELECT status FROM jobs WHERE id=?", (jid2,))["status"] == "queued"


def test_backlog_drains_in_fifo_order(queue_and_db):
    """Queued jobs run one at a time, oldest-first, as the worker frees (§3)."""
    q, database = queue_and_db
    a = q.submit("sleeper", {"steps": 3, "delay_s": 0.02})
    b = q.submit("sleeper", {"steps": 3, "delay_s": 0.02})
    c = q.submit("sleeper", {"steps": 3, "delay_s": 0.02})
    for jid in (a, b, c):
        assert _wait_terminal(database, jid) == "done"
    # started_at ordering reflects FIFO drain (a before b before c).
    rows = {r["id"]: r["started_at"] for r in
            database.query("SELECT id, started_at FROM jobs WHERE id IN (?,?,?)", (a, b, c))}
    assert rows[a] <= rows[b] <= rows[c]


def test_cancel_queued_drops_from_backlog(queue_and_db):
    """Cancelling a still-queued job marks it cancelled without ever running it (§3)."""
    q, database = queue_and_db
    q.submit("sleeper", {"steps": 50, "delay_s": 0.05})
    jid2 = q.submit("sleeper", {"steps": 2})
    assert q.cancel(jid2) is True
    row = database.query_one("SELECT status, started_at FROM jobs WHERE id=?", (jid2,))
    assert row["status"] == "cancelled" and row["started_at"] is None


def test_cooperative_cancel(queue_and_db):
    q, database = queue_and_db
    jid = q.submit("sleeper", {"steps": 200, "delay_s": 0.02})
    time.sleep(0.1)
    assert q.cancel(jid) is True
    assert _wait_terminal(database, jid) == "cancelled"


# ---------------------------------------------------------------------------
# prioritize (§3/§11) — bump a queued job to the front of the dequeue order
# ---------------------------------------------------------------------------
def test_prioritize_runs_next(queue_and_db):
    """A prioritized queued job jumps ahead of earlier-enqueued jobs and runs next (§3)."""
    q, database = queue_and_db
    q.submit("sleeper", {"steps": 30, "delay_s": 0.05})       # running
    a = q.submit("sleeper", {"steps": 2, "delay_s": 0.01})    # queued first
    b = q.submit("sleeper", {"steps": 2, "delay_s": 0.01})    # queued second
    # Bump b ahead of a while both are still queued behind the running job.
    assert q.prioritize(b) is True
    for jid in (a, b):
        assert _wait_terminal(database, jid) == "done"
    rows = {r["id"]: r["started_at"] for r in
            database.query("SELECT id, started_at FROM jobs WHERE id IN (?,?)", (a, b))}
    # b (prioritized) started before a (enqueued earlier) — priority beat FIFO.
    assert rows[b] <= rows[a]


def test_prioritize_is_durable(queue_and_db):
    """prioritize sets a durable priority column (survives a restart / re-pump)."""
    q, database = queue_and_db
    q.submit("sleeper", {"steps": 30, "delay_s": 0.05})       # running
    a = q.submit("sleeper", {"steps": 2})
    b = q.submit("sleeper", {"steps": 2})
    q.prioritize(b)
    pa = database.query_one("SELECT priority FROM jobs WHERE id=?", (a,))["priority"]
    pb = database.query_one("SELECT priority FROM jobs WHERE id=?", (b,))["priority"]
    assert pb > pa == 0


def test_prioritize_rejects_running_and_terminal(queue_and_db):
    """Only a queued job can be prioritized — a running/terminal one returns False (§11)."""
    q, database = queue_and_db
    jid = q.submit("sleeper", {"steps": 4, "delay_s": 0.02})  # runs immediately
    time.sleep(0.05)
    assert q.prioritize(jid) is False                          # it's running
    assert _wait_terminal(database, jid) == "done"
    assert q.prioritize(jid) is False                          # now terminal
    assert q.prioritize(999999) is False                       # unknown id


def test_prioritize_multiple_is_lifo(queue_and_db):
    """Prioritizing several jobs runs them last-prioritized-first (each `max+1` leapfrogs).

    Bump a, then b, then c → dequeue order is c, b, a (the most recent 'do this next'
    wins), and un-prioritized jobs stay behind them in FIFO. Locks in the documented
    multi-prioritize semantics against regression.
    """
    q, database = queue_and_db
    q.submit("sleeper", {"steps": 30, "delay_s": 0.05})       # running, occupies the worker
    a = q.submit("sleeper", {"steps": 2})
    b = q.submit("sleeper", {"steps": 2})
    c = q.submit("sleeper", {"steps": 2})
    d = q.submit("sleeper", {"steps": 2})                     # left un-prioritized
    for jid in (a, b, c):
        assert q.prioritize(jid) is True
    order = [r["id"] for r in database.query(
        "SELECT id FROM jobs WHERE status='queued' ORDER BY priority DESC, enqueued_at, id")]
    assert order == [c, b, a, d]                              # LIFO among bumped, then FIFO


def test_reconcile_orphaned_running(database):
    database.execute(
        "INSERT INTO jobs(type,status,total,done,started_at) VALUES('scan','running',100,42,?)",
        (now_iso(),),
    )
    summary = reconcile_on_startup(database)
    assert len(summary["interrupted_jobs"]) == 1
    row = database.query_one("SELECT status, error FROM jobs")
    assert row["status"] == "interrupted"
    assert row["error"] == "daemon restarted"
    # idempotent
    assert reconcile_on_startup(database)["interrupted_jobs"] == []


def test_unknown_job_type(queue_and_db):
    q, _ = queue_and_db
    with pytest.raises(ValueError):
        q.submit("nonexistent", {})


def test_reconcile_keeps_nondestructive_queued(database):
    """§3: a queued NON-destructive job (scan) survives a restart and stays queued."""
    database.execute(
        "INSERT INTO jobs(type,status,enqueued_at,params_json) "
        "VALUES('scan','queued',?, '{\"root_id\": 1}')",
        (now_iso(),),
    )
    summary = reconcile_on_startup(database)
    assert summary["carved_out_queued"] == []
    assert database.query_one("SELECT status FROM jobs")["status"] == "queued"


def test_reconcile_carves_out_queued_destructive_apply(database):
    """§3 carve-out: a queued dedup --confirm is flipped to interrupted, never auto-run."""
    database.execute(
        "INSERT INTO jobs(type,status,enqueued_at,params_json) "
        "VALUES('dedup','queued',?, '{\"root_id\": 1, \"confirm\": true}')",
        (now_iso(),),
    )
    summary = reconcile_on_startup(database)
    assert len(summary["carved_out_queued"]) == 1
    row = database.query_one("SELECT status, error FROM jobs")
    assert row["status"] == "interrupted"
    assert "not auto-run" in row["error"]


def test_config_error_closes_subscribers(packrat_home):
    """A job that dies on config load must close SSE subscribers (no stream hang)."""
    db.init_db().close()
    conn = db.connect(check_same_thread=False)
    d = db.Database(conn)

    def _bad_config():
        raise RuntimeError("boom")

    q = JobQueue(d, config_loader=_bad_config)
    try:
        jid = q.submit("sleeper", {"steps": 1})
        sub = q.subscribe(jid)
        # The sentinel (None) must arrive — else a streaming client blocks forever.
        seen_sentinel = False
        for _ in range(200):
            ev = sub.q.get(timeout=5)
            if ev is None:
                seen_sentinel = True
                break
        assert seen_sentinel
        assert _wait_terminal(d, jid) == "error"
    finally:
        q.shutdown()
        d.close()


def test_reconcile_carves_out_queued_cleanup_apply(database):
    """A queued one-shot cleanup apply (exact/undecodable delete) is also carved out."""
    database.execute(
        "INSERT INTO jobs(type,status,enqueued_at,params_json) "
        "VALUES('cleanup','queued',?, '{\"root_id\": 1, \"mode\": \"exact\", \"apply\": true}')",
        (now_iso(),),
    )
    summary = reconcile_on_startup(database)
    assert len(summary["carved_out_queued"]) == 1
    assert database.query_one("SELECT status FROM jobs")["status"] == "interrupted"
