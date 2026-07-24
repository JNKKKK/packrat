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
        if row and row["status"] not in ("queued", "running"):
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


def test_dedup_already_clean_records_last_dedup(queue_and_db, tmp_path):
    """An already-clean dedup counts as successful → sets last_dedup_at (§11)."""
    from packrat import queries

    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _distinct(lib / "a.png", 1)
    _distinct(lib / "b.png", 2)
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])

    assert queries.root_detail(str(lib))["last_dedup_at"] is None  # never deduped yet
    # The scan marked the root dedup-dirty (indexed new content) — §12 rung 3.
    assert database.query_one("SELECT needs_dedup FROM roots WHERE id=?",
                              (root["id"],))["needs_dedup"] == 1
    _run(q, database, "dedup", root_id=root["id"])                 # already clean → completed
    # A completed dedup run with confirmed_at exists, and status <root> surfaces it.
    row = database.query_one(
        "SELECT status, confirmed_at FROM review_runs WHERE root_id=? AND run_type='dedup'",
        (root["id"],),
    )
    assert row["status"] == "completed" and row["confirmed_at"] is not None
    assert queries.root_detail(str(lib))["last_dedup_at"] == row["confirmed_at"]
    # An already-clean dedup completes → it CONSUMES the dedup-dirty signal (→ ◉ green).
    assert database.query_one("SELECT needs_dedup FROM roots WHERE id=?",
                              (root["id"],))["needs_dedup"] == 0


