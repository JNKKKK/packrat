"""Schema constraints: CASCADE, partial-unique review/merge, edge ordering (§4)."""

from __future__ import annotations

import sqlite3

import pytest

from packrat import db
from packrat.util import now_iso


@pytest.fixture()
def conn(packrat_home):
    c = db.init_db()
    yield c
    c.close()


def _mk_root(conn, name="R", kind="library"):
    cur = conn.execute(
        "INSERT INTO roots(path,name,kind) VALUES(?,?,?)",
        (f"/{name}", name, kind),
    )
    conn.commit()
    return cur.lastrowid


def _mk_asset(conn, h, status="active", media="photo"):
    cur = conn.execute(
        "INSERT INTO assets(content_hash,media_type,status) VALUES(?,?,?)",
        (h, media, status),
    )
    conn.commit()
    return cur.lastrowid


def test_all_tables_created(conn):
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in (
        "roots", "assets", "file_instances", "phash", "vphash", "embeddings",
        "similarity_edges", "review_runs", "review_actions", "merge_runs",
        "merge_plan_items", "jobs", "meta",
    ):
        assert t in tables
    assert db.schema_version(conn) == db.SCHEMA_VERSION


def test_jobs_priority_defaults_to_zero(conn):
    """jobs.priority exists and defaults to 0 (normal FIFO)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "priority" in cols
    conn.execute("INSERT INTO jobs(type,status) VALUES('scan','queued')")
    conn.commit()
    assert conn.execute("SELECT priority FROM jobs").fetchone()["priority"] == 0


def test_init_db_is_idempotent(packrat_home, tmp_path):
    """init_db on an already-current DB is a no-op (CREATE … IF NOT EXISTS): the
    schema + data survive and the version stays put — there is no migration runner."""
    dbfile = tmp_path / "cat.db"
    c1 = db.init_db(dbfile)
    c1.execute("INSERT INTO roots(path,name,kind,enabled) VALUES ('X:/p','p','library',1)")
    c1.commit()
    c1.close()
    c2 = db.init_db(dbfile)                       # re-init: must not wipe or error
    try:
        assert c2.execute("SELECT COUNT(*) c FROM roots").fetchone()["c"] == 1
        assert db.schema_version(c2) == db.SCHEMA_VERSION
    finally:
        c2.close()


def test_nested_execute_inside_transaction_is_atomic(conn):
    """A db.execute() nested inside `with db.transaction()` must NOT commit early —
    a later exception rolls the WHOLE unit back (regression: execute auto-committed,
    so the pre-exception write survived a rollback)."""
    d = db.Database(conn)
    d.execute("CREATE TABLE t_atomic (x INTEGER)")
    with pytest.raises(RuntimeError):
        with d.transaction() as c:
            c.execute("INSERT INTO t_atomic(x) VALUES (1)")
            d.execute("INSERT INTO t_atomic(x) VALUES (2)")   # nested wrapper call
            raise RuntimeError("boom")                        # must roll BOTH back
    assert d.query_one("SELECT COUNT(*) c FROM t_atomic")["c"] == 0


def test_nested_transaction_commits_once(conn):
    """A nested transaction() joins the outer one — the outermost commits once."""
    d = db.Database(conn)
    d.execute("CREATE TABLE t_nest (x INTEGER)")
    with d.transaction() as c:
        c.execute("INSERT INTO t_nest(x) VALUES (1)")
        with d.transaction() as c2:                           # joins the outer txn
            c2.execute("INSERT INTO t_nest(x) VALUES (2)")
    assert d.query_one("SELECT COUNT(*) c FROM t_nest")["c"] == 2


def test_read_only_open_of_non_wal_db(tmp_path):
    """A read-only open must NOT run the WAL/synchronous WRITE pragmas — otherwise it
    raises OperationalError on a DB left in delete-journal mode (restore/copy)."""
    dbfile = tmp_path / "delete_journal.db"
    c = sqlite3.connect(dbfile)
    c.execute("PRAGMA journal_mode=DELETE")
    c.execute("CREATE TABLE t(x)")
    c.commit()
    c.close()
    ro = db.connect(dbfile, read_only=True)   # must not raise
    try:
        assert ro.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        ro.close()


def test_asset_delete_cascades(conn):
    rid = _mk_root(conn)
    aid = _mk_asset(conn, "h1")
    conn.execute("INSERT INTO file_instances(asset_id,root_id,path) VALUES(?,?,'/R/a.jpg')", (aid, rid))
    conn.execute("INSERT INTO phash(asset_id,bits) VALUES(?, x'00')", (aid,))
    conn.commit()
    conn.execute("DELETE FROM assets WHERE id=?", (aid,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) c FROM file_instances").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM phash").fetchone()["c"] == 0


def test_one_pending_review_per_root(conn):
    rid = _mk_root(conn)
    conn.execute(
        "INSERT INTO review_runs(root_id,run_type,status,created_at) VALUES(?,'dedup','pending',?)",
        (rid, now_iso()),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO review_runs(root_id,run_type,status,created_at) "
            "VALUES(?,'cleanup-perceptual','pending',?)",
            (rid, now_iso()),
        )
        conn.commit()
    conn.rollback()
    # a completed run coexists fine
    conn.execute(
        "INSERT INTO review_runs(root_id,run_type,status,created_at) VALUES(?,'dedup','completed',?)",
        (rid, now_iso()),
    )
    conn.commit()


def test_one_open_merge_per_dest_root(conn):
    rid = _mk_root(conn)
    conn.execute(
        "INSERT INTO merge_runs(source_path,dest_path,dest_root_id,status,created_at) "
        "VALUES('/s','/R/d',?,'planning',?)",
        (rid, now_iso()),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO merge_runs(source_path,dest_path,dest_root_id,status,created_at) "
            "VALUES('/s2','/R/d2',?,'copying',?)",
            (rid, now_iso()),
        )
        conn.commit()
    conn.rollback()


def test_similarity_edge_requires_canonical_order(conn):
    a = _mk_asset(conn, "ha")
    b = _mk_asset(conn, "hb")
    lo, hi = min(a, b), max(a, b)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO similarity_edges(asset_a,asset_b,media_type,algo) VALUES(?,?,'photo','pdq')",
            (hi, lo),
        )
        conn.commit()
    conn.rollback()
    # canonical order accepted
    conn.execute(
        "INSERT INTO similarity_edges(asset_a,asset_b,media_type,algo) VALUES(?,?,'photo','pdq')",
        (lo, hi),
    )
    conn.commit()


def test_root_name_unique_case_insensitive(conn):
    _mk_root(conn, "iPhone")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO roots(path,name,kind) VALUES('/other','IPHONE','library')")
        conn.commit()
    conn.rollback()
