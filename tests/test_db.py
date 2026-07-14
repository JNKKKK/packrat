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