def test_dedup_cancel_does_not_count_as_deduped(queue_and_db, tmp_path):
    """A cancelled dedup run must NOT set last_dedup_at (only completed counts, §11)."""
    from packrat import queries

    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    # Two byte-identical files → a real stage-1 exact dup, so analyze opens a pending run.
    _distinct(lib / "a.png", 1)
    import shutil
    shutil.copy(lib / "a.png", lib / "a_copy.png")
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])          # analyze → pending
    assert _run_row(database, root["id"]) is not None
    _run(q, database, "dedup", root_id=root["id"], cancel=True)  # discard
    assert database.query_one(
        "SELECT status FROM review_runs WHERE root_id=? ORDER BY id DESC LIMIT 1", (root["id"],)
    )["status"] == "cancelled"
    # Cancelled ≠ successful → still "never deduped".
    assert queries.root_detail(str(lib))["last_dedup_at"] is None


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
    jid1 = _run(q, database, "dedup", root_id=root["id"], confirm=True)
    # The confirm records its deleted total in result_json (feeds lifetime-deduped).
    import json as _json
    r1 = _json.loads(database.query_one("SELECT result_json FROM jobs WHERE id=?", (jid1,))["result_json"])
    assert r1["deleted"] == 1                    # one exact-dup file collapsed
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
    jid2 = _run(q, database, "dedup", root_id=root["id"], confirm=True)
    r2 = _json.loads(database.query_one("SELECT result_json FROM jobs WHERE id=?", (jid2,))["result_json"])
    assert r2["deleted"] == 1                    # one perceptual near-dup deleted
    assert _run_row(database, root["id"]) is None  # run completed
    run_final = database.query_one(
        "SELECT status, confirmed_at FROM review_runs WHERE root_id=? ORDER BY id DESC LIMIT 1",
        (root["id"],),
    )
    assert run_final["status"] == "completed"
    # The full 3-stage completion consumes the dedup-dirty signal (→ ◉ green; §12 rung 3).
    assert database.query_one("SELECT needs_dedup FROM roots WHERE id=?",
                              (root["id"],))["needs_dedup"] == 0
    # Went through all stages → recorded as the last successful dedup (§11).
    from packrat import queries
    assert queries.root_detail(str(lib))["last_dedup_at"] == run_final["confirmed_at"]
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
def test_dedup_stage2_keep_suggested_deletes_non_leads(queue_and_db, tmp_path):
    """--confirm --keep-suggested keeps ONLY each group's suggested lead, ignoring edits."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "master.png", 5, kind="PNG")               # lossless → suggested lead
    _photo(lib / "export.jpg", 5, kind="JPEG", quality=80)  # recompression, not the lead
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])
    run = _run_row(database, root["id"])
    assert run["stage"] == 2
    acts = {os.path.basename(a["path"]): a for a in _stage_actions(database, run["id"], 2)}
    lead = acts["master.png"]
    assert lead["shortcut_name"].endswith("_suggested.lnk")

    # DELETE the lead's shortcut too — under normal confirm that would delete the lead.
    # --keep-suggested must IGNORE edits: keep master.png, delete export.jpg.
    grp = _stage_dir(lib, 2)
    os.remove(os.path.join(grp, lead["shortcut_name"]))
    os.remove(os.path.join(grp, acts["export.jpg"]["shortcut_name"]))
    _run(q, database, "dedup", root_id=root["id"], confirm=True, keep_suggested=True)
    assert _run_row(database, root["id"]) is None
    assert (lib / "master.png").exists()          # lead kept despite its shortcut removed
    assert not (lib / "export.jpg").exists()       # non-lead deleted
    tr = database.query_one("SELECT status, trash_reason FROM assets WHERE id=?",
                            (acts["export.jpg"]["asset_id"],))
    assert tr["status"] == "trashed" and tr["trash_reason"] == "dedup-perceptual"


@win_only
def test_dedup_keep_suggested_spares_group_without_lead(queue_and_db, tmp_path):
    """A stage-2 group with no _suggested lead is fully SPARED under --keep-suggested.

    Stage 3 (minor edits) never marks a lead, so a run that opens directly on stage 3
    with --keep-suggested is rejected; here we assert the group-level safety inside the
    intended-set builder via a stage-2 group whose lead row we strip of its marker.
    """
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 5, kind="PNG")
    _photo(lib / "a.jpg", 5, kind="JPEG", quality=80)
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])
    run = _run_row(database, root["id"])
    assert run["stage"] == 2
    # Simulate a group with no suggested lead: strip the _suggested marker from the
    # persisted rows AND rename the on-disk shortcut so nothing is a lead.
    grp = _stage_dir(lib, 2)
    for a in _stage_actions(database, run["id"], 2):
        if a["shortcut_name"].endswith("_suggested.lnk"):
            plain = a["shortcut_name"].replace("_suggested", "")
            os.rename(os.path.join(grp, a["shortcut_name"]), os.path.join(grp, plain))
            database.execute("UPDATE review_actions SET shortcut_name=? WHERE id=?",
                             (plain, a["id"]))
    _run(q, database, "dedup", root_id=root["id"], confirm=True, keep_suggested=True)
    # No lead → whole group spared → both files remain, nothing trashed.
    assert (lib / "a.png").exists() and (lib / "a.jpg").exists()
    assert database.query_one("SELECT COUNT(*) c FROM assets WHERE status='trashed'")["c"] == 0


def test_dedup_keep_suggested_rejected_on_non_stage2(queue_and_db, tmp_path):
    """--keep-suggested on a run parked at stage 3 (no leads) is rejected, not silently applied."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 7, kind="PNG")
    _photo(lib / "a_edit.png", 7, kind="PNG", tweak=20)  # minor edit → stage 3 only
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])
    run = _run_row(database, root["id"])
    assert run["stage"] == 3  # opens directly on stage 3 (no exact/stage-2 candidates)
    _run(q, database, "dedup", expect="error", root_id=root["id"],
         confirm=True, keep_suggested=True)
    assert _run_row(database, root["id"]) is not None  # still pending, nothing applied


