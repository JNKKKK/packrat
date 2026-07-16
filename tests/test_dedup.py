r"""dedup (§8 B) 3-stage sequence: exact → recompression → minor-edit.

Drives the real dedup handler through a ``JobQueue`` + ``Database`` (as test_scan
does), against real PNGs/JPEGs so the PDQ path runs. A JPEG-80 recompress of a PNG
lands at PDQ distance ~0–4 (measured) → stage 2 (recompression); a lightly edited
copy lands in the wider band → stage 3. ``.lnk`` creation + Recycle Bin need
Windows, so staging/confirm tests are ``@win_only``; plan/dry-run/reconcile run
everywhere.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from packrat import db, review
from packrat.jobs import JobQueue
from packrat.jobs import dedup as _dedup  # noqa: F401 - registers 'dedup'
from packrat.jobs import scan as _scan  # noqa: F401 - registers 'scan'
from packrat.roots import register

pytest.importorskip("blake3")
pytest.importorskip("PIL")
pytest.importorskip("pdqhash")

WINDOWS = sys.platform == "win32"
win_only = pytest.mark.skipif(not WINDOWS, reason="dedup staging/delete needs Windows shell APIs")


# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def queue_and_db(packrat_home):
    db.init_db().close()
    conn = db.connect(check_same_thread=False)
    d = db.Database(conn)
    q = JobQueue(d)
    yield q, d
    q.shutdown()
    d.close()


def _run(q, database, job_type, expect="done", **params):
    jid = q.submit(job_type, params)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        row = database.query_one("SELECT status, error FROM jobs WHERE id=?", (jid,))
        if row and row["status"] != "running":
            assert row["status"] == expect, f"{job_type} -> {row['status']}: {row['error']}"
            return jid
        time.sleep(0.02)
    raise AssertionError(f"{job_type} did not finish")


def _run_capture(q, database, job_type, **params):
    jid = q.submit(job_type, params)
    sub = q.subscribe(jid)
    logs = []
    while True:
        ev = sub.q.get(timeout=30)
        if ev is None:
            break
        if ev.type == "log":
            logs.append(ev.message)
    row = database.query_one("SELECT status, error FROM jobs WHERE id=?", (jid,))
    assert row["status"] == "done", f"{job_type} failed: {row['error']}"
    return logs


def _scan_root(q, database, root_id, **params):
    _run(q, database, "scan", root_id=root_id, **params)


def _photo(path, seed, kind="PNG", quality=92, tweak=0):
    """A structured (compressible) photo. ``tweak`` nudges pixels for a 'minor edit'."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    w, h = 256, 192
    yy, xx = np.mgrid[0:h, 0:w]
    base = np.sin(xx / w * 6.28) * 60 + np.cos(yy / h * 9.42) * 50 + 128
    im = Image.fromarray(np.stack([base, base * 0.8 + 30, base * 0.6 + 60], -1).clip(0, 255).astype("uint8"))
    dr = ImageDraw.Draw(im)
    rng = np.random.default_rng(seed)
    for _ in range(6):
        x0, y0 = int(rng.integers(0, w)), int(rng.integers(0, h))
        r = int(rng.integers(w // 8, w // 3))
        dr.ellipse([x0, y0, x0 + r, y0 + r], fill=tuple(int(v) for v in rng.integers(0, 255, 3)))
    if tweak:
        # A small opaque rectangle in a corner → a modest PDQ shift (a "minor edit").
        dr.rectangle([0, 0, tweak, tweak], fill=(0, 0, 0))
    im = im.filter(ImageFilter.GaussianBlur(1))
    if kind == "JPEG":
        im.save(path, "JPEG", quality=quality)
    else:
        im.save(path, "PNG")


def _distinct(path, seed):
    import numpy as np
    from PIL import Image

    Image.fromarray(np.random.default_rng(seed).integers(0, 256, (64, 64, 3), dtype="uint8")).save(path)


def _run_row(database, root_id):
    return database.query_one(
        "SELECT * FROM review_runs WHERE root_id=? AND status='pending'", (root_id,)
    )


def _stage_actions(database, run_id, stage):
    return database.query(
        "SELECT * FROM review_actions WHERE run_id=? AND stage=? ORDER BY id", (run_id, stage)
    )


def _stage_dir(root_path, stage):
    folder = {1: review.EXACT_DUP, 2: review.SUSPECT_RECOMPRESSION, 3: review.WITH_MINOR_EDITS}[stage]
    return review.staging_folder(root_path, folder)


# ---------------------------------------------------------------------------
# validation + dry-run (no Windows staging needed)
# ---------------------------------------------------------------------------
def test_dedup_rejects_trash_root(queue_and_db, tmp_path):
    q, database = queue_and_db
    trash = tmp_path / "Trash"
    trash.mkdir()
    root = register(database, str(trash), kind="trash")
    _run(q, database, "dedup", expect="error", root_id=root["id"])


def test_dedup_dry_run_reports_all_stages(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    import shutil

    shutil.copy(lib / "a.png", lib / "a_copy.png")  # exact dup → stage 1
    _photo(lib / "a.jpg", 1, kind="JPEG", quality=80)  # recompress of a.png → stage 2
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    logs = _run_capture(q, database, "dedup", root_id=root["id"], dry_run=True)
    blob = "\n".join(logs)
    assert "stage 1" in blob and "stage 2" in blob and "stage 3" in blob
    assert "no staging folders" in blob
    # Dry-run writes nothing.
    assert _run_row(database, root["id"]) is None
    assert database.query_one("SELECT COUNT(*) c FROM review_actions")["c"] == 0


def test_dedup_already_clean_autocompletes(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _distinct(lib / "a.png", 1)
    _distinct(lib / "b.png", 2)
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])
    assert _run_row(database, root["id"]) is None  # no dangling pending run


# ---------------------------------------------------------------------------
# the 3-stage sequence (Windows: real .lnk)
# ---------------------------------------------------------------------------
@win_only
def test_dedup_stage1_exact_then_advances_to_stage2(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    import shutil

    shutil.copy(lib / "a.png", lib / "a_copy.png")   # exact dup → stage 1
    _photo(lib / "a.jpg", 1, kind="JPEG", quality=80)  # recompress → stage 2 near-dup of a.png
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])

    # Analyze → stage 1.
    _run(q, database, "dedup", root_id=root["id"])
    run = _run_row(database, root["id"])
    assert run["stage"] == 1 and run["stage_phase"] == "staged"
    s1 = _stage_actions(database, run["id"], 1)
    assert len(s1) == 1 and s1[0]["folder"] == review.EXACT_DUP and s1[0]["reason"] == "exact-internal"
    assert os.path.exists(os.path.join(_stage_dir(lib, 1), s1[0]["shortcut_name"]))

    # Confirm stage 1 (default-delete, shortcut present → delete the redundant copy),
    # auto-advance to stage 2 (recompression: a.png & a.jpg grouped).
    _run(q, database, "dedup", root_id=root["id"], confirm=True)
    run = _run_row(database, root["id"])
    assert run is not None and run["stage"] == 2 and run["stage_phase"] == "staged"
    # a_copy.png deleted; a.png + a.jpg remain (2 files).
    assert database.query_one("SELECT COUNT(*) c FROM file_instances")["c"] == 2
    s2 = _stage_actions(database, run["id"], 2)
    assert len(s2) == 2 and all(a["folder"] == review.SUSPECT_RECOMPRESSION for a in s2)
    assert database.query_one("SELECT COUNT(*) c FROM similarity_edges")["c"] >= 1

    # Stage 2 is default-KEEP: delete one member by REMOVING its shortcut.
    grp = _stage_dir(lib, 2)
    victim = s2[0]
    os.remove(os.path.join(grp, victim["shortcut_name"]))
    # Confirm stage 2 → advance to stage 3 (likely empty) → completed.
    _run(q, database, "dedup", root_id=root["id"], confirm=True)
    assert _run_row(database, root["id"]) is None  # run completed
    run_final = database.query_one(
        "SELECT status FROM review_runs WHERE root_id=? ORDER BY id DESC LIMIT 1", (root["id"],)
    )
    assert run_final["status"] == "completed"
    # The victim's file is gone and its asset is trashed (perceptual discard).
    assert database.query_one("SELECT COUNT(*) c FROM file_instances")["c"] == 1
    tr = database.query_one("SELECT status, trash_reason FROM assets WHERE id=?", (victim["asset_id"],))
    assert tr["status"] == "trashed" and tr["trash_reason"] == "dedup-perceptual"


@win_only
def test_dedup_stage1_spare_by_removing_shortcut(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    import shutil

    shutil.copy(lib / "a.png", lib / "a_copy.png")
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])
    run = _run_row(database, root["id"])
    s1 = _stage_actions(database, run["id"], 1)
    # Remove the shortcut → spare the file (default-delete stage, veto).
    os.remove(os.path.join(_stage_dir(lib, 1), s1[0]["shortcut_name"]))
    _run(q, database, "dedup", root_id=root["id"], confirm=True)
    # Nothing deleted — both exact copies remain (and no near-dups → run completes).
    assert database.query_one("SELECT COUNT(*) c FROM file_instances")["c"] == 2


