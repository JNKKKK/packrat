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
    from packrat.tui.layout import cell_width
    rows = frame.split("\n")
    assert len(rows) == 24
    # DISPLAY width — root detail's 📁 mascot is a 2-cell glyph (1 char), so len()
    # would under-count; measure by terminal cells.
    assert all(cell_width(r) == 100 for r in rows)


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


def test_fmt_size_formats_human_readable():
    assert render.fmt_size(0) == "0 B"
    assert render.fmt_size(None) == "0 B"
    assert render.fmt_size(900) == "900 B"
    assert render.fmt_size(3_200_000_000) == "3.0 GB"      # <10 → one decimal
    assert render.fmt_size(148_000_000_000) == "138 GB"    # ≥10 → no decimal
    assert render.fmt_size(512_000_000_000) == "477 GB"


def test_collection_lines_right_align_values():
    """Every collection stat's value is right-aligned to the box's inner width, so the
    numbers hug the right edge (item 2)."""
    from packrat.tui.layout import cell_width
    snap = {"assets": 124803, "photos": 111240, "videos": 13563,
            "trashed": 3904, "size_bytes": 704_500_000_000, "lifetime_deduped": 8241}
    w = render.COLLECTION_INNER_W
    lines = render.collection_lines(snap, now="2026-07-15T13:30:00", width=w)
    assert len(lines) == 6
    for ln in lines:
        assert cell_width(ln) == w, (ln, cell_width(ln))   # padded to the full width
        assert ln[-1] != " "                               # value ends flush at the right
    # the label is on the left, the value on the right of the same line
    assets = lines[0]
    assert assets.startswith("Assets") and assets.rstrip().endswith("124,803")


def test_roots_snapshot_carries_size_bytes(seeded):
    """roots_snapshot() sums file_instances.size per root (item 3)."""
    snap = queries.roots_snapshot()
    r = next(x for x in snap if x["name"] == "MyPhotos")
    assert r["size_bytes"] > 0                 # the 3 PNGs have real bytes on disk
    # The wide row renders the size (right-aligned), and root detail shows it too.
    assert render.fmt_size(r["size_bytes"]) in render.root_row_wide(r, now=NOW)


def test_status_snapshot_carries_collection_size(seeded):
    """status_snapshot() sums file_instances.size collection-wide, and the Collection
    box shows it as the "Size" line (the full collection size)."""
    from packrat.tui.screens.dashboard import dashboard_body

    snap = queries.status_snapshot()
    # Collection total == the sum of per-root sizes (same files, summed once).
    assert snap["size_bytes"] == sum(r["size_bytes"] for r in snap["roots"])
    assert snap["size_bytes"] > 0
    frame = screen("packrat", dashboard_body(snap, now=NOW), "daemon ● up", footer="Esc")
    _fixed_frame(frame)
    assert "Size" in frame and render.fmt_size(snap["size_bytes"]) in frame


def test_root_detail_shows_size_not_files(seeded):
    """root_detail() carries size_bytes; the stats header shows size, not a file count."""
    d = queries.root_detail(seeded["name"])
    assert d["size_bytes"] > 0
    frame = screen(f"packrat · {d['name']}", detail_body(d, now=NOW, jobs=[]),
                   detail_header_right(d), footer="Esc")
    _fixed_frame(frame)
    assert "size" in frame and render.fmt_size(d["size_bytes"]) in frame
    assert "files " not in frame               # the raw file-count row is gone (item 2)


def test_lifetime_deduped_sums_dedup_deleted(seeded, packrat_home):
    """status_snapshot()['lifetime_deduped'] sums the `deleted` total across completed
    dedup jobs' result_json (item 1)."""
    import json as _json

    conn = db.connect(check_same_thread=False)
    database = db.Database(conn)
    try:
        # Two completed dedup jobs recording deleted totals + one with none (older row).
        for deleted in (12, 40):
            database.execute(
                "INSERT INTO jobs(type, status, result_json) VALUES ('dedup', 'done', ?)",
                (_json.dumps({"op": "dedup", "action": "confirm", "deleted": deleted}),),
            )
        database.execute(
            "INSERT INTO jobs(type, status, result_json) VALUES ('dedup', 'done', ?)",
            (_json.dumps({"op": "dedup", "action": "analyze"}),),   # no 'deleted' key
        )
        snap = queries.status_snapshot()
        assert snap["lifetime_deduped"] == 52          # 12 + 40, analyze ignored
    finally:
        database.close()