def test_dedup_analyze_snapshots_match_thresholds_on_run(packrat_home, tmp_path):
    """Analyze snapshots the config's PDQ thresholds onto the review_runs row (§8 B
    follow-up), so the CLI log and the TUI poll both read ONE analyze-time source and a
    later config edit can't retroactively rewrite the run's histogram bands. Driven under a
    NON-default config to prove the values are config-derived, not hardcoded."""
    import dataclasses

    from packrat.config import Config, MatchConfig

    db.init_db().close()
    conn = db.connect(check_same_thread=False)
    database = db.Database(conn)
    custom = dataclasses.replace(
        Config(), match=dataclasses.replace(MatchConfig(), t_photo_recompress=7,
                                            t_photo_edit=44, t_match_video=110))
    q = JobQueue(database, config_loader=lambda: custom)
    try:
        lib = tmp_path / "lib"
        lib.mkdir()
        _photo(lib / "master.png", 5, kind="PNG")
        _photo(lib / "export.jpg", 5, kind="JPEG", quality=80)   # a stage-2 near-dup
        root = register(database, str(lib))
        _scan_root(q, database, root["id"])
        _run(q, database, "dedup", root_id=root["id"])
        run = _run_row(database, root["id"])
        assert (run["t_photo_recompress"], run["t_photo_edit"], run["t_match_video"]) == (7, 44, 110)
    finally:
        q.shutdown()
        database.close()


