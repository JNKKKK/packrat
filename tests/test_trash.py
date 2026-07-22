r"""trash refresh (§6.1) — absorb trash-folder files into the trashed set + empty.

Drives the real ``trash-refresh`` handler through a ``JobQueue`` + ``Database`` (as
the dedup/scan tests do). Emptying moves files to the Recycle Bin (``send2trash``),
which needs Windows shell APIs — so the *delete* assertions are ``@win_only``; the
record-to-DB assertions (the important half) run everywhere. Uses real PNGs so the
decode→PDQ path runs and the trashed asset gets its perceptual signature.
"""

from __future__ import annotations

import sys
import time

import pytest

from packrat import db
from packrat.jobs import JobQueue
from packrat.jobs import scan as _scan  # noqa: F401 - registers 'scan'
from packrat.jobs import trash_refresh as _trash_refresh  # noqa: F401 - registers 'trash-refresh'
from packrat.roots import register

pytest.importorskip("blake3")
pytest.importorskip("PIL")
pytest.importorskip("pdqhash")

WINDOWS = sys.platform == "win32"
win_only = pytest.mark.skipif(not WINDOWS, reason="emptying trash needs Windows shell APIs")


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


def _png(path, seed):
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 256, size=(48, 48, 3), dtype=np.uint8)
    Image.fromarray(arr).save(path, format="PNG")


# ---------------------------------------------------------------------------
# recording (runs everywhere — the DB half)
# ---------------------------------------------------------------------------
def test_refresh_new_content_creates_trashed_asset(queue_and_db, tmp_path):
    q, database = queue_and_db
    trash = tmp_path / "Trash"
    trash.mkdir()
    _png(trash / "junk.png", 1)
    register(database, str(trash), kind="trash")

    _run(q, database, "trash-refresh")
    # A brand-new trashed asset with its perceptual signature (photo → phash row).
    a = database.query_one("SELECT id, status, trash_reason FROM assets")
    assert a["status"] == "trashed" and a["trash_reason"] == "trash-folder"
    assert database.query_one("SELECT COUNT(*) c FROM phash WHERE asset_id=?", (a["id"],))["c"] == 1