# --- review-card state is reconciled against the LIVE review_runs row --------
# A dedup/cleanup analyze (or advancing confirm) job freezes review_status='pending'
# + its stage into result_json; a later --confirm advances/finishes the run WITHOUT
# rewriting that older job's row. queries._job_dict must reconcile the frozen snapshot
# against the live run so a stale card shows the right actions (§8 B).
def _seed_review_job(database, *, root_id, stage, run_id, status="done"):
    """Insert a dedup analyze job frozen at ``stage`` referencing run ``run_id``."""
    import json as _json

    return database.execute(
        "INSERT INTO jobs(type, root_id, status, params_json, result_json) "
        "VALUES ('dedup', ?, ?, ?, ?)",
        (root_id, status, _json.dumps({"root_id": root_id}),
         _json.dumps({"op": "dedup", "action": "analyze", "review_status": "pending",
                      "stage": stage, "run_id": run_id, "to_delete_exact": 0,
                      "groups": 3, "members": 7, "summary": f"stage {stage}"})),
    ).lastrowid


def test_review_state_current_advanced_and_closed(seeded, packrat_home):
    """job_detail attaches review_state reconciled from the live review_runs row."""
    from packrat.tui.screens import jobcard

    conn = db.connect(check_same_thread=False)
    database = db.Database(conn)
    try:
        rid = queries.roots_snapshot()[0]["id"]
        # One dedup run, currently pending on stage 3 (stage 2 already confirmed).
        run_id = database.execute(
            "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
            "VALUES (?, 'dedup', 'pending', 3, 'staged', '2026-07-20T00:00:00')", (rid,)
        ).lastrowid
        j_stage2 = _seed_review_job(database, root_id=rid, stage=2, run_id=run_id)  # advanced
        j_stage3 = _seed_review_job(database, root_id=rid, stage=3, run_id=run_id)  # current

        d2 = queries.job_detail(j_stage2)
        d3 = queries.job_detail(j_stage3)
        # The stage-2 analyze card: its stage was confirmed, run moved on → 'advanced'.
        assert d2["review_state"] == "advanced" and d2["review_live_stage"] == 3
        assert jobcard.review_ui(d2) == "advanced"
        # The stage-3 analyze card owns the pending stage → 'current'.
        assert d3["review_state"] == "current" and d3["review_live_stage"] == 3
        assert jobcard.review_ui(d3) == "current"

        # Now the run completes (stage 3 confirmed) → both cards go 'closed' (no actions).
        database.execute("UPDATE review_runs SET status='completed' WHERE id=?", (run_id,))
        for jid in (j_stage2, j_stage3):
            d = queries.job_detail(jid)
            assert d["review_state"] == "closed"
            assert jobcard.review_ui(d) is None
    finally:
        database.close()


def test_review_card_bodies_reflect_reconciled_state(seeded, packrat_home):
    """The job card body/status renders per reconciled state — the three scenarios."""
    from packrat.tui.screens import jobcard

    conn = db.connect(check_same_thread=False)
    database = db.Database(conn)
    try:
        rid = queries.roots_snapshot()[0]["id"]
        run_id = database.execute(
            "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
            "VALUES (?, 'dedup', 'pending', 3, 'staged', '2026-07-20T00:00:00')", (rid,)
        ).lastrowid
        j2 = _seed_review_job(database, root_id=rid, stage=2, run_id=run_id)
        j3 = _seed_review_job(database, root_id=rid, stage=3, run_id=run_id)

        # current: full actions incl. confirm.
        b3 = "\n".join(jobcard.card_body(queries.job_detail(j3), now=NOW))
        assert "confirm this stage" in b3 and "open review folder" in b3
        assert jobcard._status_word(queries.job_detail(j3)) == "⚠ awaiting review"

        # advanced: open + cancel, but NO confirm (would apply a different stage).
        b2 = "\n".join(jobcard.card_body(queries.job_detail(j2), now=NOW))
        assert "open review folder" in b2 and "cancel run" in b2
        assert "confirm this stage" not in b2
        assert "advanced to stage 3" in b2
        assert jobcard._status_word(queries.job_detail(j2)) != "⚠ awaiting review"

        # closed (run finished): no review actions at all, plain summary card.
        database.execute("UPDATE review_runs SET status='completed' WHERE id=?", (run_id,))
        for jid in (j2, j3):
            body = "\n".join(jobcard.card_body(queries.job_detail(jid), now=NOW))
            assert "confirm this stage" not in body
            assert "open review folder" not in body and "cancel run" not in body
            assert jobcard._status_word(queries.job_detail(jid)) == "done"
    finally:
        database.close()