@win_only
def test_dedup_stage2_keep_by_leaving_shortcut(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 5, kind="PNG")
    _photo(lib / "a.jpg", 5, kind="JPEG", quality=80)  # recompress near-dup, no exact dup
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    # No exact dups → analyze skips empty stage 1 and lands on stage 2.
    _run(q, database, "dedup", root_id=root["id"])
    run = _run_row(database, root["id"])
    assert run["stage"] == 2
    assert len(_stage_actions(database, run["id"], 2)) == 2
    # Leave BOTH shortcuts (keep everything) → confirm deletes nothing, run completes.
    _run(q, database, "dedup", root_id=root["id"], confirm=True)
    assert _run_row(database, root["id"]) is None
    assert database.query_one("SELECT COUNT(*) c FROM file_instances")["c"] == 2
    assert database.query_one("SELECT COUNT(*) c FROM assets WHERE status='trashed'")["c"] == 0


@win_only
def test_dedup_confirm_resumes_from_applied_phase(queue_and_db, tmp_path):
    """A crash between apply and stage-next (stage_phase='applied') → re-confirm advances,
    it does NOT re-delete (§8 B Phase 7 resumable window)."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    import shutil

    shutil.copy(lib / "a.png", lib / "a_copy.png")   # exact dup → stage 1
    _photo(lib / "a.jpg", 1, kind="JPEG", quality=80)  # recompress → stage 2
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])
    run = _run_row(database, root["id"])
    # Simulate: stage 1 deletions applied + committed, but the daemon died before
    # staging stage 2. Delete the file + flip the phase by hand to mimic that state.
    s1 = _stage_actions(database, run["id"], 1)
    from send2trash import send2trash

    send2trash(s1[0]["path"])
    database.execute("DELETE FROM file_instances WHERE id=?", (s1[0]["instance_id"],))
    database.execute("UPDATE review_runs SET stage_phase='applied' WHERE id=?", (run["id"],))
    review.remove_tree(_stage_dir(lib, 1))
    files_before = database.query_one("SELECT COUNT(*) c FROM file_instances")["c"]

    # Re-confirm: must NOT re-apply stage 1; should stage stage 2 and pause.
    _run(q, database, "dedup", root_id=root["id"], confirm=True)
    run2 = _run_row(database, root["id"])
    assert run2 is not None and run2["stage"] == 2 and run2["stage_phase"] == "staged"
    assert database.query_one("SELECT COUNT(*) c FROM file_instances")["c"] == files_before


def test_scan_persists_detail_score_for_photos(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    row = database.query_one("SELECT media_type, detail_score FROM assets")
    assert row["media_type"] == "photo"
    assert row["detail_score"] is not None and row["detail_score"] > 0


@win_only
def test_dedup_stage2_marks_lossless_original_as_lead(queue_and_db, tmp_path):
    """Stage 2 suggests the lossless original over a same-resolution recompression (§8 B).

    A resize shifts PDQ into stage 3; the real stage-2 case is a same-resolution
    recompression. The lossless-format tier must rank the PNG master above the JPEG."""
    q, database = queue_and_db
    lib = lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "master.png", 5, kind="PNG")            # lossless original
    _photo(lib / "export.jpg", 5, kind="JPEG", quality=80)  # same-res recompression
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])
    run = _run_row(database, root["id"])
    assert run["stage"] == 2
    acts = {os.path.basename(a["path"]): a for a in _stage_actions(database, run["id"], 2)}
    assert set(acts) == {"master.png", "export.jpg"}
    # The lossless PNG master is the suggested lead (lossless tier > detail_score).
    lead = acts["master.png"]
    assert lead["shortcut_name"].endswith("_suggested.lnk")
    assert not acts["export.jpg"]["shortcut_name"].endswith("_suggested.lnk")
    grp = _stage_dir(lib, 2)
    assert os.path.exists(os.path.join(grp, lead["shortcut_name"]))
    manifest = open(os.path.join(grp, "manifest.csv"), encoding="utf-8").read()
    assert "suggested_lead" in manifest and "detail_score" in manifest


@win_only
def test_dedup_stage3_has_no_lead(queue_and_db, tmp_path):
    """Stage 3 (minor edits) is deliberately unranked — no _suggested marker (§8 B)."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 7, kind="PNG")
    # tweak=20 → PDQ distance 32 (measured): inside the match, above t_photo_recompress
    # (10) → lands in the stage-3 minor-edit band, not stage 2.
    _photo(lib / "a_edit.png", 7, kind="PNG", tweak=20)
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])
    run = _run_row(database, root["id"])
    assert run is not None and run["stage"] == 3  # no exact/stage-2 candidates → opens on 3
    acts = _stage_actions(database, run["id"], 3)
    assert acts and not any(a["shortcut_name"].endswith("_suggested.lnk") for a in acts)