def test_refresh_flips_matching_active_asset(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _png(lib / "keep.png", 7)
    lib_root = register(database, str(lib))
    _run(q, database, "scan", root_id=lib_root["id"])
    asset = database.query_one("SELECT id, status FROM assets")
    assert asset["status"] == "active"

    # Drop a byte-identical copy into a trash folder → refresh flips the asset.
    import shutil

    trash = tmp_path / "Trash"
    trash.mkdir()
    shutil.copy(lib / "keep.png", trash / "same.png")
    register(database, str(trash), kind="trash")
    _run(q, database, "trash-refresh")

    row = database.query_one("SELECT status, trash_reason FROM assets WHERE id=?", (asset["id"],))
    assert row["status"] == "trashed" and row["trash_reason"] == "trash-folder"
    # The library instance stays on disk (cleanup removes it later), so the asset
    # keeps its instance — a trashed asset legitimately still has instances here.
    assert database.query_one(
        "SELECT COUNT(*) c FROM file_instances WHERE asset_id=?", (asset["id"],)
    )["c"] == 1


def test_refresh_no_trash_roots_is_noop(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    register(database, str(lib))  # a library root, not trash
    _run(q, database, "trash-refresh")
    assert database.query_one("SELECT COUNT(*) c FROM assets")["c"] == 0


def test_refresh_idempotent_rerun(queue_and_db, tmp_path):
    """A second refresh of the same (undeleted) file is a DB no-op, not a dup asset."""
    q, database = queue_and_db
    trash = tmp_path / "Trash"
    trash.mkdir()
    _png(trash / "junk.png", 3)
    register(database, str(trash), kind="trash")
    _run(q, database, "trash-refresh")
    n1 = database.query_one("SELECT COUNT(*) c FROM assets")["c"]
    # Re-run: on non-Windows the file wasn't emptied, so it's re-hashed → must hit
    # the existing trashed asset (already_trashed), never create a second.
    _run(q, database, "trash-refresh")
    assert database.query_one("SELECT COUNT(*) c FROM assets")["c"] == n1


# ---------------------------------------------------------------------------
# single-root refresh  (trash refresh <root>)
# ---------------------------------------------------------------------------
def test_refresh_single_root_only_absorbs_that_root(queue_and_db, tmp_path):
    """`trash refresh <root>` (root_id param) absorbs ONLY that trash root, not the others."""
    q, database = queue_and_db
    t1 = tmp_path / "Trash1"
    t1.mkdir()
    _png(t1 / "a.png", 11)
    r1 = register(database, str(t1), kind="trash")
    t2 = tmp_path / "Trash2"
    t2.mkdir()
    _png(t2 / "b.png", 12)
    register(database, str(t2), kind="trash")

    # Scope to r1: exactly one trashed asset appears (t2 untouched).
    _run(q, database, "trash-refresh", root_id=r1["id"])
    assert database.query_one("SELECT COUNT(*) c FROM assets WHERE status='trashed'")["c"] == 1

    # A bare refresh then absorbs the remaining root too → two total.
    _run(q, database, "trash-refresh")
    assert database.query_one("SELECT COUNT(*) c FROM assets WHERE status='trashed'")["c"] == 2


def test_refresh_single_root_disabled_is_noop(queue_and_db, tmp_path):
    """A disabled trash root absorbs nothing — the handler never falls back to all-roots.

    (jobs.root_id is a FK, so a *nonexistent* id can't even be enqueued; the real
    defensive case the handler's ``enabled=1`` filter guards is a disabled root.)"""
    q, database = queue_and_db
    t1 = tmp_path / "Trash1"
    t1.mkdir()
    _png(t1 / "a.png", 13)
    r1 = register(database, str(t1), kind="trash")
    t2 = tmp_path / "Trash2"
    t2.mkdir()
    _png(t2 / "b.png", 15)
    register(database, str(t2), kind="trash")
    database.execute("UPDATE roots SET enabled=0 WHERE id=?", (r1["id"],))

    # Scoped to the DISABLED r1 → nothing absorbed; must NOT spill over to t2.
    _run(q, database, "trash-refresh", root_id=r1["id"])
    assert database.query_one("SELECT COUNT(*) c FROM assets")["c"] == 0


def test_refresh_single_root_rejects_library_id(queue_and_db, tmp_path):
    """Pointing the single-root refresh at a LIBRARY root's id absorbs nothing (kind gate)."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _png(lib / "keep.png", 14)
    lib_root = register(database, str(lib))  # library kind
    _run(q, database, "trash-refresh", root_id=lib_root["id"])
    # The library file is NOT absorbed as trash (kind='trash' filter excludes it).
    assert database.query_one("SELECT COUNT(*) c FROM assets WHERE status='trashed'")["c"] == 0


# ---------------------------------------------------------------------------
# emptying (Windows shell APIs)
# ---------------------------------------------------------------------------
@win_only
def test_refresh_empties_the_folder(queue_and_db, tmp_path):
    q, database = queue_and_db
    trash = tmp_path / "Trash"
    trash.mkdir()
    _png(trash / "junk.png", 5)
    register(database, str(trash), kind="trash")
    _run(q, database, "trash-refresh")
    # File absorbed AND emptied; its trashed fingerprint persists at zero instances.
    assert not (trash / "junk.png").exists()
    a = database.query_one("SELECT id, status FROM assets")
    assert a["status"] == "trashed"
    assert database.query_one(
        "SELECT COUNT(*) c FROM file_instances WHERE asset_id=?", (a["id"],)
    )["c"] == 0


@win_only
def test_scan_never_touches_trash_root(queue_and_db, tmp_path):
    """A manual scan of a trash root errors; --all skips it (§8 A2 step 1)."""
    q, database = queue_and_db
    trash = tmp_path / "Trash"
    trash.mkdir()
    _png(trash / "junk.png", 9)
    root = register(database, str(trash), kind="trash")
    _run(q, database, "scan", expect="error", root_id=root["id"])


def test_empty_file_network_fallback_permanently_deletes(tmp_path, monkeypatch):
    """When recycle() ERRORS on a NETWORK path (no Recycle Bin), _empty_file falls back
    to a permanent os.remove so the trash inbox is still emptied (§6.1/§10). A LOCAL
    recycle failure (locked file) is left in place + reported."""
    from packrat import trash

    f = tmp_path / "junk.png"
    f.write_bytes(b"x")

    def boom_recycle(path):
        raise OSError("no Recycle Bin on this volume")

    monkeypatch.setattr(trash.shortcuts, "recycle", boom_recycle)

    # Network path → permanent delete fallback empties it.
    monkeypatch.setattr(trash.fsutil, "is_network_path", lambda p: True)
    summary = {"emptied": 0, "undeletable": 0}
    trash._empty_file(str(f), summary)
    assert not f.exists() and summary == {"emptied": 1, "undeletable": 0}

    # Local path → left in place + reported undeletable (no permanent delete).
    g = tmp_path / "locked.png"
    g.write_bytes(b"y")
    monkeypatch.setattr(trash.fsutil, "is_network_path", lambda p: False)
    summary = {"emptied": 0, "undeletable": 0}
    trash._empty_file(str(g), summary)
    assert g.exists() and summary == {"emptied": 0, "undeletable": 1}