def test_review_counts_reports_network_delete_set(seeded):
    """The pending review's counts carry a `network` tally (files on a non-recyclable
    share, deleted PERMANENTLY — §10) so the confirm surface can warn. Regression: the
    TUI confirm never surfaced the permanent-delete warning."""
    conn = db.connect(check_same_thread=False)
    database = db.Database(conn)
    try:
        rid = queries.roots_snapshot()[0]["id"]
        run_id = database.execute(
            "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
            "VALUES (?, 'dedup', 'pending', 1, 'staged', '2026-07-20T00:00:00')", (rid,)
        ).lastrowid
        # Two stage-1 exact-delete candidates: one on a UNC share, one local.
        for path in (r"\\nas\photos\dup.jpg", r"C:\photos\dup2.jpg"):
            database.execute(
                "INSERT INTO review_actions(run_id, stage, folder, kind, reason, "
                "default_action, asset_id, instance_id, path, shortcut_name) "
                "VALUES (?, 1, 'exact_dup_to_delete', 'exact', 'exact-external', 'delete', "
                "1, 1, ?, 'g0001_0001.lnk')", (run_id, path),
            )
        d = queries.root_detail(queries.roots_snapshot()[0]["name"])
        counts = d["pending_review"]["counts"]
        assert counts["to_delete_exact"] == 2
        assert counts["network"] == 1              # only the \\nas UNC path counts
    finally:
        database.close()


def test_status_snapshot_assets_total_reconciles_with_split(seeded):
    """The headline `assets` total = photos + videos (ACTIVE), so it reconciles with the
    split shown beside it; trashed assets are counted separately, not folded in."""
    conn = db.connect(check_same_thread=False)
    database = db.Database(conn)
    try:
        # Trash one asset (a fileless trash-memory entry, as a refresh flip would leave).
        aid = database.query_one("SELECT id FROM assets LIMIT 1")["id"]
        database.execute("UPDATE assets SET status='trashed' WHERE id=?", (aid,))
        snap = queries.status_snapshot()
        assert snap["assets"] == snap["photos"] + snap["videos"]   # total == active split
        assert snap["trashed"] == 1                                # trashed counted apart
    finally:
        database.close()


def test_status_snapshot_binds_to_dashboard(seeded):
    snap = queries.status_snapshot()
    frame = screen("packrat", dashboard_body(snap, now=NOW),
                   "v0.1.0 · daemon ● up", footer="Esc / Ctrl-Q quit")
    _fixed_frame(frame)
    assert "MyPhotos" in frame
    assert f"{snap['assets']:,}" in frame    # the live asset count


def test_status_snapshot_running_carries_status_and_bar(seeded, packrat_home):
    """A running job in status_snapshot() must carry status='running' so the dashboard
    Queue box renders the progress bar (regression: the query omitted j.status, so
    render.queue_row fell through to the plain, bar-less row — issue #2)."""
    from packrat.tui.tokens import BAR_FILL, BAR_EMPTY

    conn = db.connect(check_same_thread=False)
    database = db.Database(conn)
    try:
        # Simulate a job mid-scan: a durable `running` row with a progress counter.
        database.execute(
            "INSERT INTO jobs(type, root_id, status, total, done, started_at) "
            "VALUES ('scan', ?, 'running', 1000, 400, '2026-07-15T09:00:00')",
            (seeded["id"],),
        )
        snap = queries.status_snapshot()
        assert snap["running"] is not None
        assert snap["running"]["status"] == "running"      # the missing column
        frame = screen("packrat", dashboard_body(snap, now=NOW),
                       "daemon ● up", footer="Esc")
        _fixed_frame(frame)
        # The dashboard Queue box now draws the ███░░░ bar + a percentage for the run.
        assert (BAR_FILL in frame and BAR_EMPTY in frame), "no progress bar in the dashboard"
        assert "40%" in frame and "400/1,000" in frame
    finally:
        database.close()


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