@win_only
def test_dedup_cancel_discards_all_staging(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    import shutil

    shutil.copy(lib / "a.png", lib / "a_copy.png")
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])
    run = _run_row(database, root["id"])
    _run(q, database, "dedup", root_id=root["id"], cancel=True)
    r = database.query_one("SELECT status FROM review_runs WHERE id=?", (run["id"],))
    assert r["status"] == "cancelled"
    assert database.query_one("SELECT COUNT(*) c FROM file_instances")["c"] == 2  # nothing deleted
    for name in review.DEDUP_STAGE_FOLDERS:
        assert not os.path.exists(review.staging_folder(str(lib), name))


@win_only
def test_dedup_confirm_aborts_if_stage_folder_missing(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    import shutil

    shutil.copy(lib / "a.png", lib / "a_copy.png")
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])
    # Delete the whole stage-1 folder → confirm must ABORT (not delete-all).
    review.remove_tree(_stage_dir(lib, 1))
    _run(q, database, "dedup", expect="error", root_id=root["id"], confirm=True)
    assert _run_row(database, root["id"]) is not None  # still pending
    assert database.query_one("SELECT COUNT(*) c FROM file_instances")["c"] == 2


@win_only
def test_dedup_second_analyze_rejected_while_pending(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    import shutil

    shutil.copy(lib / "a.png", lib / "a_copy.png")
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])
    from packrat.jobs import BusyError

    with pytest.raises(BusyError):
        q.submit("dedup", {"root_id": root["id"]})  # analyze owns the root
    # confirm/cancel own nothing → not rejected by per-root exclusivity.
    assert q.submit("dedup", {"root_id": root["id"], "cancel": True})