def test_dedup_stage2_reports_lead_pick_stats(queue_and_db, tmp_path):
    """Analyze logs the keep-lead breakdown (the shared review_stats block: photo column
    + format decision) — the same text the TUI Review box renders (§8 B)."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "master.png", 5, kind="PNG")               # lead by format (lossless > jpg)
    _photo(lib / "export.jpg", 5, kind="JPEG", quality=80)
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    logs = _run_capture(q, database, "dedup", root_id=root["id"])
    blob = "\n".join(logs)
    assert "keep-lead decided by:" in blob
    assert "photos (" in blob                # per-medium column header
    assert "+ format" in blob                # PNG vs JPEG at equal resolution → format decides
    assert "PDQ photo" in blob               # the split photo histogram is in the shared block


def test_dedup_stage1_log_shows_group_makeup(queue_and_db, tmp_path):
    """The stage-1 (analyze) staging log carries the internal/mixed group make-up line."""
    import shutil

    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    shutil.copy(lib / "a.png", lib / "a_copy.png")           # exact dup → stage 1
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    blob = "\n".join(_run_capture(q, database, "dedup", root_id=root["id"]))
    assert "to delete (exact):" in blob
    assert "group make-up:" in blob and "internal-only" in blob


def test_dedup_result_summary_omits_exact_when_not_stage1(queue_and_db, tmp_path):
    """A run opened at stage 2 (no stage-1 exact dups) must NOT report '0 exact' in its
    result summary — the exact count is stage-1-only, so stages 2/3 show grp/mbr instead."""
    import json as _json

    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 5, kind="PNG")                    # PNG + its JPEG recompress →
    _photo(lib / "a.jpg", 5, kind="JPEG", quality=80)       # a stage-2 near-dup, no stage-1 exact
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    jid = _run(q, database, "dedup", root_id=root["id"])
    summary = _json.loads(
        database.query_one("SELECT result_json FROM jobs WHERE id=?", (jid,))["result_json"]
    )["summary"]
    assert "staged stage 2" in summary
    assert "exact" not in summary            # regression: was "staged stage 2 · 0 exact · …"
    assert "grp/" in summary and "mbr" in summary


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

    # A real crash after the apply transaction would have committed deleted_count=1
    # (stage 1 collapsed a_copy.png). Mimic that so we can assert it's credited.
    database.execute("UPDATE review_runs SET deleted_count=1 WHERE id=?", (run["id"],))
    files_before = database.query_one("SELECT COUNT(*) c FROM file_instances")["c"]

    # Re-confirm: must NOT re-apply stage 1; should stage stage 2 and pause.
    jid = _run(q, database, "dedup", root_id=root["id"], confirm=True)
    run2 = _run_row(database, root["id"])
    assert run2 is not None and run2["stage"] == 2 and run2["stage_phase"] == "staged"
    assert database.query_one("SELECT COUNT(*) c FROM file_instances")["c"] == files_before
    # The resumed confirm SKIPPED the apply block, but still credits the deletions its
    # crashed predecessor applied — result_json.deleted must be 1, not 0 (regression:
    # the lifetime-deduped metric silently undercounted crash-interrupted confirms).
    import json as _json
    r = _json.loads(database.query_one("SELECT result_json FROM jobs WHERE id=?", (jid,))["result_json"])
    assert r["deleted"] == 1, r
    # And the accumulator is drained so a later stage-2 confirm can't double-count it.
    assert _run_row(database, root["id"])["deleted_count"] == 0


def test_reconcile_interrupted_dry_run_spares_unrelated_pending_review(queue_and_db, tmp_path):
    """An interrupted dedup --dry-run must NOT roll back a real pending review on the
    same root (§8 B). dry-run owns no root, so it can run alongside a pending review;
    reconcile treating it like an interrupted analyze would delete that review's staging.
    """
    import json as _json

    from packrat.jobs.reconcile import reconcile_on_startup

    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    import shutil

    shutil.copy(lib / "a.png", lib / "a_copy.png")     # exact dup → a real pending review
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])      # opens a pending review, stages stage 1
    run = _run_row(database, root["id"])
    assert run is not None and run["status"] == "pending"
    stage_dir = _stage_dir(lib, 1)
    assert os.path.isdir(stage_dir)                     # staging exists

    # Simulate a dedup --dry-run that was RUNNING on this root when the daemon died.
    database.execute(
        "INSERT INTO jobs(type, root_id, status, params_json) VALUES ('dedup', ?, 'running', ?)",
        (root["id"], _json.dumps({"root_id": root["id"], "dry_run": True})),
    )
    reconcile_on_startup(database)

    # The pending review + its staging must be untouched (dry-run rolls back nothing).
    run2 = _run_row(database, root["id"])
    assert run2 is not None and run2["status"] == "pending", "real review wrongly rolled back"
    assert os.path.isdir(stage_dir), "real review's staging wrongly deleted by a dry-run rollback"


def test_dedup_multistage_deleted_totals_do_not_double_count(queue_and_db, tmp_path):
    """Each per-stage confirm job reports ONLY its own deletions (drain-on-report), so
    the lifetime metric summing across a run's confirm jobs never double-counts (§8 B)."""
    import json as _json

    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    import shutil

    _photo(lib / "a.png", 1)
    shutil.copy(lib / "a.png", lib / "a_copy.png")        # exact dup → stage 1 (1 delete)
    _photo(lib / "a.jpg", 1, kind="JPEG", quality=80)     # recompress → stage 2
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])         # stage 1 staged

    j1 = _run(q, database, "dedup", root_id=root["id"], confirm=True)   # apply stage 1 → stage 2
    r1 = _json.loads(database.query_one("SELECT result_json FROM jobs WHERE id=?", (j1,))["result_json"])
    # Remove a.jpg's shortcut so stage 2 deletes it (default-KEEP → remove to delete).
    run = _run_row(database, root["id"])
    for a in _stage_actions(database, run["id"], 2):
        if os.path.basename(a["path"]) == "a.jpg":
            os.remove(os.path.join(_stage_dir(lib, 2), a["shortcut_name"]))
    j2 = _run(q, database, "dedup", root_id=root["id"], confirm=True)   # apply stage 2 → complete
    r2 = _json.loads(database.query_one("SELECT result_json FROM jobs WHERE id=?", (j2,))["result_json"])

    # Each job reports its OWN stage's deletions; summed = 2 total, no double-count.
    assert r1["deleted"] == 1 and r2["deleted"] == 1
    total = database.query_one(
        "SELECT COALESCE(SUM(json_extract(result_json,'$.deleted')),0) c FROM jobs "
        "WHERE type='dedup' AND status='done' AND json_extract(result_json,'$.deleted') IS NOT NULL"
    )["c"]
    assert total == 2, total


