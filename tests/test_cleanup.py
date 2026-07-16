r"""cleanup (§6.2) — remove trashed content from a library folder.

Default mode is exact-hash removal (the CLI does the count-confirm; the handler's
``apply`` submode does the deletion). ``--perceptual`` is a stateful analyze→confirm
run staging recompressed-trash matches. ``.lnk`` staging + Recycle Bin need Windows,
so those are ``@win_only``; match discovery / validation / dry-run run everywhere.

Real PNGs/JPEGs so the PDQ path runs: a JPEG-80 recompress of a PNG lands at PDQ
distance ~0–4 (measured, [[pdq-downscale]]) → a perceptual trash match.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from packrat import db, review
from packrat.jobs import JobQueue
from packrat.jobs import cleanup as _cleanup  # noqa: F401 - registers 'cleanup'
from packrat.jobs import scan as _scan  # noqa: F401 - registers 'scan'
from packrat.jobs import trash_refresh as _trash_refresh  # noqa: F401 - registers 'trash-refresh'
from packrat.roots import register

pytest.importorskip("blake3")
pytest.importorskip("PIL")
pytest.importorskip("pdqhash")

WINDOWS = sys.platform == "win32"
win_only = pytest.mark.skipif(not WINDOWS, reason="cleanup staging/delete needs Windows shell APIs")


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


def _photo(path, seed, kind="PNG", quality=92):
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
    im = im.filter(ImageFilter.GaussianBlur(1))
    if kind == "JPEG":
        im.save(path, "JPEG", quality=quality)
    else:
        im.save(path, "PNG")


def _run_row(database, root_id, run_type="cleanup-perceptual"):
    return database.query_one(
        "SELECT * FROM review_runs WHERE root_id=? AND run_type=? AND status='pending'",
        (root_id, run_type),
    )


def _trash_asset_from(database, path, media_type="photo"):
    """Insert a zero-instance trashed asset from a file's hash + PDQ (a refresh-then-empty state)."""
    from packrat import media
    from packrat.config import Config

    fp = media.fingerprint(str(path), os.path.getsize(path), Config())
    with database.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO assets(content_hash, media_type, size, width, height, status, "
            "undecodable, added_at, trashed_at, trash_reason) "
            "VALUES (?,?,?,?,?, 'trashed', ?, 't', 't', 'trash-folder')",
            (fp.content_hash, fp.media_type, fp.size, fp.width, fp.height,
             1 if fp.undecodable else 0),
        )
        aid = int(cur.lastrowid)
        from packrat.jobs import scan as scanmod

        scanmod._insert_perceptual(conn, aid, fp)
    return aid


# ---------------------------------------------------------------------------
# validation (everywhere)
# ---------------------------------------------------------------------------
def test_cleanup_rejects_trash_root(queue_and_db, tmp_path):
    q, database = queue_and_db
    trash = tmp_path / "Trash"
    trash.mkdir()
    root = register(database, str(trash), kind="trash")
    _run(q, database, "cleanup", expect="error", root_id=root["id"])


def test_cleanup_blocked_by_pending_dedup(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    root = register(database, str(lib))
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
        "VALUES (?, 'dedup', 'pending', 1, 'staged', 't')",
        (root["id"],),
    )
    # A --perceptual analyze OWNS the root → the queue rejects it (per-root exclusivity).
    from packrat.jobs import BusyError

    with pytest.raises(BusyError):
        q.submit("cleanup", {"root_id": root["id"], "perceptual": True})
    # A default preview owns nothing but re-checks the holder in-handler → errors.
    _run(q, database, "cleanup", expect="error", root_id=root["id"])


