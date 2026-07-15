"""Job queue: submit, progress, busy rejection, cancel, reconciliation (§3)."""

from __future__ import annotations

import time

import pytest

from packrat import db
from packrat.jobs import BusyError, JobQueue
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
        if row and row["status"] != "running":
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


def test_busy_rejection(queue_and_db):
    q, _ = queue_and_db
    q.submit("sleeper", {"steps": 50, "delay_s": 0.05})
    with pytest.raises(BusyError) as ei:
        q.submit("sleeper", {"steps": 2})
    assert ei.value.kind == "global"


def test_cooperative_cancel(queue_and_db):
    q, database = queue_and_db
    jid = q.submit("sleeper", {"steps": 200, "delay_s": 0.02})
    time.sleep(0.1)
    assert q.cancel(jid) is True
    assert _wait_terminal(database, jid) == "cancelled"


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
