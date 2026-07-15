"""Scan-result persistence: scan_results + scan_problem_files + status read path."""

from __future__ import annotations

import time

import pytest

from packrat import db, queries
from packrat.jobs import JobQueue
from packrat.jobs import scan as _scan  # noqa: F401 - registers 'scan'
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


def _run_scan(q, database, root_id=None, **params):
    if root_id is not None:
        params["root_id"] = root_id
    jid = q.submit("scan", params)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        row = database.query_one("SELECT status, error FROM jobs WHERE id=?", (jid,))
        if row and row["status"] != "running":
            assert row["status"] == "done", f"scan failed: {row['error']}"
            return jid
        time.sleep(0.02)
    raise AssertionError("scan did not finish")


def _lib_with_bad(tmp_path):
    import numpy as np
    from PIL import Image

    lib = tmp_path / "lib"
    lib.mkdir()
    Image.fromarray(np.random.default_rng(1).integers(0, 256, (32, 32, 3), dtype=np.uint8)).save(lib / "good.png")
    (lib / "broken.png").write_bytes(b"not a real png")
    (lib / "broken2.heic").write_bytes(b"garbage heic")
    return lib


def test_scan_writes_result_row(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = _lib_with_bad(tmp_path)
    root = register(database, str(lib))
    jid = _run_scan(q, database, root["id"])
    row = database.query_one("SELECT * FROM scan_results WHERE job_id=? AND root_id=?", (jid, root["id"]))
    assert row is not None
    assert row["new"] == 1          # good.png
    assert row["undecodable"] == 2  # the two broken files
    assert row["profiled"] == 0     # no --profile
    assert row["profile_json"] is None
    assert row["root_name"] == root["name"]


def test_scan_records_problem_files_with_reasons(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = _lib_with_bad(tmp_path)
    root = register(database, str(lib))
    jid = _run_scan(q, database, root["id"])
    probs = database.query(
        "SELECT problem, path, content_hash, detail FROM scan_problem_files "
        "WHERE job_id=? ORDER BY path", (jid,)
    )
    assert len(probs) == 2
    for p in probs:
        assert p["problem"] == "undecodable"
        assert p["detail"]                  # a reason was recorded
        assert p["content_hash"]            # undecodable still has a hash


def test_profile_json_stored_when_profiled(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = _lib_with_bad(tmp_path)
    root = register(database, str(lib))
    jid = _run_scan(q, database, root["id"], profile=True)
    row = database.query_one("SELECT profiled, profile_json FROM scan_results WHERE job_id=?", (jid,))
    assert row["profiled"] == 1
    import json
    snap = json.loads(row["profile_json"])
    assert "secs" in snap and "wall_s" in snap  # the flattened snapshot


def test_status_root_detail_surfaces_problem_files(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = _lib_with_bad(tmp_path)
    root = register(database, str(lib))
    _run_scan(q, database, root["id"])
    det = queries.root_detail(root["name"])
    assert det["last_scan"] is not None
    assert det["last_scan"]["undecodable"] == 2
    assert len(det["problem_files"]) == 2
    assert all(pf["problem"] == "undecodable" for pf in det["problem_files"])


def test_dry_run_writes_no_result(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = _lib_with_bad(tmp_path)
    root = register(database, str(lib))
    _run_scan(q, database, root["id"], dry_run=True)
    assert database.query_one("SELECT COUNT(*) c FROM scan_results")["c"] == 0
    assert database.query_one("SELECT COUNT(*) c FROM scan_problem_files")["c"] == 0


def test_all_scan_writes_row_per_root(queue_and_db, tmp_path):
    q, database = queue_and_db
    import numpy as np
    from PIL import Image

    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    for d in (a, b):
        Image.fromarray(np.random.default_rng(hash(str(d)) % 999).integers(0, 256, (16, 16, 3), dtype=np.uint8)).save(d / "x.png")
    ra = register(database, str(a))
    rb = register(database, str(b))
    jid = _run_scan(q, database, all=True)
    rows = database.query("SELECT root_id FROM scan_results WHERE job_id=?", (jid,))
    assert {r["root_id"] for r in rows} == {ra["id"], rb["id"]}  # one row per root


def test_clear_db_wipes_scan_results(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = _lib_with_bad(tmp_path)
    root = register(database, str(lib))
    _run_scan(q, database, root["id"])
    assert database.query_one("SELECT COUNT(*) c FROM scan_problem_files")["c"] == 2
    database.clear_catalog()
    assert database.query_one("SELECT COUNT(*) c FROM scan_results")["c"] == 0
    assert database.query_one("SELECT COUNT(*) c FROM scan_problem_files")["c"] == 0
