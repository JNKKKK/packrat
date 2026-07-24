"""probe job (§8 A2b) — cheap discovery: count new/changed WITHOUT fingerprinting.

Drives the real probe handler through a ``JobQueue`` + ``Database`` against tiny
real PNGs (like test_scan), plus the submit-dedup + dequeue-gate behaviors. Requires
the ``media`` extra for the *scan* half of the "scan clears the count" test.
"""

from __future__ import annotations

import time

import pytest

from packrat import db
from packrat.jobs import JobQueue
from packrat.jobs import probe as _probe  # noqa: F401 - registers 'probe'
from packrat.jobs import scan as _scan    # noqa: F401 - registers 'scan'
from packrat.roots import register

pytest.importorskip("blake3")
pytest.importorskip("PIL")
pytest.importorskip("pdqhash")


@pytest.fixture()
def queue_and_db(packrat_home):
    db.init_db().close()
    conn = db.connect(check_same_thread=False)
    d = db.Database(conn)
    q = JobQueue(d)
    yield q, d
    q.shutdown()
    d.close()


def _wait_terminal(database, job_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = database.query_one("SELECT status, error FROM jobs WHERE id=?", (job_id,))
        if row and row["status"] not in ("queued", "running"):
            return row["status"], row["error"]
        time.sleep(0.02)
    raise AssertionError("job did not terminate")


def _run(q, database, job_type, params, timeout=30.0):
    jid = q.submit(job_type, params)
    status, error = _wait_terminal(database, jid, timeout)
    assert status == "done", f"{job_type} failed: {error}"
    return jid


def _fingerprint_counts(database):
    return {
        "assets": database.query_one("SELECT COUNT(*) c FROM assets")["c"],
        "instances": database.query_one("SELECT COUNT(*) c FROM file_instances")["c"],
        "phash": database.query_one("SELECT COUNT(*) c FROM phash")["c"],
        "vphash": database.query_one("SELECT COUNT(*) c FROM vphash")["c"],
    }


def _result(database, job_id):
    import json
    row = database.query_one("SELECT result_json FROM jobs WHERE id=?", (job_id,))
    return json.loads(row["result_json"]) if row and row["result_json"] else {}


# ---------------------------------------------------------------------------
# counts new-only, writes NO fingerprint rows
# ---------------------------------------------------------------------------
def test_probe_counts_new_and_writes_no_fingerprints(queue_and_db, tiny_photos):
    """A probe on a never-scanned root counts every candidate new + writes ZERO catalog
    rows (no assets/instances/phash) — only the per-root probe signal (§8 A2b)."""
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    before = _fingerprint_counts(database)

    jid = _run(q, database, "probe", {"root_id": root["id"]})

    after = _fingerprint_counts(database)
    assert after == before, "probe must not fingerprint anything"
    assert all(v == 0 for v in after.values())  # nothing scanned yet

    # tiny_photos has 3 media files (a.png, b.png, sub/a_copy.png); notes.txt is ignored.
    res = _result(database, jid)
    assert res["op"] == "probe" and res["root_offline"] is False
    assert res["new_count"] == 3 and res["candidates"] == 3
    row = database.query_one(
        "SELECT last_probe_at, probe_new_count FROM roots WHERE id=?", (root["id"],))
    assert row["probe_new_count"] == 3 and row["last_probe_at"] is not None


def test_probe_of_scanned_root_finds_nothing_new(queue_and_db, tiny_photos):
    """After a full scan, a probe finds 0 new/changed (the fast-path predicate matches)."""
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run(q, database, "scan", {"root_id": root["id"]})
    jid = _run(q, database, "probe", {"root_id": root["id"]})
    assert _result(database, jid)["new_count"] == 0
    # A found-nothing probe still stamps last_probe_at + writes count 0 (§8 A2b).
    row = database.query_one("SELECT last_probe_at, probe_new_count FROM roots WHERE id=?",
                             (root["id"],))
    assert row["probe_new_count"] == 0 and row["last_probe_at"] is not None


def test_probe_sees_a_new_file_after_scan(queue_and_db, tiny_photos):
    """probe says N ⇒ scan would fingerprint ≥ N: a file added after a scan is counted."""
    import numpy as np
    from PIL import Image

    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run(q, database, "scan", {"root_id": root["id"]})
    # Drop a new photo into the root; a probe must notice exactly it.
    arr = np.random.default_rng(99).integers(0, 256, (32, 32, 3), dtype=np.uint8)
    Image.fromarray(arr).save(tiny_photos / "new.png")
    jid = _run(q, database, "probe", {"root_id": root["id"]})
    assert _result(database, jid)["new_count"] == 1


# ---------------------------------------------------------------------------
# scan clears the count
# ---------------------------------------------------------------------------
def test_scan_clears_probe_new_count(queue_and_db, tiny_photos):
    """A completed scan CONSUMES the probe signal → probe_new_count back to 0 (§8 A2b)."""
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run(q, database, "probe", {"root_id": root["id"]})
    assert database.query_one("SELECT probe_new_count FROM roots WHERE id=?",
                              (root["id"],))["probe_new_count"] == 3
    _run(q, database, "scan", {"root_id": root["id"]})
    assert database.query_one("SELECT probe_new_count FROM roots WHERE id=?",
                              (root["id"],))["probe_new_count"] == 0


def test_dry_run_scan_does_not_clear_count(queue_and_db, tiny_photos):
    """A --dry-run scan changes nothing — it must not clear the probe signal (§8 A2b)."""
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run(q, database, "probe", {"root_id": root["id"]})
    _run(q, database, "scan", {"root_id": root["id"], "dry_run": True})
    assert database.query_one("SELECT probe_new_count FROM roots WHERE id=?",
                              (root["id"],))["probe_new_count"] == 3


# ---------------------------------------------------------------------------
# offline root writes nothing
# ---------------------------------------------------------------------------
def test_probe_offline_root_writes_no_signal(queue_and_db, tmp_path):
    """An unreadable/offline root must never be recorded as "0 new files" (§8 A2b/§10.1)."""
    q, database = queue_and_db
    missing = tmp_path / "lib"
    missing.mkdir()
    root = register(database, str(missing))
    # Simulate the root going offline AFTER registration by pointing its stored path at a
    # now-missing directory (enumerate's first listing fails → root_offline).
    database.execute("UPDATE roots SET path=? WHERE id=?",
                     (str(tmp_path / "gone"), root["id"]))
    jid = _run(q, database, "probe", {"root_id": root["id"]})
    res = _result(database, jid)
    assert res["root_offline"] is True and res["new_count"] is None
    row = database.query_one("SELECT last_probe_at, probe_new_count FROM roots WHERE id=?",
                             (root["id"],))
    assert row["last_probe_at"] is None and row["probe_new_count"] == 0  # untouched


# ---------------------------------------------------------------------------
# trash roots are never probed
# ---------------------------------------------------------------------------
def test_probe_rejects_trash_root(queue_and_db, tmp_path):
    """probe never inspects a trash root (§6.1) — the job errors."""
    q, database = queue_and_db
    tdir = tmp_path / "trash"
    tdir.mkdir()
    root = register(database, str(tdir), kind="trash")
    jid = q.submit("probe", {"root_id": root["id"]})
    status, error = _wait_terminal(database, jid)
    assert status == "error" and "trash" in error.lower()


# ---------------------------------------------------------------------------
# submit-dedup: one pending probe per root (but a 2nd scan enqueues freely)
# ---------------------------------------------------------------------------
def _two_roots(database, tmp_path):
    """Register two library roots (jobs.root_id FKs to roots.id), returning their ids."""
    a, b = tmp_path / "ra", tmp_path / "rb"
    a.mkdir()
    b.mkdir()
    r1 = register(database, str(a))
    r2 = register(database, str(b))
    return r1["id"], r2["id"]


def test_submit_dedup_skips_second_queued_probe(queue_and_db, tmp_path):
    """A 2nd probe for the same root while one is queued coalesces to the queued id (§8 A2b)."""
    q, database = queue_and_db
    rid, _ = _two_roots(database, tmp_path)
    q.submit("sleeper", {"steps": 200, "delay_s": 0.05})   # occupy the worker
    p1 = q.submit("probe", {"root_id": rid})
    p2 = q.submit("probe", {"root_id": rid})
    assert p1 == p2, "second queued probe for the same root must coalesce"
    n = database.query_one(
        "SELECT COUNT(*) c FROM jobs WHERE type='probe' AND status='queued' AND root_id=?",
        (rid,))["c"]
    assert n == 1


def test_submit_dedup_is_per_root(queue_and_db, tmp_path):
    """The probe dedup is per-root — a probe for a DIFFERENT root enqueues separately."""
    q, database = queue_and_db
    r1, r2 = _two_roots(database, tmp_path)
    q.submit("sleeper", {"steps": 200, "delay_s": 0.05})
    p1 = q.submit("probe", {"root_id": r1})
    p2 = q.submit("probe", {"root_id": r2})
    assert p1 != p2
    n = database.query_one(
        "SELECT COUNT(*) c FROM jobs WHERE type='probe' AND status='queued'")["c"]
    assert n == 2


def test_submit_dedup_is_probe_only(queue_and_db, tmp_path):
    """The dedup is probe-specific: a 2nd scan behind a first is intentional, NOT deduped."""
    q, database = queue_and_db
    rid, _ = _two_roots(database, tmp_path)
    q.submit("sleeper", {"steps": 200, "delay_s": 0.05})
    s1 = q.submit("scan", {"root_id": rid})
    s2 = q.submit("scan", {"root_id": rid})
    assert s1 != s2
    n = database.query_one(
        "SELECT COUNT(*) c FROM jobs WHERE type='scan' AND status='queued' AND root_id=?",
        (rid,))["c"]
    assert n == 2


# ---------------------------------------------------------------------------
# probe OWNS its root — dequeue gate holds it behind a pending review, runs when free
# ---------------------------------------------------------------------------
def test_probe_waits_for_busy_root(queue_and_db, tiny_photos):
    """probe owns its root, so a probe on a root with a pending review is held in the
    backlog (blocked), then runs once the holder clears (§3 dequeue gate / §8 A2b)."""
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    # Open a pending dedup review on the root → it "owns" the root.
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
        "VALUES (?, 'dedup', 'pending', 1, 'staged', 't')",
        (root["id"],),
    )
    jid = q.submit("probe", {"root_id": root["id"]})
    # Held: enqueued + blocked (not run), because its owned root is busy.
    assert database.query_one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "queued"
    assert q.blocked_reason("probe", {"root_id": root["id"]}) is not None

    # Clear the holder + pump → the probe now runs to completion.
    database.execute("UPDATE review_runs SET status='cancelled' WHERE root_id=?", (root["id"],))
    q.pump()
    status, error = _wait_terminal(database, jid)
    assert status == "done", error
    assert database.query_one("SELECT probe_new_count FROM roots WHERE id=?",
                              (root["id"],))["probe_new_count"] == 3


def test_submit_dedup_allows_fresh_probe_after_running(queue_and_db, tiny_photos):
    """A fresh queued probe AFTER one started running is legitimate (files may have arrived).

    Dedup matches only a QUEUED probe, never a running one — so a probe submitted while a
    prior probe is mid-run is NOT coalesced (§8 A2b)."""
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    # Start a real (slow-ish, but real) probe and, while it runs, submit another for the
    # same root behind a worker-occupying sleeper so the second one is queued, not deduped
    # against the running one. Use a blocker to hold the worker deterministically.
    q.submit("sleeper", {"steps": 200, "delay_s": 0.05})   # occupies the worker slot
    p1 = q.submit("probe", {"root_id": root["id"]})         # queued behind the sleeper
    # Flip p1 to 'running' to model "a probe already started" (dedup must ignore it).
    database.execute("UPDATE jobs SET status='running' WHERE id=?", (p1,))
    p2 = q.submit("probe", {"root_id": root["id"]})         # fresh queued probe
    assert p2 != p1, "a fresh probe must not dedup against a RUNNING probe"
