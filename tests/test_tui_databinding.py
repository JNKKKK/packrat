"""Data-binding tests — render from a REAL seeded DB via queries.py (§Testing).

The other TUI tests render from ``fixtures.py`` (hand-authored, mockup-shaped).
These close the loop: seed a root + scan through the real pipeline, then feed the
actual ``queries.roots_snapshot()`` / ``status_snapshot()`` / ``root_detail()``
dicts into the pure renderers and assert they render without error and reflect the
data — verifying "widget ⇄ data contract" so the TUI can't invent state the query
doesn't have (component-plan Why-build-it #4).
"""

from __future__ import annotations

import time

import pytest

from packrat import db, queries
from packrat.jobs import JobQueue
from packrat.jobs import scan as _scan  # noqa: F401 - registers 'scan'
from packrat.roots import register
from packrat.tui import render
from packrat.tui.data import DataSource
from packrat.tui.fixtures import REFERENCE_NOW as NOW
from packrat.tui.framing import screen
from packrat.tui.screens.dashboard import dashboard_body
from packrat.tui.screens.rootdetail import detail_body, detail_header_right
from packrat.tui.screens.roots import roots_body

pytest.importorskip("blake3")
pytest.importorskip("PIL")
pytest.importorskip("pdqhash")


@pytest.fixture()
def seeded(packrat_home, tmp_path):
    """A DB with one scanned library root (a few PNGs, one exact dup)."""
    import numpy as np
    from PIL import Image

    db.init_db().close()
    conn = db.connect(check_same_thread=False)
    database = db.Database(conn)
    q = JobQueue(database)

    lib = tmp_path / "MyPhotos"
    lib.mkdir()
    for i in range(3):
        arr = np.random.default_rng(i).integers(0, 256, (32, 32, 3), dtype=np.uint8)
        Image.fromarray(arr).save(lib / f"p{i}.png")
    root = register(database, str(lib))

    jid = q.submit("scan", {"root_id": root["id"]})
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        row = database.query_one("SELECT status FROM jobs WHERE id=?", (jid,))
        if row and row["status"] != "running":
            assert row["status"] == "done"
            break
        time.sleep(0.02)
    yield root
    q.shutdown()
    database.close()


def _fixed_frame(frame: str) -> None:
    rows = frame.split("\n")
    assert len(rows) == 24
    assert all(len(r) == 100 for r in rows)


def test_roots_snapshot_binds_to_rootrow(seeded):
    snap = queries.roots_snapshot()
    assert snap, "expected at least one root"
    r = next(x for x in snap if x["name"] == "MyPhotos")
    # The renderer accepts the real query row shape and shows the dot + count.
    compact = render.root_row_compact(r)
    assert "MyPhotos" in compact
    assert r["asset_count"] == 3            # 3 distinct PNGs
    # Never scanned dedup → the ◐ scanned-only dot.
    assert render.root_dot(r) == "◐"


def test_status_snapshot_binds_to_dashboard(seeded):
    snap = queries.status_snapshot()
    frame = screen("packrat", dashboard_body(snap, now=NOW),
                   "v0.1.0 · daemon ● up", footer="Esc / Ctrl-Q quit")
    _fixed_frame(frame)
    assert "MyPhotos" in frame
    assert f"{snap['assets']:,}" in frame    # the live asset count


def test_roots_interface_binds(seeded):
    snap = queries.roots_snapshot()
    frame = screen("packrat · Roots", roots_body(snap, now=NOW),
                   "daemon ● up", footer="Esc back")
    _fixed_frame(frame)
    assert "MyPhotos" in frame


def test_root_detail_binds(seeded):
    d = queries.root_detail(seeded["name"])
    assert d is not None
    jobs = queries.root_jobs(d["id"])
    frame = screen(f"packrat · {d['name']}", detail_body(d, now=NOW, jobs=jobs),
                   detail_header_right(d), footer="Esc")
    _fixed_frame(frame)
    assert "scan" in frame                   # the scan job appears in history


def test_datasource_over_real_query(seeded):
    ds = DataSource(queries.status_snapshot)
    snap = ds.refresh()
    assert ds.healthy and snap["assets"] == 3


def test_sort_cycle_over_real_snapshot(seeded):
    snap = queries.roots_snapshot()
    # All four sort modes must produce the same set, just reordered.
    base = {r["id"] for r in snap}
    for mode in range(4):
        ordered = render.sort_roots(snap, mode)
        assert {r["id"] for r in ordered} == base
        assert len(ordered) == len(snap)