def test_scan_blocked_on_root_with_pending_dedup(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    root = register(database, str(lib))
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
        "VALUES (?, 'dedup', 'pending', 1, 'staged', '2026-01-01T00:00:00+00:00')",
        (root["id"],),
    )
    from packrat.jobs import BusyError

    with pytest.raises(BusyError):
        q.submit("scan", {"root_id": root["id"]})


# ---------------------------------------------------------------------------
# reconcile — analyze rollback vs. mid-sequence resume (§3)
# ---------------------------------------------------------------------------
def _fake_interrupted_dedup(database, root_id, *, stage, stage_phase, confirm=False):
    database.execute(
        "INSERT INTO jobs(type, status, total, done, started_at, params_json) "
        "VALUES ('dedup','running',0,0,'2026-01-01T00:00:00+00:00', ?)",
        (f'{{"root_id": {root_id}, "confirm": {str(confirm).lower()}, "cancel": false}}',),
    )
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
        "VALUES (?, 'dedup', 'pending', ?, ?, '2026-01-01T00:00:00+00:00')",
        (root_id, stage, stage_phase),
    )


def test_reconcile_rolls_back_interrupted_stage1_analyze(queue_and_db, tmp_path):
    q, database = queue_and_db
    from packrat.jobs.reconcile import reconcile_on_startup

    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    root = register(database, str(lib))
    _fake_interrupted_dedup(database, root["id"], stage=1, stage_phase="staged")
    staging = _stage_dir(lib, 1)
    review.ensure_dir(staging)

    summary = reconcile_on_startup(database)
    assert summary["rolled_back_runs"]
    assert _run_row(database, root["id"]) is None      # cancelled
    assert not os.path.exists(staging)                  # staging removed


def test_reconcile_keeps_midsequence_run_pending(queue_and_db, tmp_path):
    q, database = queue_and_db
    from packrat.jobs.reconcile import reconcile_on_startup

    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    root = register(database, str(lib))
    # Interrupted while on stage 2 (stage 1 already confirmed+deleted) → MUST stay pending.
    _fake_interrupted_dedup(database, root["id"], stage=2, stage_phase="staged")
    summary = reconcile_on_startup(database)
    assert not summary["rolled_back_runs"]
    assert _run_row(database, root["id"]) is not None


def test_reconcile_keeps_applied_phase_run_pending(queue_and_db, tmp_path):
    q, database = queue_and_db
    from packrat.jobs.reconcile import reconcile_on_startup

    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    root = register(database, str(lib))
    # Crash between apply and stage-next (stage_phase='applied') → resume via --confirm.
    _fake_interrupted_dedup(database, root["id"], stage=1, stage_phase="applied", confirm=True)
    summary = reconcile_on_startup(database)
    assert not summary["rolled_back_runs"]
    assert _run_row(database, root["id"]) is not None
