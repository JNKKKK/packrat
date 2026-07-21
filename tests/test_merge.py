r"""merge (§8 C) — copy into a folder only what's new, decided by exact hash.

Drives the real ``merge`` handler through a ``JobQueue`` + ``Database`` (as the
dedup/scan/cleanup tests do). Merge is **copy-only** (``shutil.copyfile`` + atomic
rename), so — unlike dedup/cleanup/trash-refresh, which recycle files — its copy path
needs no Windows shell APIs and runs everywhere. The only Windows-gated part is trash
*emptying* inside the Phase-0 refresh; we register trash roots only where the refresh
behavior is under test.

Real PNGs so the (later) fingerprint path is meaningful, though merge itself hashes
bytes only (BLAKE3) and never decodes for classification.
"""

from __future__ import annotations

import os
import shutil
import sys
import time

import pytest

from packrat import db
from packrat.jobs import JobQueue
from packrat.jobs import merge as _merge  # noqa: F401 - registers 'merge'
from packrat.jobs import scan as _scan  # noqa: F401 - registers 'scan'
from packrat.jobs import trash_refresh as _trash_refresh  # noqa: F401 - registers 'trash-refresh'
from packrat.roots import register

pytest.importorskip("blake3")
pytest.importorskip("PIL")

WINDOWS = sys.platform == "win32"
win_only = pytest.mark.skipif(not WINDOWS, reason="trash emptying needs Windows shell APIs")


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
        if row and row["status"] not in ("running", "queued"):
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


def _png(path, seed):
    import numpy as np
    from PIL import Image

    os.makedirs(os.path.dirname(path), exist_ok=True)
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 256, size=(32, 32, 3), dtype=np.uint8)
    Image.fromarray(arr).save(path, format="PNG")


def _merge_job(q, database, source, dest_root, expect="done", **params):
    """Submit a merge with the params the daemon would freeze (root_id + dest_path)."""
    return _run(q, database, "merge", expect=expect,
                root_id=dest_root["id"], source=str(source),
                dest_path=params.pop("dest_path", str(dest_root_path(dest_root))),
                **params)