# ---------------------------------------------------------------------------
# default exact mode
# ---------------------------------------------------------------------------
def test_cleanup_preview_counts_exact_trash(queue_and_db, tmp_path):
    """The preview (refresh + count) reports exact-trash matches without deleting."""
    q, database = queue_and_db
    from packrat import queries

    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "junk.png", 1)
    root = register(database, str(lib))
    _run(q, database, "scan", root_id=root["id"])
    # Trash that content (as a refresh flip would): the library instance stays.
    database.execute("UPDATE assets SET status='trashed', trash_reason='trash-folder'")

    _run(q, database, "cleanup", root_id=root["id"])  # preview (no flags): act on nothing
    prev = queries.cleanup_exact_preview(str(lib))
    assert prev["count"] == 1
    # Nothing deleted by the preview.
    assert database.query_one("SELECT COUNT(*) c FROM file_instances")["c"] == 1


@win_only
def test_cleanup_default_exact_apply_deletes(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "junk.png", 1)
    _photo(lib / "keep.png", 2)
    root = register(database, str(lib))
    _run(q, database, "scan", root_id=root["id"])
    # Mark junk.png's asset trashed (exact-trash re-appearance).
    junk_asset = database.query_one(
        "SELECT asset_id FROM file_instances WHERE filename='junk.png'"
    )["asset_id"]
    database.execute("UPDATE assets SET status='trashed', trash_reason='trash-folder' WHERE id=?",
                     (junk_asset,))

    _run(q, database, "cleanup", root_id=root["id"], apply=True)
    # junk.png deleted; its asset stays trashed (fingerprints retained); keep.png intact.
    assert not (lib / "junk.png").exists()
    assert (lib / "keep.png").exists()
    assert database.query_one(
        "SELECT COUNT(*) c FROM file_instances WHERE asset_id=?", (junk_asset,)
    )["c"] == 0
    assert database.query_one("SELECT status FROM assets WHERE id=?", (junk_asset,))["status"] == "trashed"