@win_only
def test_dedup_stage2_marks_lossless_original_as_lead(queue_and_db, tmp_path):
    """Stage 2 suggests the lossless original over a same-resolution recompression (§8 B).

    A resize shifts PDQ into stage 3; the real stage-2 case is a same-resolution
    recompression. The format rank must rank the PNG master above the JPEG."""
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
    # The lossless PNG master is the suggested lead (format rank: lossless > lossy).
    lead = acts["master.png"]
    assert lead["shortcut_name"].endswith("_suggested.lnk")
    assert not acts["export.jpg"]["shortcut_name"].endswith("_suggested.lnk")
    grp = _stage_dir(lib, 2)
    assert os.path.exists(os.path.join(grp, lead["shortcut_name"]))
    # The manifest carries the per-row lead reason: filled for the lead, blank otherwise.
    import csv as _csv

    with open(os.path.join(grp, "manifest.csv"), encoding="utf-8", newline="") as f:
        rows = {os.path.basename(r["target_path"]): r for r in _csv.DictReader(f)}
    assert "suggested_reason" in rows["master.png"]
    assert rows["master.png"]["suggested_lead"] == "1"
    assert rows["master.png"]["suggested_reason"] == "resolution + format"  # PNG vs JPEG @ equal res
    assert rows["export.jpg"]["suggested_lead"] == "0"
    assert rows["export.jpg"]["suggested_reason"] == ""  # non-lead → blank


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
def test_dedup_second_analyze_held_while_pending(queue_and_db, tmp_path):
    """§3: a second analyze on a root with a pending run is ENQUEUED + held, not rejected.

    It is not run (its owned root is held → dequeue-gate skips it), so it sits `queued`
    and `blocked_reason` names the holder. A confirm/cancel owns no root → runnable.
    """
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    import shutil

    shutil.copy(lib / "a.png", lib / "a_copy.png")
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])  # opens the pending run

    # Second analyze: enqueued (not rejected), and held — its owned root is busy.
    jid2 = q.submit("dedup", {"root_id": root["id"]})
    assert database.query_one("SELECT status FROM jobs WHERE id=?", (jid2,))["status"] == "queued"
    assert q.blocked_reason("dedup", {"root_id": root["id"]}) is not None
    # confirm/cancel own nothing → runnable (not blocked by per-root exclusivity).
    assert q.blocked_reason("dedup", {"root_id": root["id"], "cancel": True}) is None
    # Cancel the held analyze so teardown is clean.
    q.cancel(jid2)


def test_dedup_held_analyze_wakes_after_root_freed(queue_and_db, tmp_path):
    """A held analyze auto-runs once the pending run it waited on is cancelled (§3 pump).

    Verifies the finish-pump both starts the next job AND unblocks the one that was
    waiting on the freed root — no separate wake signal.
    """
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    import shutil

    shutil.copy(lib / "a.png", lib / "a_copy.png")
    root = register(database, str(lib))
    _scan_root(q, database, root["id"])
    _run(q, database, "dedup", root_id=root["id"])  # pending run #1 holds the root

    jid2 = q.submit("dedup", {"root_id": root["id"]})  # held behind #1
    assert database.query_one("SELECT status FROM jobs WHERE id=?", (jid2,))["status"] == "queued"
    # Cancelling run #1 frees the root; the finish-pump then starts the held analyze,
    # so jid2 leaves 'queued' on its own (no new submission).
    q.submit("dedup", {"root_id": root["id"], "cancel": True})
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        st = database.query_one("SELECT status FROM jobs WHERE id=?", (jid2,))["status"]
        if st != "queued":
            break
        time.sleep(0.02)
    assert st != "queued"  # the pump woke it once the root was free