def dest_root_path(root):
    return root["path"]


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def test_merge_rejects_missing_source(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    root = register(database, str(lib))
    _run(q, database, "merge", expect="error", root_id=root["id"],
         source=str(tmp_path / "nope"), dest_path=str(lib))


def test_merge_rejects_empty_source(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    root = register(database, str(lib))
    _run(q, database, "merge", expect="error", root_id=root["id"],
         source=str(src), dest_path=str(lib))


def test_merge_rejects_source_dest_overlap(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    _png(lib / "a.png", 1)
    root = register(database, str(lib))
    # source == a subfolder of dest → overlap.
    sub = lib / "sub"
    _png(sub / "b.png", 2)
    _run(q, database, "merge", expect="error", root_id=root["id"],
         source=str(sub), dest_path=str(lib))


# ---------------------------------------------------------------------------
# classification + copy (copy-only → runs everywhere)
# ---------------------------------------------------------------------------
def test_merge_copies_new_files(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    root = register(database, str(lib))
    src = tmp_path / "src"
    _png(src / "new1.png", 1)
    _png(src / "new2.png", 2)

    _merge_job(q, database, src, root)
    # Both copied + registered as active assets.
    assert (lib / "new1.png").exists() and (lib / "new2.png").exists()
    assert database.query_one("SELECT COUNT(*) c FROM assets WHERE status='active'")["c"] == 2
    assert database.query_one("SELECT COUNT(*) c FROM file_instances")["c"] == 2
    # Merge-created assets are un-perceptual (no phash yet — a later scan backfills).
    assert database.query_one("SELECT COUNT(*) c FROM phash")["c"] == 0
    # merge_runs finalized as history.
    mr = database.query_one("SELECT status FROM merge_runs")
    assert mr["status"] == "done"


def test_merge_mirrors_structure(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    root = register(database, str(lib))
    src = tmp_path / "src"
    _png(src / "2024" / "jan" / "IMG.png", 5)

    _merge_job(q, database, src, root)
    assert (lib / "2024" / "jan" / "IMG.png").exists()
    inst = database.query_one("SELECT path FROM file_instances")
    assert inst["path"].endswith(os.path.join("2024", "jan", "IMG.png"))


def test_merge_skips_exact_known(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    _png(lib / "have.png", 7)
    root = register(database, str(lib))
    _run(q, database, "scan", root_id=root["id"])  # 'have.png' now an active asset

    src = tmp_path / "src"
    shutil.copytree(lib, src)  # byte-identical copy of have.png in the source
    _png(src / "brand_new.png", 8)

    logs = _run_capture(q, database, "merge", root_id=root["id"], source=str(src), dest_path=str(lib))
    blob = "\n".join(logs)
    assert "1 copied (new)" in blob and "1 exact-known" in blob
    # brand_new.png copied; the exact-known one not re-copied.
    assert (lib / "brand_new.png").exists()
    assert database.query_one("SELECT COUNT(*) c FROM assets WHERE status='active'")["c"] == 2


def test_merge_discards_trashed(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    root = register(database, str(lib))
    src = tmp_path / "src"
    _png(src / "junk.png", 3)
    _png(src / "keep.png", 4)

    # Pre-trash junk.png's content (zero-instance trashed asset, as a refresh would leave).
    from packrat import media
    from packrat.config import Config

    ch = media.hash_file(str(src / "junk.png"))
    database.execute(
        "INSERT INTO assets(content_hash, media_type, size, status, undecodable, added_at, "
        "trashed_at, trash_reason) VALUES (?, 'photo', 0, 'trashed', 0, 't', 't', 'trash-folder')",
        (ch,),
    )

    logs = _run_capture(q, database, "merge", root_id=root["id"], source=str(src), dest_path=str(lib))
    assert "1 trashed" in "\n".join(logs)
    assert not (lib / "junk.png").exists()   # trashed content discarded
    assert (lib / "keep.png").exists()       # new content copied


def test_merge_collapses_dup_in_source(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    root = register(database, str(lib))
    src = tmp_path / "src"
    _png(src / "a.png", 9)
    shutil.copy(src / "a.png", src / "a_dup.png")  # byte-identical sibling

    logs = _run_capture(q, database, "merge", root_id=root["id"], source=str(src), dest_path=str(lib))
    assert "1 dup-in-source" in "\n".join(logs)
    # Exactly one instance copied for the collapsed pair.
    assert database.query_one("SELECT COUNT(*) c FROM assets WHERE status='active'")["c"] == 1
    copied = list((lib).glob("*.png"))
    assert len(copied) == 1


def test_merge_collision_rename(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    _png(lib / "IMG.png", 1)   # a DIFFERENT-content file already at this rel path (unscanned)
    root = register(database, str(lib))
    src = tmp_path / "src"
    _png(src / "IMG.png", 2)   # different bytes, same name

    _merge_job(q, database, src, root)
    # Different content at the same name → numeric-suffix rename.
    assert (lib / "IMG.png").exists() and (lib / "IMG (1).png").exists()


def test_merge_collision_identical_no_dup(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    _png(lib / "IMG.png", 1)   # already there, unscanned
    root = register(database, str(lib))
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(lib / "IMG.png", src / "IMG.png")  # byte-identical, same name

    _merge_job(q, database, src, root)
    # Identical content already present → reuse in place, register it, no "(1)" copy.
    assert not (lib / "IMG (1).png").exists()
    assert database.query_one("SELECT COUNT(*) c FROM file_instances")["c"] == 1


def test_merge_source_never_modified(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    root = register(database, str(lib))
    src = tmp_path / "src"
    _png(src / "a.png", 1)
    _png(src / "sub" / "b.png", 2)
    before = {p.name: p.stat().st_size for p in src.rglob("*.png")}

    _merge_job(q, database, src, root)
    after = {p.name: p.stat().st_size for p in src.rglob("*.png")}
    assert before == after  # source untouched


# ---------------------------------------------------------------------------
# ignored-dest handling (§8 C step 11/13)
# ---------------------------------------------------------------------------
def test_merge_ignored_dest_copies_but_does_not_register(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    # Register with an ignore glob that will match the dest subpath.
    root = register(database, str(lib), ignore_globs=["Screenshots/"])
    src = tmp_path / "src"
    _png(src / "Screenshots" / "shot.png", 1)
    _png(src / "keep.png", 2)

    logs = _run_capture(q, database, "merge", root_id=root["id"], source=str(src), dest_path=str(lib))
    # Both files land on disk (structure mirrored) …
    assert (lib / "Screenshots" / "shot.png").exists()
    assert (lib / "keep.png").exists()
    # … but the ignored one is NOT catalogued (only keep.png registered).
    assert database.query_one("SELECT COUNT(*) c FROM file_instances")["c"] == 1
    inst = database.query_one("SELECT path FROM file_instances")
    assert inst["path"].endswith("keep.png")
    blob = "\n".join(logs)
    assert "ignored path" in blob
    # The plan records the unindexed disposition.
    assert database.query_one(
        "SELECT COUNT(*) c FROM merge_plan_items WHERE progress='copied-unindexed'"
    )["c"] == 1


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------
def test_merge_dry_run_copies_nothing(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    root = register(database, str(lib))
    src = tmp_path / "src"
    _png(src / "new1.png", 1)
    _png(src / "new2.png", 2)

    logs = _run_capture(q, database, "merge", root_id=root["id"], source=str(src),
                        dest_path=str(lib), dry_run=True)
    blob = "\n".join(logs)
    assert "dry-run" in blob and "2 would copy (new)" in blob
    # Nothing copied, no rows written.
    assert not (lib / "new1.png").exists()
    assert database.query_one("SELECT COUNT(*) c FROM assets")["c"] == 0
    assert database.query_one("SELECT COUNT(*) c FROM merge_runs")["c"] == 0
    assert database.query_one("SELECT COUNT(*) c FROM merge_plan_items")["c"] == 0


def test_merge_dry_run_warns_ignored_dest(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    root = register(database, str(lib), ignore_globs=["Screenshots/"])
    src = tmp_path / "src"
    _png(src / "Screenshots" / "shot.png", 1)

    logs = _run_capture(q, database, "merge", root_id=root["id"], source=str(src),
                        dest_path=str(lib), dry_run=True)
    assert "ignored path" in "\n".join(logs)


# ---------------------------------------------------------------------------
# resume from the frozen plan (§8 C Safety & resume)
# ---------------------------------------------------------------------------
def test_merge_resume_finishes_copied_but_unregistered(queue_and_db, tmp_path):
    """A crash between rename and DB register leaves progress='copied' → resume registers it."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    root = register(database, str(lib))
    src = tmp_path / "src"
    _png(src / "a.png", 1)

    # Hand-build an open 'copying' run with one 'copied' item (file already on disk).
    from packrat import media

    dest_file = lib / "a.png"
    shutil.copy(src / "a.png", dest_file)
    ch = media.hash_file(str(dest_file))
    cur = database.execute(
        "INSERT INTO merge_runs(source_path, dest_path, dest_root_id, status, created_at) "
        "VALUES (?,?,?, 'copying', 't')",
        (str(src), str(lib), root["id"]),
    )
    run_id = int(cur.lastrowid)
    database.execute(
        "INSERT INTO merge_plan_items(run_id, source_rel_path, size, mtime, content_hash, "
        "classification, dest_path, progress) VALUES (?,?,?,?,?, 'new', ?, 'copied')",
        (run_id, "a.png", dest_file.stat().st_size, dest_file.stat().st_mtime, ch, str(dest_file)),
    )

    # Re-run merge → auto-resumes: registers the already-copied file, no re-copy.
    _merge_job(q, database, src, root)
    assert database.query_one(
        "SELECT progress FROM merge_plan_items WHERE run_id=?", (run_id,)
    )["progress"] == "registered"
    assert database.query_one("SELECT COUNT(*) c FROM file_instances")["c"] == 1
    assert database.query_one("SELECT status FROM merge_runs WHERE id=?", (run_id,))["status"] == "done"


def test_merge_resume_skips_already_registered(queue_and_db, tmp_path):
    """A terminal 'registered' item is counted on resume without re-touching disk."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    root = register(database, str(lib))
    src = tmp_path / "src"
    _png(src / "a.png", 1)
    _png(src / "b.png", 2)

    _merge_job(q, database, src, root)  # full run
    # Second identical merge: source files are now exact-known → nothing new copies.
    logs = _run_capture(q, database, "merge", root_id=root["id"], source=str(src), dest_path=str(lib))
    assert "0 copied (new)" in "\n".join(logs)
    assert "2 exact-known" in "\n".join(logs)


def test_merge_phase1_persists_hashes_incrementally_and_resume_skips(queue_and_db, tmp_path, monkeypatch):
    """Phase 1 UPSERTs each source hash as it's computed, so a crash mid-hash keeps the
    work; a `planning`-resume skips already-hashed files (§8 C SMB-cost avoidance)."""
    from packrat import media
    from packrat.jobs import merge as merge_mod

    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    root = register(database, str(lib))
    src = tmp_path / "src"
    _png(src / "a.png", 1)
    _png(src / "b.png", 2)

    # An open 'planning' run (Phase 1 not yet done) — the state a crash before the
    # copying-flip leaves. Hand-build it so we can drive _build_plan directly.
    cur = database.execute(
        "INSERT INTO merge_runs(source_path, dest_path, dest_root_id, status, created_at) "
        "VALUES (?,?,?, 'planning', 't')", (str(src), str(lib), root["id"]),
    )
    run_id = int(cur.lastrowid)
    run = dict(database.query_one("SELECT * FROM merge_runs WHERE id=?", (run_id,)))

    # Crash Phase 1 after the FIRST hash: wrap hash_file to raise on the 2nd call.
    real_hash = media.hash_file
    calls = {"n": 0}

    def flaky_hash(path):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash mid-Phase-1")
        return real_hash(path)

    monkeypatch.setattr(merge_mod.media, "hash_file", flaky_hash)
    import threading

    from packrat.config import Config
    from packrat.jobs.context import JobContext

    ctx = JobContext(0, "merge", {}, Config(), database,
                     emit=lambda ev: None, set_progress=lambda d, t: None,
                     cancel_event=threading.Event())
    with pytest.raises(RuntimeError):
        merge_mod._build_plan(ctx, run)

    # The first file's hash was persisted despite the crash (incremental UPSERT).
    rows = database.query("SELECT source_rel_path, content_hash FROM merge_plan_items WHERE run_id=?", (run_id,))
    hashed = {r["source_rel_path"]: r["content_hash"] for r in rows if r["content_hash"] is not None}
    assert len(hashed) == 1, rows

    # Resume with the real hasher: the already-hashed file must NOT be re-read.
    monkeypatch.setattr(merge_mod.media, "hash_file", real_hash)
    reread = {"n": 0}
    orig = real_hash

    def counting_hash(path):
        reread["n"] += 1
        return orig(path)

    monkeypatch.setattr(merge_mod.media, "hash_file", counting_hash)
    merge_mod._build_plan(ctx, dict(database.query_one("SELECT * FROM merge_runs WHERE id=?", (run_id,))))
    # Only the ONE not-yet-hashed file is read on resume (the first is reused).
    assert reread["n"] == 1, f"resume re-hashed {reread['n']} files (expected 1)"
    both = database.query("SELECT content_hash FROM merge_plan_items WHERE run_id=?", (run_id,))
    assert all(r["content_hash"] is not None for r in both) and len(both) == 2


# ---------------------------------------------------------------------------
# cross-op guard (§8 C Phase 0 step 2a) — held behind a pending review
# ---------------------------------------------------------------------------
def test_merge_held_by_pending_dedup(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    _png(lib / "a.png", 1)
    root = register(database, str(lib))
    src = tmp_path / "src"
    _png(src / "new.png", 2)
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
        "VALUES (?, 'dedup', 'pending', 1, 'staged', 't')",
        (root["id"],),
    )
    jid = q.submit("merge", {"root_id": root["id"], "source": str(src), "dest_path": str(lib)})
    # Held in the backlog, not run — the dest root has a pending dedup.
    assert database.query_one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "queued"
    assert q.blocked_reason("merge", {"root_id": root["id"]}) is not None
    q.cancel(jid)


def test_merge_dry_run_owns_no_root(queue_and_db, tmp_path):
    """A --dry-run merge owns no root, so a pending review does NOT hold it (writes nothing)."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    _png(lib / "a.png", 1)
    root = register(database, str(lib))
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
        "VALUES (?, 'dedup', 'pending', 1, 'staged', 't')",
        (root["id"],),
    )
    assert q.blocked_reason("merge", {"root_id": root["id"], "dry_run": True}) is None


def test_merge_refreshes_trash_first(queue_and_db, tmp_path):
    """merge runs refresh-trash first, so a just-dropped trash file is absorbed + excludes a match (§8 C)."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    root = register(database, str(lib))
    src = tmp_path / "src"
    _png(src / "junk.png", 3)

    # Drop a byte-identical copy of junk.png into a trash folder (not yet absorbed).
    trash = tmp_path / "Trash"
    trash.mkdir()
    shutil.copy(src / "junk.png", trash / "junk.png")
    register(database, str(trash), kind="trash")

    logs = _run_capture(q, database, "merge", root_id=root["id"], source=str(src), dest_path=str(lib))
    # Refresh absorbed the trash file → junk.png now matches a trashed hash → discarded.
    assert "1 trashed" in "\n".join(logs)
    assert not (lib / "junk.png").exists()