# ---------------------------------------------------------------------------
# --perceptual analyze → confirm / cancel
# ---------------------------------------------------------------------------
@win_only
def test_cleanup_perceptual_stage_confirm_deletes(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    # A recompressed copy (JPEG) of trashed PNG content lives in the library.
    master = tmp_path / "master.png"
    _photo(master, 5, kind="PNG")
    _photo(lib / "recompressed.jpg", 5, kind="JPEG", quality=80)
    root = register(database, str(lib))
    _run(q, database, "scan", root_id=root["id"])
    _trash_asset_from(database, master)  # zero-instance trashed asset (matches the jpg)

    _run(q, database, "cleanup", root_id=root["id"], perceptual=True)
    run = _run_row(database, root["id"])
    assert run is not None and run["stage"] == 1
    acts = database.query("SELECT * FROM review_actions WHERE run_id=?", (run["id"],))
    perceptual = [a for a in acts if a["kind"] == "perceptual"]
    assert len(perceptual) == 1 and perceptual[0]["matched_trashed_asset_id"] is not None
    stage_dir = review.staging_folder(str(lib), review.PERCEPTUAL_TRASH)
    assert os.path.exists(os.path.join(stage_dir, perceptual[0]["shortcut_name"]))

    # delete-default: leave the shortcut in place → confirm deletes it.
    _run(q, database, "cleanup", root_id=root["id"], confirm=True)
    assert not (lib / "recompressed.jpg").exists()
    assert _run_row(database, root["id"]) is None
    # The deleted near-dup's own asset is now trashed (cleanup-perceptual).
    tr = database.query_one(
        "SELECT status, trash_reason FROM assets WHERE id=?", (perceptual[0]["asset_id"],)
    )
    assert tr["status"] == "trashed" and tr["trash_reason"] == "cleanup-perceptual"


@win_only
def test_cleanup_perceptual_spare_by_removing_shortcut(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    master = tmp_path / "master.png"
    _photo(master, 6, kind="PNG")
    _photo(lib / "recompressed.jpg", 6, kind="JPEG", quality=80)
    root = register(database, str(lib))
    _run(q, database, "scan", root_id=root["id"])
    _trash_asset_from(database, master)

    _run(q, database, "cleanup", root_id=root["id"], perceptual=True)
    run = _run_row(database, root["id"])
    a = database.query_one("SELECT * FROM review_actions WHERE run_id=? AND kind='perceptual'", (run["id"],))
    # Remove the shortcut → spare the file (delete-default veto).
    os.remove(os.path.join(review.staging_folder(str(lib), review.PERCEPTUAL_TRASH), a["shortcut_name"]))
    _run(q, database, "cleanup", root_id=root["id"], confirm=True)
    assert (lib / "recompressed.jpg").exists()  # spared
    assert _run_row(database, root["id"]) is None
    # Spared → NOT trashed.
    assert database.query_one("SELECT status FROM assets WHERE id=?", (a["asset_id"],))["status"] == "active"


@win_only
def test_cleanup_perceptual_confirm_deletes_exact_too(queue_and_db, tmp_path):
    """In --perceptual mode, exact trash matches are deleted at --confirm (not inline)."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "exact_junk.png", 3)
    master = tmp_path / "master.png"
    _photo(master, 8, kind="PNG")
    _photo(lib / "recompressed.jpg", 8, kind="JPEG", quality=80)
    root = register(database, str(lib))
    _run(q, database, "scan", root_id=root["id"])
    # exact_junk.png's asset → trashed (exact re-appearance).
    database.execute(
        "UPDATE assets SET status='trashed', trash_reason='trash-folder' WHERE id="
        "(SELECT asset_id FROM file_instances WHERE filename='exact_junk.png')"
    )
    _trash_asset_from(database, master)  # perceptual match for recompressed.jpg

    _run(q, database, "cleanup", root_id=root["id"], perceptual=True)
    run = _run_row(database, root["id"])
    acts = database.query("SELECT * FROM review_actions WHERE run_id=?", (run["id"],))
    assert sum(1 for a in acts if a["kind"] == "exact") == 1
    # exact_junk.png must NOT be deleted at analyze (deferred to confirm).
    assert (lib / "exact_junk.png").exists()

    _run(q, database, "cleanup", root_id=root["id"], confirm=True)
    assert not (lib / "exact_junk.png").exists()   # exact deleted at confirm
    assert not (lib / "recompressed.jpg").exists()  # perceptual deleted at confirm


@win_only
def test_cleanup_perceptual_cancel_discards(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    master = tmp_path / "master.png"
    _photo(master, 9, kind="PNG")
    _photo(lib / "recompressed.jpg", 9, kind="JPEG", quality=80)
    root = register(database, str(lib))
    _run(q, database, "scan", root_id=root["id"])
    _trash_asset_from(database, master)
    _run(q, database, "cleanup", root_id=root["id"], perceptual=True)
    run = _run_row(database, root["id"])
    _run(q, database, "cleanup", root_id=root["id"], cancel=True)
    r = database.query_one("SELECT status FROM review_runs WHERE id=?", (run["id"],))
    assert r["status"] == "cancelled"
    assert (lib / "recompressed.jpg").exists()  # nothing deleted
    assert not os.path.exists(review.staging_folder(str(lib), review.PERCEPTUAL_TRASH))


@win_only
def test_cleanup_perceptual_confirm_aborts_if_folder_missing(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    master = tmp_path / "master.png"
    _photo(master, 4, kind="PNG")
    _photo(lib / "recompressed.jpg", 4, kind="JPEG", quality=80)
    root = register(database, str(lib))
    _run(q, database, "scan", root_id=root["id"])
    _trash_asset_from(database, master)
    _run(q, database, "cleanup", root_id=root["id"], perceptual=True)
    # Delete the whole staging folder → confirm must ABORT (delete-default: never "delete all").
    review.remove_tree(review.staging_folder(str(lib), review.PERCEPTUAL_TRASH))
    _run(q, database, "cleanup", expect="error", root_id=root["id"], confirm=True)
    assert _run_row(database, root["id"]) is not None  # still pending
    assert (lib / "recompressed.jpg").exists()


# ---------------------------------------------------------------------------
# refresh interaction + dry-run
# ---------------------------------------------------------------------------
def test_cleanup_refreshes_trash_first(queue_and_db, tmp_path):
    """cleanup runs refresh-trash first, so a just-dropped trash file is absorbed (§6.2)."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "junk.png", 1)
    root = register(database, str(lib))
    _run(q, database, "scan", root_id=root["id"])
    # Drop a byte-identical copy into a trash folder — NOT yet absorbed.
    import shutil

    trash = tmp_path / "Trash"
    trash.mkdir()
    shutil.copy(lib / "junk.png", trash / "same.png")
    register(database, str(trash), kind="trash")

    from packrat import queries

    _run(q, database, "cleanup", root_id=root["id"])  # preview refreshes trash first
    # Refresh flipped junk.png's asset to trashed → the preview now counts it.
    assert queries.cleanup_exact_preview(str(lib))["count"] == 1


# ---------------------------------------------------------------------------
# reconcile — an interrupted cleanup analyze rolls back (inherited from M3, §3)
# ---------------------------------------------------------------------------
def test_reconcile_rolls_back_interrupted_cleanup_analyze(queue_and_db, tmp_path):
    q, database = queue_and_db
    from packrat.jobs.reconcile import reconcile_on_startup

    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    root = register(database, str(lib))
    database.execute(
        "INSERT INTO jobs(type, status, total, done, started_at, params_json) "
        "VALUES ('cleanup','running',0,0,'t', ?)",
        (f'{{"root_id": {root["id"]}, "perceptual": true}}',),
    )
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
        "VALUES (?, 'cleanup-perceptual', 'pending', 1, 'staged', 't')",
        (root["id"],),
    )
    staging = review.staging_folder(str(lib), review.PERCEPTUAL_TRASH)
    review.ensure_dir(staging)

    summary = reconcile_on_startup(database)
    assert summary["rolled_back_runs"]
    assert _run_row(database, root["id"]) is None  # cancelled
    assert not os.path.exists(staging)


def test_reconcile_keeps_interrupted_cleanup_confirm_pending(queue_and_db, tmp_path):
    """An interrupted cleanup --confirm stays pending for an idempotent --confirm re-run (§3)."""
    q, database = queue_and_db
    from packrat.jobs.reconcile import reconcile_on_startup

    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "a.png", 1)
    root = register(database, str(lib))
    database.execute(
        "INSERT INTO jobs(type, status, total, done, started_at, params_json) "
        "VALUES ('cleanup','running',0,0,'t', ?)",
        (f'{{"root_id": {root["id"]}, "perceptual": true, "confirm": true}}',),
    )
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
        "VALUES (?, 'cleanup-perceptual', 'pending', 1, 'staged', 't')",
        (root["id"],),
    )
    summary = reconcile_on_startup(database)
    assert not summary["rolled_back_runs"]
    assert _run_row(database, root["id"]) is not None  # left pending for --confirm


def test_cleanup_dry_run_refreshes_but_deletes_nothing(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _photo(lib / "junk.png", 1)
    root = register(database, str(lib))
    _run(q, database, "scan", root_id=root["id"])
    import shutil

    trash = tmp_path / "Trash"
    trash.mkdir()
    shutil.copy(lib / "junk.png", trash / "same.png")
    register(database, str(trash), kind="trash")

    logs = _run_capture(q, database, "cleanup", root_id=root["id"], dry_run=True)
    blob = "\n".join(logs)
    assert "dry-run" in blob
    # Refresh ran for real (§6.1): the active asset was flipped to trashed …
    assert database.query_one(
        "SELECT status FROM assets WHERE id=(SELECT asset_id FROM file_instances WHERE filename='junk.png')"
    )["status"] == "trashed"
    # … but the library file was NOT deleted.
    assert (lib / "junk.png").exists()