def test_runnable_job_passes_blocked_head_of_queue(queue_and_db, tmp_path):
    """§3 runnable-first: a blocked job at the head must NOT stall a runnable one behind it.

    With a pending dedup holding root A, submit `scan A` (blocked) THEN `scan B`
    (runnable, different root). The blocked scan A stays queued; scan B jumps it and
    completes — proving dequeue is runnable-first, not strict FIFO head-of-line.
    """
    q, database = queue_and_db
    liba = tmp_path / "liba"; liba.mkdir(); _photo(liba / "a.png", 1)
    libb = tmp_path / "libb"; libb.mkdir(); _photo(libb / "b.png", 2)
    ra = register(database, str(liba))
    rb = register(database, str(libb))
    # A pending dedup holds root A (no worker slot — analyze finished).
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
        "VALUES (?, 'dedup', 'pending', 1, 'staged', 't')",
        (ra["id"],),
    )
    blocked = q.submit("scan", {"root_id": ra["id"]})   # head, blocked on root A
    runnable = q.submit("scan", {"root_id": rb["id"]})  # behind it, runnable
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        st = database.query_one("SELECT status FROM jobs WHERE id=?", (runnable,))["status"]
        if st not in ("queued", "running"):
            break
        time.sleep(0.02)
    assert st == "done"  # the runnable scan passed the blocked head
    assert database.query_one("SELECT status FROM jobs WHERE id=?", (blocked,))["status"] == "queued"
    q.cancel(blocked)  # clean teardown


def test_status_counts_scope_to_current_dedup_stage(queue_and_db, tmp_path):
    """`status` counts reflect the run's CURRENT stage, not all-time rows (§11).

    A dedup run keeps its confirmed stage-1 `review_actions` rows after advancing, so
    an unscoped count would report already-deleted exact dups as still 'to delete'.
    """
    q, database = queue_and_db
    from packrat import queries

    lib = tmp_path / "lib"
    lib.mkdir()
    root = register(database, str(lib))
    # A pending run advanced to stage 2, but with a leftover stage-1 exact action.
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
        "VALUES (?, 'dedup', 'pending', 2, 'staged', 't')",
        (root["id"],),
    )
    run_id = database.query_one("SELECT id FROM review_runs WHERE root_id=?", (root["id"],))["id"]
    database.execute(
        "INSERT INTO review_actions(run_id, stage, folder, kind, path) VALUES (?, 1, 'x', 'exact', 'p1')",
        (run_id,),
    )
    database.execute(
        "INSERT INTO review_actions(run_id, stage, folder, kind, path, group_no, member_no) "
        "VALUES (?, 2, 'x', 'perceptual', 'p2', 1, 1)",
        (run_id,),
    )
    d = queries.root_detail(str(lib))
    counts = d["pending_review"]["counts"]
    # Stage 2 is current → the stage-1 exact row is NOT counted as pending.
    assert counts["to_delete_exact"] == 0
    assert counts["members"] == 1 and counts["groups"] == 1


def test_root_detail_shows_pending_review_and_queued_jobs(queue_and_db, tmp_path):
    """§12 root detail: pending review + this root's queued backlog (blocked reasons)."""
    q, database = queue_and_db
    from packrat import queries

    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    root = register(database, str(lib))
    # A pending dedup holds the root (no worker slot — analyze finished).
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
        "VALUES (?, 'dedup', 'pending', 1, 'staged', 't')",
        (root["id"],),
    )
    # A scan submitted against it enqueues + is held (blocked on the pending dedup).
    q.submit("scan", {"root_id": root["id"]})

    d = queries.root_detail(str(lib))
    assert d["pending_review"]["run_type"] == "dedup"
    assert d["running_job"] is None                 # nothing running on this root
    assert len(d["queued_jobs"]) == 1
    qj = d["queued_jobs"][0]
    assert qj["type"] == "scan"
    assert qj["blocked"] is not None and qj["blocked"]["run_type"] == "dedup"


def test_scan_held_on_root_with_pending_dedup(queue_and_db, tmp_path):
    """§3: a manual scan of a root under review is ENQUEUED + held (not rejected)."""
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
    jid = q.submit("scan", {"root_id": root["id"]})
    # Held in the backlog, not run against the under-review root.
    assert database.query_one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "queued"
    holder = q.blocked_reason("scan", {"root_id": root["id"]})
    assert holder is not None and holder["run_type"] == "dedup"
    q.cancel(jid)  # clean teardown


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


