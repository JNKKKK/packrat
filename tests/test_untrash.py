r"""untrash (§6.3) — forget content from the trashed-hash set by presenting the file.

Pure DB/hash logic (no shell APIs), so these run everywhere. untrash never touches
disk; it hashes the presented file and edits ``assets`` rows only.
"""

from __future__ import annotations

import time

import pytest

from packrat import db
from packrat.jobs import JobQueue
from packrat.jobs import scan as _scan  # noqa: F401 - registers 'scan'
from packrat.jobs import trash_refresh as _trash_refresh  # noqa: F401 - registers 'trash-refresh'
from packrat.jobs import untrash as _untrash  # noqa: F401 - registers 'untrash'
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
    Image.fromarray(rng.integers(0, 256, (48, 48, 3), dtype="uint8")).save(path, format="PNG")


def test_untrash_reactivates_asset_with_live_instance(queue_and_db, tmp_path):
    """A trashed asset that still has a live instance → flipped back to active in place."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _png(lib / "photo.png", 1)
    root = register(database, str(lib))
    _run(q, database, "scan", root_id=root["id"])
    aid = database.query_one("SELECT id FROM assets")["id"]
    # Trash it directly (as a refresh flip would) — instance stays on disk.
    database.execute(
        "UPDATE assets SET status='trashed', trashed_at='t', trash_reason='trash-folder' WHERE id=?",
        (aid,),
    )
    # Present the same file → reactivate in place.
    _run(q, database, "untrash", path=str(lib / "photo.png"))
    row = database.query_one("SELECT status, trash_reason FROM assets WHERE id=?", (aid,))
    assert row["status"] == "active" and row["trash_reason"] is None


def test_untrash_forgets_zero_instance_asset(queue_and_db, tmp_path):
    """A trashed asset with zero instances → forgotten entirely (blocklist entry dropped)."""
    q, database = queue_and_db
    # A trash folder absorbed then emptied leaves a zero-instance trashed asset. We
    # simulate that state, keeping the actual file elsewhere to present to untrash.
    keep = tmp_path / "recovered"
    keep.mkdir()
    _png(keep / "IMG.png", 2)
    from packrat import media

    content_hash = media.hash_file(str(keep / "IMG.png"))
    database.execute(
        "INSERT INTO assets(content_hash, media_type, status, trash_reason, added_at) "
        "VALUES (?, 'photo', 'trashed', 'trash-folder', 't')",
        (content_hash,),
    )
    _run(q, database, "untrash", path=str(keep / "IMG.png"))
    # Forgotten: the content is now unknown → treated as brand-new on a future merge.
    assert database.query_one("SELECT COUNT(*) c FROM assets WHERE content_hash=?", (content_hash,))["c"] == 0


def test_untrash_active_and_unknown_are_noops(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _png(lib / "active.png", 3)
    _png(lib / "novel.png", 4)  # never cataloged
    root = register(database, str(lib))
    _run(q, database, "scan", root_id=root["id"])  # both become active assets
    n_before = database.query_one("SELECT COUNT(*) c FROM assets")["c"]
    # active.png is active → no-op; unknown content untrash never creates an asset.
    _run(q, database, "untrash", path=str(lib / "active.png"))
    novel_outside = tmp_path / "outside.png"
    _png(novel_outside, 99)
    _run(q, database, "untrash", path=str(novel_outside))
    assert database.query_one("SELECT COUNT(*) c FROM assets")["c"] == n_before
    assert database.query_one(
        "SELECT status FROM assets WHERE id=(SELECT asset_id FROM file_instances "
        "WHERE filename='active.png')"
    )["status"] == "active"


def test_untrash_dry_run_changes_nothing(queue_and_db, tmp_path):
    q, database = queue_and_db
    keep = tmp_path / "recovered"
    keep.mkdir()
    _png(keep / "IMG.png", 5)
    from packrat import media

    content_hash = media.hash_file(str(keep / "IMG.png"))
    database.execute(
        "INSERT INTO assets(content_hash, media_type, status, trash_reason, added_at) "
        "VALUES (?, 'photo', 'trashed', 'trash-folder', 't')",
        (content_hash,),
    )
    _run(q, database, "untrash", path=str(keep / "IMG.png"), dry_run=True)
    # Dry-run: the trashed asset is untouched (still trashed, still present).
    assert database.query_one(
        "SELECT status FROM assets WHERE content_hash=?", (content_hash,)
    )["status"] == "trashed"


def test_untrash_folder_recursive(queue_and_db, tmp_path):
    """A folder arg walks recursively (allowlist/ignore as scan); non-media skipped."""
    q, database = queue_and_db
    from packrat import media

    src = tmp_path / "recovered"
    (src / "sub").mkdir(parents=True)
    _png(src / "a.png", 10)
    _png(src / "sub" / "b.png", 11)
    (src / "notes.txt").write_text("ignored")
    hashes = [media.hash_file(str(src / "a.png")), media.hash_file(str(src / "sub" / "b.png"))]
    for h in hashes:
        database.execute(
            "INSERT INTO assets(content_hash, media_type, status, trash_reason, added_at) "
            "VALUES (?, 'photo', 'trashed', 'trash-folder', 't')",
            (h,),
        )
    _run(q, database, "untrash", path=str(src))
    # Both media forgotten; the .txt was never a candidate.
    for h in hashes:
        assert database.query_one("SELECT COUNT(*) c FROM assets WHERE content_hash=?", (h,))["c"] == 0


def test_untrash_owns_no_root(queue_and_db, tmp_path):
    """untrash takes a worker slot but owns no root → not blocked by a pending review (§3)."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    _png(lib / "a.png", 1)
    root = register(database, str(lib))
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
        "VALUES (?, 'dedup', 'pending', 1, 'staged', 't')",
        (root["id"],),
    )
    # A pending dedup on lib must NOT block untrash (it owns no root).
    _run(q, database, "untrash", path=str(lib / "a.png"))