# ---------------------------------------------------------------------------
# --prefer-internal (§8 B): stage-1 survivor flip + run-scoped persistence
# ---------------------------------------------------------------------------
def _cross_root_dup(tmp_path, database, q):
    """Register an internal + external root sharing one byte-identical photo; scan both.

    Returns ``(internal_root, external_root)``. The shared asset therefore has a copy in
    each root — the exact-dup-across-roots case stage 1 resolves.
    """
    import shutil

    internal = tmp_path / "internal"; internal.mkdir()
    external = tmp_path / "external"; external.mkdir()
    _photo(internal / "shared.png", 7)
    shutil.copy(internal / "shared.png", external / "shared.png")   # byte-identical → exact dup
    ri = register(database, str(internal))
    re = register(database, str(external))
    _scan_root(q, database, ri["id"])
    _scan_root(q, database, re["id"])
    return ri, re


def test_dedup_stage1_default_deletes_internal(queue_and_db, tmp_path):
    """Default (no flag): the EXTERNAL copy survives, the internal copy is staged for
    deletion (reason exact-external, not is_external)."""
    q, database = queue_and_db
    ri, _re = _cross_root_dup(tmp_path, database, q)
    _run(q, database, "dedup", root_id=ri["id"])
    run = _run_row(database, ri["id"])
    assert run is not None and run["prefer_internal"] == 0
    acts = [dict(a) for a in _stage_actions(database, run["id"], 1)]
    assert acts, "expected a stage-1 exact action"
    assert all(a["reason"] == "exact-external" for a in acts)
    assert all(not a["is_external"] for a in acts)           # deletes the INTERNAL copy
    assert all(a["path"].endswith("internal\\shared.png") for a in acts)


def test_dedup_stage1_prefer_internal_deletes_external(queue_and_db, tmp_path):
    """--prefer-internal: the INTERNAL copy survives, the external copy is staged for
    deletion (reason exact-internal-preferred, is_external=1 for the network warning)."""
    q, database = queue_and_db
    ri, _re = _cross_root_dup(tmp_path, database, q)
    _run(q, database, "dedup", root_id=ri["id"], prefer_internal=True)
    run = _run_row(database, ri["id"])
    assert run is not None and run["prefer_internal"] == 1
    acts = [dict(a) for a in _stage_actions(database, run["id"], 1)]
    assert acts, "expected a stage-1 exact action"
    assert all(a["reason"] == "exact-internal-preferred" for a in acts)
    assert all(a["is_external"] for a in acts)               # deletes the EXTERNAL copy
    assert all(a["path"].endswith("external\\shared.png") for a in acts)


def test_dedup_prefer_internal_persists_across_confirm(queue_and_db, tmp_path):
    """The flag is stored on the run at analyze and read from the row, NOT re-passed on
    confirm — a bare --confirm keeps applying the run's policy (§8 B run-scoped)."""
    q, database = queue_and_db
    ri, _re = _cross_root_dup(tmp_path, database, q)
    _run(q, database, "dedup", root_id=ri["id"], prefer_internal=True)
    run = _run_row(database, ri["id"])
    assert run["prefer_internal"] == 1     # persisted, available to every later confirm


def test_dedup_confirm_conflicting_prefer_internal_errors(queue_and_db, tmp_path):
    """A --prefer-internal on --confirm that conflicts with the run's stored value is
    rejected — the preference is fixed when the run opens."""
    q, database = queue_and_db
    ri, _re = _cross_root_dup(tmp_path, database, q)
    _run(q, database, "dedup", root_id=ri["id"])            # opened WITHOUT the flag
    assert _run_row(database, ri["id"])["prefer_internal"] == 0
    # Now confirm WITH the flag → conflict → the job errors (doesn't silently flip).
    _run(q, database, "dedup", root_id=ri["id"], confirm=True, prefer_internal=True,
         expect="error")
