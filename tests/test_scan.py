"""scan job + perceptual: new/dup, fast-path, deletion, undecodable, PDQ.

Drives the real scan handler through a ``JobQueue`` + ``Database`` (as test_jobs
does), against tiny real PNGs so the decode→hash→PDQ path actually runs. Requires
the ``media`` extra (blake3/pillow/pdqhash) — all confirmed by the smoke test.
"""

from __future__ import annotations

import os
import time

import pytest

from packrat import db
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


def _counts(database):
    return {
        "assets": database.query_one("SELECT COUNT(*) c FROM assets")["c"],
        "instances": database.query_one("SELECT COUNT(*) c FROM file_instances")["c"],
        "phash": database.query_one("SELECT COUNT(*) c FROM phash")["c"],
    }


def _run_scan_capture_logs(q, database, root_id=None, **params):
    """Run a scan and return its emitted ``ctx.log`` lines (via the SSE fan-out)."""
    if root_id is not None:
        params["root_id"] = root_id
    jid = q.submit("scan", params)
    sub = q.subscribe(jid)
    logs = []
    while True:
        ev = sub.q.get(timeout=30)
        if ev is None:
            break
        if ev.type == "log":
            logs.append(ev.message)
    row = database.query_one("SELECT status, error FROM jobs WHERE id=?", (jid,))
    assert row["status"] == "done", f"scan failed: {row['error']}"
    return logs


def test_plain_scan_sets_scan_recency_not_full(queue_and_db, tiny_photos):
    from packrat import queries

    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run_scan(q, database, root["id"])  # plain incremental, NOT --full
    snap = queries.roots_snapshot()[0]
    # A plain scan records general recency (max last_seen_at) ...
    assert snap["last_scan_at"] is not None
    # ... but does NOT stamp last_full_scan_at (only `scan --full` does).
    assert snap["last_full_scan_at"] is None
    det = queries.root_detail(root["name"])
    assert det["last_scan_at"] == snap["last_scan_at"]

    # A --full scan additionally stamps last_full_scan_at.
    _run_scan(q, database, root["id"], full=True)
    snap2 = queries.roots_snapshot()[0]
    assert snap2["last_full_scan_at"] is not None


def test_full_scan_of_offline_root_does_not_stamp_last_full_scan(queue_and_db, tmp_path):
    """An offline/unreadable root in a --full scan must NOT record last_full_scan_at — its
    enumeration failed, so nothing was fingerprinted or deletion-detected.

    A --all sweep skips+logs a busy root but still ENTERS the per-root loop for an offline
    one (offline is discovered at enumerate time, not the dequeue gate), so the post-scan
    roots writes must guard on root_offline exactly as the probe-signal clear does."""
    from packrat import queries

    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    root = register(database, str(lib))
    # Point the stored path at a now-missing dir so enumerate's first listing fails → offline.
    database.execute("UPDATE roots SET path=? WHERE id=?", (str(tmp_path / "gone"), root["id"]))
    _run_scan(q, database, root["id"], full=True)   # completes 'done', but the root is offline
    snap = queries.roots_snapshot()[0]
    assert snap["last_full_scan_at"] is None         # no full scan actually happened
    assert snap["probe_new_count"] == 0              # (untouched default; not falsely cleared either)


def test_roots_snapshot_media_split_and_dedup_recency(queue_and_db, tiny_photos):
    """roots_snapshot exposes photos/videos + last_dedup_at for the dot & sort."""
    from packrat import queries

    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run_scan(q, database, root["id"])
    snap = queries.roots_snapshot()[0]
    det = queries.root_detail(root["name"])
    # photos/videos split matches root_detail and sums to asset_count.
    assert snap["photos"] == det["photos"]
    assert snap["videos"] == det["videos"]
    assert snap["photos"] + snap["videos"] == snap["asset_count"]
    # Never deduped yet → last_dedup_at is NULL (the ◐ "scanned only" dot state).
    assert snap["last_dedup_at"] is None


def test_scan_new_and_exact_dup(queue_and_db, tiny_photos):
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run_scan(q, database, root["id"])
    c = _counts(database)
    # a.png, b.png distinct; a_copy.png is a byte-dup of a.png; notes.txt ignored.
    assert c["assets"] == 2
    assert c["instances"] == 3
    # A PDQ row per photo asset.
    assert c["phash"] == 2


def test_scan_fast_path_skips(queue_and_db, tiny_photos):
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run_scan(q, database, root["id"])
    jid = _run_scan(q, database, root["id"])
    row = database.query_one("SELECT total, done FROM jobs WHERE id=?", (jid,))
    # Second pass: all candidates fast-path-skipped, but the bar still reaches total.
    assert row["done"] == row["total"] == 3
    # No duplicate assets/instances created on re-scan.
    c = _counts(database)
    assert c["assets"] == 2 and c["instances"] == 3


def test_scan_sets_needs_dedup_only_when_it_indexes_new_content(queue_and_db, tiny_photos):
    """The dedup-dirty signal: a scan that indexes NEW content marks the root
    needs_dedup=1; a no-op re-scan (all fast-path skips) leaves the flag untouched. This
    is the fix for "a routine re-scan flipped a fully-deduped root back to ◉ yellow"."""
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run_scan(q, database, root["id"])                 # first scan indexes 2 new assets
    assert database.query_one("SELECT needs_dedup FROM roots WHERE id=?",
                              (root["id"],))["needs_dedup"] == 1
    # Simulate a completed dedup consuming the signal.
    database.execute("UPDATE roots SET needs_dedup=0 WHERE id=?", (root["id"],))
    # A no-op re-scan (everything fast-path-skips) must NOT re-dirty the root.
    _run_scan(q, database, root["id"])
    assert database.query_one("SELECT needs_dedup FROM roots WHERE id=?",
                              (root["id"],))["needs_dedup"] == 0, "no-op re-scan must stay clean"
    # But a genuinely new file re-dirties it.
    import numpy as np
    from PIL import Image
    arr = np.random.default_rng(7).integers(0, 256, (32, 32, 3), dtype=np.uint8)
    Image.fromarray(arr).save(tiny_photos / "fresh.png")
    _run_scan(q, database, root["id"])
    assert database.query_one("SELECT needs_dedup FROM roots WHERE id=?",
                              (root["id"],))["needs_dedup"] == 1


def test_scan_reappearing_trash_does_not_dirty_root(queue_and_db, tiny_photos):
    """A byte-identical TRASH re-appearance (matches_trashed) is NOT new dedup-able content
    — dedup ignores trashed assets — so it must not set needs_dedup (rung 3). Covers
    both the plain-attach and the backfill (perceptual-refill of a trashed asset) variants."""
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run_scan(q, database, root["id"])
    # Trash b.png's asset, drop its perceptual rows AND its instances so the on-disk file
    # re-appears and takes the backfill branch on the trashed asset (matches_trashed).
    a = database.query_one(
        "SELECT a.id FROM assets a JOIN file_instances fi ON fi.asset_id=a.id "
        "WHERE fi.filename='b.png' LIMIT 1")
    database.execute("UPDATE assets SET status='trashed' WHERE id=?", (a["id"],))
    database.execute("DELETE FROM phash WHERE asset_id=?", (a["id"],))
    database.execute("DELETE FROM file_instances WHERE asset_id=?", (a["id"],))
    database.execute("UPDATE roots SET needs_dedup=0 WHERE id=?", (root["id"],))  # start clean
    _run_scan(q, database, root["id"])                 # b.png hits the trashed asset
    assert database.query_one("SELECT needs_dedup FROM roots WHERE id=?",
                              (root["id"],))["needs_dedup"] == 0, "reappearing trash must not dirty"


def test_scan_undecodable_only_does_not_dirty_root(queue_and_db, tmp_path):
    """A scan that indexes ONLY an undecodable file gains no phash → nothing dedup-able →
    needs_dedup stays 0 (rung 3). An undecodable asset is hash-only, never a near-dup."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "broken.png").write_bytes(b"not a real png")
    root = register(database, str(lib))
    _run_scan(q, database, root["id"])
    assert database.query_one("SELECT COUNT(*) c FROM assets WHERE undecodable=1")["c"] == 1
    assert database.query_one("SELECT needs_dedup FROM roots WHERE id=?",
                              (root["id"],))["needs_dedup"] == 0


def test_scan_deletion_detection_forgets_asset(queue_and_db, tiny_photos):
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run_scan(q, database, root["id"])
    # Remove b.png (a unique asset) → its asset should be forgotten.
    (tiny_photos / "b.png").unlink()
    _run_scan(q, database, root["id"])
    c = _counts(database)
    assert c["assets"] == 1  # only a.png's asset remains
    assert c["instances"] == 2  # a.png + sub/a_copy.png
    assert c["phash"] == 1


def test_scan_deletes_whole_subfolder(queue_and_db, tiny_photos):
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run_scan(q, database, root["id"])
    # Deleting the entire sub/ folder must still forget the file under it (the
    # parent listed cleanly and simply no longer contains sub/) — regression for
    # the clean-dirs-vs-suppressed guard bug.
    import shutil

    shutil.rmtree(tiny_photos / "sub")
    _run_scan(q, database, root["id"])
    c = _counts(database)
    # a.png (asset), b.png (asset) survive; a_copy.png under sub/ is gone. a.png's
    # asset persists via its own instance.
    assert c["instances"] == 2
    orphans = database.query_one(
        "SELECT COUNT(*) c FROM assets WHERE id NOT IN (SELECT asset_id FROM file_instances)"
    )["c"]
    assert orphans == 0


def test_scan_in_place_edit_forgets_old_asset(queue_and_db, tmp_path):
    q, database = queue_and_db
    import numpy as np
    from PIL import Image

    lib = tmp_path / "lib"
    lib.mkdir()
    Image.fromarray(np.random.default_rng(1).integers(0, 256, (16, 16, 3), dtype=np.uint8)).save(lib / "a.png")
    root = register(database, str(lib))
    _run_scan(q, database, root["id"])
    assert _counts(database)["assets"] == 1
    # Overwrite a.png with different content: the instance repoints to a new asset;
    # the old asset must be forgotten (zero instances, active) — not orphaned.
    Image.fromarray(np.random.default_rng(2).integers(0, 256, (48, 48, 3), dtype=np.uint8)).save(lib / "a.png")
    _run_scan(q, database, root["id"], full=True)
    c = _counts(database)
    assert c["assets"] == 1 and c["instances"] == 1
    orphans = database.query_one(
        "SELECT COUNT(*) c FROM assets WHERE id NOT IN (SELECT asset_id FROM file_instances)"
    )["c"]
    assert orphans == 0


def test_scan_deletion_keeps_asset_with_other_instance(queue_and_db, tiny_photos):
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run_scan(q, database, root["id"])
    # Remove one of the two instances of a.png; the asset survives via the other.
    (tiny_photos / "sub" / "a_copy.png").unlink()
    _run_scan(q, database, root["id"])
    c = _counts(database)
    assert c["assets"] == 2
    assert c["instances"] == 2


# --- move detection ---------------------------------------------------------
def _one_png(path, seed=1):
    import numpy as np
    from PIL import Image
    arr = np.random.default_rng(seed).integers(0, 256, (32, 32, 3), dtype=np.uint8)
    Image.fromarray(arr).save(path, format="PNG")


def _moved_count(database, job_id):
    return database.query_one("SELECT moved FROM scan_results WHERE job_id=?", (job_id,))["moved"]


def test_scan_relinks_moved_file_without_rehash(queue_and_db, tmp_path):
    """A file moved to a new dir (same name/size/mtime, its bucket unambiguous) is
    RELINKED — the SAME file_instances row is repointed, no new asset/hash."""
    q, database = queue_and_db
    lib = tmp_path / "lib"; lib.mkdir()
    _one_png(lib / "a.png")
    root = register(database, str(lib))
    _run_scan(q, database, root["id"])
    before = database.query_one("SELECT id, asset_id FROM file_instances WHERE filename='a.png'")
    # Move (rename preserves mtime) into sub/, keeping the filename.
    (lib / "sub").mkdir()
    (lib / "a.png").rename(lib / "sub" / "a.png")
    jid = _run_scan(q, database, root["id"])
    after = database.query_one("SELECT id, asset_id, path FROM file_instances WHERE filename='a.png'")
    # Same instance row (repointed, not delete+recreate) + same asset (no re-hash).
    assert after["id"] == before["id"]
    assert after["asset_id"] == before["asset_id"]
    assert after["path"].endswith(os.path.join("sub", "a.png"))
    c = _counts(database)
    assert c["assets"] == 1 and c["instances"] == 1
    assert _moved_count(database, jid) == 1


def test_scan_move_is_dedup_neutral(queue_and_db, tmp_path):
    """A relinked move adds no new dedup-able content → must NOT set needs_dedup.

    Asserts the move ACTUALLY fired (moved==1): the hash-attach fallback also leaves
    needs_dedup 0, so without this the test would pass even if move detection broke."""
    q, database = queue_and_db
    lib = tmp_path / "lib"; lib.mkdir()
    _one_png(lib / "a.png")
    root = register(database, str(lib))
    _run_scan(q, database, root["id"])
    database.execute("UPDATE roots SET needs_dedup=0 WHERE id=?", (root["id"],))  # simulate a dedup
    (lib / "sub").mkdir()
    (lib / "a.png").rename(lib / "sub" / "a.png")
    jid = _run_scan(q, database, root["id"])
    assert _moved_count(database, jid) == 1, "the move must have fired, not the hash fallback"
    assert database.query_one("SELECT needs_dedup FROM roots WHERE id=?",
                              (root["id"],))["needs_dedup"] == 0


def test_scan_move_of_trashed_asset_falls_through_to_hash(queue_and_db, tmp_path):
    """A moved file whose asset is TRASHED must NOT relink silently (that would drop the
    matches_trashed signal, guard 6) — it falls through to the hash path, which
    hits the trashed asset and reports it as matches_trashed (moved stays 0)."""
    q, database = queue_and_db
    lib = tmp_path / "lib"; lib.mkdir()
    _one_png(lib / "a.png")
    root = register(database, str(lib))
    _run_scan(q, database, root["id"])
    # Trash a.png's asset in place (file still on disk — no cleanup yet), then move it.
    database.execute(
        "UPDATE assets SET status='trashed' WHERE id="
        "(SELECT asset_id FROM file_instances WHERE filename='a.png')")
    (lib / "sub").mkdir()
    (lib / "a.png").rename(lib / "sub" / "a.png")
    jid = _run_scan(q, database, root["id"])
    r = database.query_one("SELECT moved, matches_trashed FROM scan_results WHERE job_id=?", (jid,))
    assert r["moved"] == 0 and r["matches_trashed"] == 1
    # The instance still relocated correctly (via the hash path), asset stays trashed.
    inst = database.query_one("SELECT path FROM file_instances WHERE filename='a.png'")
    assert inst["path"].endswith(os.path.join("sub", "a.png"))


def test_scan_copy_is_not_a_move(queue_and_db, tmp_path):
    """A byte-copy (origin still present) is NOT a move — the origin is live, so the new
    file is hashed and attaches as a second instance; moved stays 0."""
    q, database = queue_and_db
    import shutil
    lib = tmp_path / "lib"; lib.mkdir()
    _one_png(lib / "a.png")
    root = register(database, str(lib))
    _run_scan(q, database, root["id"])
    (lib / "sub").mkdir()
    shutil.copy(lib / "a.png", lib / "sub" / "a.png")   # both present, same name
    jid = _run_scan(q, database, root["id"])
    c = _counts(database)
    assert c["assets"] == 1 and c["instances"] == 2      # one asset, two instances
    assert _moved_count(database, jid) == 0


def test_scan_two_move_candidates_are_hashed_not_relinked(queue_and_db, tmp_path):
    """A file relocated into TWO new spots (two path-absent candidates sharing the
    bucket) is ambiguous — only one could be the move, so BOTH are hashed (moved=0) and
    resolve correctly via content hash to one asset (condition v)."""
    q, database = queue_and_db
    import shutil
    lib = tmp_path / "lib"; lib.mkdir()
    _one_png(lib / "a.png")
    root = register(database, str(lib))
    _run_scan(q, database, root["id"])
    (lib / "x").mkdir(); (lib / "y").mkdir()
    shutil.copy(lib / "a.png", lib / "x" / "a.png")
    shutil.copy(lib / "a.png", lib / "y" / "a.png")
    (lib / "a.png").unlink()                             # origin gone; 2 new same-name candidates
    jid = _run_scan(q, database, root["id"])
    c = _counts(database)
    assert c["assets"] == 1 and c["instances"] == 2
    assert _moved_count(database, jid) == 0


def test_scan_full_disables_move_detection(queue_and_db, tmp_path):
    """--full forces re-hashing, so move detection is skipped (moved=0); the moved file
    still resolves via the hash path (new instance row, old one forgotten)."""
    q, database = queue_and_db
    lib = tmp_path / "lib"; lib.mkdir()
    _one_png(lib / "a.png")
    root = register(database, str(lib))
    _run_scan(q, database, root["id"])
    before = database.query_one("SELECT id FROM file_instances WHERE filename='a.png'")
    (lib / "sub").mkdir()
    (lib / "a.png").rename(lib / "sub" / "a.png")
    jid = _run_scan(q, database, root["id"], full=True)
    after = database.query_one("SELECT id FROM file_instances WHERE filename='a.png'")
    assert _moved_count(database, jid) == 0
    assert after["id"] != before["id"]                   # hash path made a NEW row, not a relink
    c = _counts(database)
    assert c["assets"] == 1 and c["instances"] == 1


# --- plan_moves guards (pure unit tests) -----------------------------------
def _rec(fid, path, size, mtime, *, undecodable=0, media_type="photo",
         has_phash=1, has_vphash=0, status="active"):
    return {"fid": fid, "path": path, "size": size, "mtime": mtime, "asset_id": fid * 10,
            "undecodable": undecodable, "media_type": media_type, "status": status,
            "has_phash": has_phash, "has_vphash": has_vphash}


def _existing(*recs):
    return {os.path.normcase(r["path"]): r for r in recs}


def _P(*parts):
    return os.path.join(os.sep + "lib", *parts)


def test_plan_moves_happy_path():
    from packrat.jobs.scan import Candidate, Enumeration, plan_moves
    existing = _existing(_rec(1, _P("a.png"), 100, 1000.0))          # origin (will be gone)
    cand = Candidate(path=_P("sub", "a.png"), rel="sub/a.png", size=100, mtime=1000.5)
    moves = plan_moves([cand], existing, Enumeration(), 2.0)
    assert moves.get(os.path.normcase(cand.path))["fid"] == 1


def test_plan_moves_live_origin_vetoes():
    """The sole bucket holder is still on disk this pass → a copy, not a move."""
    from packrat.jobs.scan import Candidate, Enumeration, plan_moves
    existing = _existing(_rec(1, _P("a.png"), 100, 1000.0))
    live = Candidate(path=_P("a.png"), rel="a.png", size=100, mtime=1000.0)      # origin present
    copy = Candidate(path=_P("sub", "a.png"), rel="sub/a.png", size=100, mtime=1000.0)
    assert plan_moves([live, copy], existing, Enumeration(), 2.0) == {}


def test_plan_moves_suppressed_origin_vetoes():
    """A gone-looking origin under a suppressed (errored/ignored) subtree may still exist
    on disk → never relink it."""
    from packrat.jobs.scan import Candidate, Enumeration, plan_moves
    existing = _existing(_rec(1, _P("subx", "a.png"), 100, 1000.0))
    en = Enumeration(suppressed={os.path.normcase(_P("subx"))})
    cand = Candidate(path=_P("a.png"), rel="a.png", size=100, mtime=1000.0)
    assert plan_moves([cand], existing, en, 2.0) == {}


def test_plan_moves_not_fully_fingerprinted_vetoes():
    """A not-yet-fingerprinted origin (e.g. merge-created, no phash) must fall through so
    the miss/backfill path decodes it."""
    from packrat.jobs.scan import Candidate, Enumeration, plan_moves
    existing = _existing(_rec(1, _P("a.png"), 100, 1000.0, has_phash=0))
    cand = Candidate(path=_P("sub", "a.png"), rel="sub/a.png", size=100, mtime=1000.0)
    assert plan_moves([cand], existing, Enumeration(), 2.0) == {}


def test_plan_moves_trashed_origin_vetoes():
    """A trashed origin must fall through to the hash path so the re-appearance is counted
    matches_trashed, not silently relinked (guard 6)."""
    from packrat.jobs.scan import Candidate, Enumeration, plan_moves
    existing = _existing(_rec(1, _P("a.png"), 100, 1000.0, status="trashed"))
    cand = Candidate(path=_P("sub", "a.png"), rel="sub/a.png", size=100, mtime=1000.0)
    assert plan_moves([cand], existing, Enumeration(), 2.0) == {}


def test_plan_moves_mtime_out_of_tolerance_vetoes():
    from packrat.jobs.scan import Candidate, Enumeration, plan_moves
    existing = _existing(_rec(1, _P("a.png"), 100, 1000.0))
    cand = Candidate(path=_P("sub", "a.png"), rel="sub/a.png", size=100, mtime=1005.0)
    assert plan_moves([cand], existing, Enumeration(), 2.0) == {}


def test_plan_moves_ambiguous_db_bucket_vetoes():
    """Two DB instances share the (filename,size) bucket → the pair doesn't identify
    content in this root → hash rather than guess the origin."""
    from packrat.jobs.scan import Candidate, Enumeration, plan_moves
    existing = _existing(
        _rec(1, _P("p", "a.png"), 100, 1000.0),
        _rec(2, _P("q", "a.png"), 100, 1000.0),
    )
    cand = Candidate(path=_P("r", "a.png"), rel="r/a.png", size=100, mtime=1000.0)
    assert plan_moves([cand], existing, Enumeration(), 2.0) == {}


def test_plan_moves_gone_origin_with_live_bucket_collision_vetoes():
    """The crux of the live+gone rule: a gone origin AND a SEPARATE still-present file
    share the (filename,size) bucket. The bucket is non-unique in the DB, so even though
    exactly one match is *gone*, the tuple no longer identifies content → hash the
    candidate (a live collision must veto regardless of its mtime)."""
    from packrat.jobs.scan import Candidate, Enumeration, plan_moves
    gone = _rec(1, _P("p", "a.png"), 100, 1000.0)         # this pass won't enumerate it
    live = _rec(2, _P("q", "a.png"), 100, 3000.0)         # different mtime, still on disk
    existing = _existing(gone, live)
    en = Enumeration()
    # The live file is enumerated this pass (at its own path); the new candidate is a
    # third same-bucket path. `live` present + `gone` absent = 2 DB matches → veto.
    live_cand = Candidate(path=_P("q", "a.png"), rel="q/a.png", size=100, mtime=3000.0)
    new_cand = Candidate(path=_P("r", "a.png"), rel="r/a.png", size=100, mtime=1000.0)
    assert plan_moves([live_cand, new_cand], existing, en, 2.0) == {}


def test_scan_undecodable_kept_with_hash(queue_and_db, tiny_photos):
    q, database = queue_and_db
    (tiny_photos / "broken.png").write_bytes(b"not a real png")
    root = register(database, str(tiny_photos))
    _run_scan(q, database, root["id"])
    bad = database.query_one(
        "SELECT undecodable, decode_error, content_hash FROM assets WHERE undecodable=1"
    )
    assert bad is not None
    assert bad["decode_error"]  # a reason was recorded
    assert bad["content_hash"]  # identity preserved despite decode failure
    # No phash row for the undecodable asset.
    n = database.query_one("SELECT COUNT(*) c FROM phash")["c"]
    assert n == 2  # a.png + b.png only


def test_scan_undecodable_not_retried_incremental_but_retried_full(queue_and_db, tiny_photos, monkeypatch):
    q, database = queue_and_db
    bad = tiny_photos / "broken.png"
    bad.write_bytes(b"not a real png")
    root = register(database, str(tiny_photos))
    _run_scan(q, database, root["id"])

    # Count decode attempts by spying on fill_perceptual.
    from packrat import media

    calls = {"n": 0}
    orig = media.fill_perceptual

    def spy(fp, path, config, **kwargs):
        calls["n"] += 1
        return orig(fp, path, config, **kwargs)

    monkeypatch.setattr(media, "fill_perceptual", spy)

    # Incremental re-scan: undecodable is "fully fingerprinted" → not re-decoded.
    _run_scan(q, database, root["id"])
    assert calls["n"] == 0

    # --full bypasses the fast-path and retries the undecodable file.
    _run_scan(q, database, root["id"], full=True)
    assert calls["n"] >= 1


def test_scan_trash_root_rejected(queue_and_db, tmp_path):
    q, database = queue_and_db
    trash = tmp_path / "Trash"
    trash.mkdir()
    root = register(database, str(trash), kind="trash")
    jid = q.submit("scan", {"root_id": root["id"]})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = database.query_one("SELECT status, error FROM jobs WHERE id=?", (jid,))
        if row and row["status"] != "running":
            break
        time.sleep(0.02)
    assert row["status"] == "error"
    assert "trash root" in (row["error"] or "")


def test_scan_all_skips_busy_root(queue_and_db, tiny_photos, tmp_path):
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    # A second empty root that is "busy" via a pending review run.
    other = tmp_path / "Other"
    other.mkdir()
    other_root = register(database, str(other))
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, created_at) "
        "VALUES (?, 'dedup', 'pending', '2026-01-01T00:00:00+00:00')",
        (other_root["id"],),
    )
    # --all must not fail; it scans the free root and skips the busy one.
    _run_scan(q, database, all=True)
    c = _counts(database)
    assert c["assets"] == 2  # only tiny_photos indexed


def test_enumeration_prunes_and_suppresses_ignored_subtree(tmp_path):
    from packrat.config import Config
    from packrat.ignore import IgnoreSet
    from packrat.jobs.scan import enumerate_root

    lib = tmp_path / "lib"
    (lib / "cache").mkdir(parents=True)
    import numpy as np
    from PIL import Image

    Image.fromarray(np.zeros((8, 8, 3), dtype=np.uint8)).save(lib / "a.png")
    Image.fromarray(np.zeros((8, 8, 3), dtype=np.uint8)).save(lib / "cache" / "b.png")
    ignore = IgnoreSet.build(Config(), ["cache/"])
    en = enumerate_root(str(lib), ignore)
    names = {os.path.basename(c.path) for c in en.candidates}
    assert names == {"a.png"}  # cache/ pruned
    # The pruned subtree is suppressed so a prior-indexed cache file isn't forgotten.
    assert en.is_suppressed(str(lib / "cache" / "b.png"))
    assert not en.is_suppressed(str(lib / "a.png"))


def test_enumeration_per_entry_error_suppresses_subtree(tmp_path, monkeypatch):
    # A per-entry stat()/is_dir() OSError (a NAS blip) must SUPPRESS the
    # containing directory so deletion-detection can't read the unreadable file as
    # "deleted" and forget its fingerprints. (Regression: a bare `continue` left the
    # file neither enumerated nor suppressed → silently forgotten.)
    from packrat.config import Config
    from packrat.ignore import IgnoreSet
    from packrat.jobs import scan as scan_mod
    from packrat.jobs.scan import enumerate_root

    lib = tmp_path / "lib"
    lib.mkdir()
    import numpy as np
    from PIL import Image

    Image.fromarray(np.zeros((8, 8, 3), dtype=np.uint8)).save(lib / "a.png")

    real_scandir = os.scandir

    class _BadStat:
        """A DirEntry-like wrapper whose stat() raises, else delegates."""

        def __init__(self, entry):
            self._e = entry
            self.name = entry.name

        def is_dir(self, *, follow_symlinks=True):
            return self._e.is_dir(follow_symlinks=follow_symlinks)

        def stat(self, *, follow_symlinks=True):
            raise OSError("simulated NAS stat timeout")

    class _CM:
        def __init__(self, path):
            self._it = list(real_scandir(path))

        def __enter__(self):
            return [_BadStat(e) if e.name == "a.png" else e for e in self._it]

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(scan_mod.os, "scandir", lambda p: _CM(p))
    ignore = IgnoreSet.build(Config(), [])
    en = enumerate_root(str(lib), ignore)
    assert not en.candidates                      # a.png couldn't be stat'd → not a candidate
    assert en.is_suppressed(str(lib / "a.png"))   # but its subtree is suppressed (not forgotten)


def test_scan_dry_run_writes_nothing(queue_and_db, tiny_photos):
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run_scan(q, database, root["id"], dry_run=True)
    c = _counts(database)
    assert c["assets"] == 0 and c["instances"] == 0 and c["phash"] == 0


def test_scan_attaches_to_trashed_asset_without_unflip(queue_and_db, tiny_photos):
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run_scan(q, database, root["id"])
    # Flip a.png's asset to trashed, drop its instances, then re-scan: the file
    # re-appears and attaches, but the asset stays trashed (Phase 4).
    a = database.query_one(
        "SELECT a.id FROM assets a JOIN file_instances fi ON fi.asset_id=a.id "
        "WHERE fi.filename='a.png' LIMIT 1"
    )
    database.execute("UPDATE assets SET status='trashed' WHERE id=?", (a["id"],))
    _run_scan(q, database, root["id"], full=True)
    row = database.query_one("SELECT status FROM assets WHERE id=?", (a["id"],))
    assert row["status"] == "trashed"


def test_scan_backfill_of_trashed_asset_reports_matches_trashed(queue_and_db, tiny_photos):
    """A hit on a TRASHED, not-yet-fingerprinted asset (the merge-created-then-trashed
    case) reports matches_trashed even though scan also backfills its perceptual data —
    the banner's trash signal must be honest: the backfill branch reports it as
    matches_trashed, not 'backfilled'."""
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    _run_scan(q, database, root["id"])
    a = database.query_one(
        "SELECT a.id FROM assets a JOIN file_instances fi ON fi.asset_id=a.id "
        "WHERE fi.filename='b.png' LIMIT 1"
    )
    # Make it look merge-created-then-trashed: trashed, its perceptual rows gone (so it's
    # NOT fully fingerprinted), and its instances dropped so the on-disk file re-appears.
    database.execute("UPDATE assets SET status='trashed' WHERE id=?", (a["id"],))
    database.execute("DELETE FROM phash WHERE asset_id=?", (a["id"],))
    database.execute("DELETE FROM file_instances WHERE asset_id=?", (a["id"],))
    # Incremental re-scan → the file hits the trashed asset, takes the backfill branch
    # (fills phash), and is reported as matches_trashed (not 'backfilled').
    res = _rescan_capture(q, database, root["id"])
    assert res["matches_trashed"] >= 1, res
    # The asset stayed trashed and its phash was re-filled.
    assert database.query_one("SELECT status FROM assets WHERE id=?", (a["id"],))["status"] == "trashed"
    assert database.query_one("SELECT COUNT(*) c FROM phash WHERE asset_id=?", (a["id"],))["c"] == 1


def _rescan_capture(q, database, root_id) -> dict:
    """Run a scan and return its result_json counts."""
    import json as _json
    jid = q.submit("scan", {"root_id": root_id})
    import time as _time
    deadline = _time.monotonic() + 30.0
    while _time.monotonic() < deadline:
        row = database.query_one("SELECT status, result_json FROM jobs WHERE id=?", (jid,))
        if row and row["status"] not in ("queued", "running"):
            assert row["status"] == "done", row["status"]
            return _json.loads(row["result_json"] or "{}")
        _time.sleep(0.02)
    raise AssertionError("scan did not finish")


def test_scan_profile_emits_report(queue_and_db, tiny_photos):
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    logs = _run_scan_capture_logs(q, database, root["id"], profile=True)
    blob = "\n".join(logs)
    # The sectioned profile block is emitted: header, a PHOTOS section (tiny_photos
    # is all PNGs → photo pipeline), per-medium rollup, and the parallelism line.
    assert "scan profile" in blob
    assert "PHOTOS" in blob
    assert "rollup:" in blob
    assert "parallelism" in blob
    # Photo pipeline buckets: I/O (producer reads) + pdq/decode/hash (CPU).
    assert "I/O" in blob and "pdq" in blob


def test_scan_without_profile_emits_no_report(queue_and_db, tiny_photos):
    q, database = queue_and_db
    root = register(database, str(tiny_photos))
    logs = _run_scan_capture_logs(q, database, root["id"])  # no profile=True
    assert not any("scan profile" in line for line in logs)


# ---------------------------------------------------------------------------
# producer/consumer photo pipeline (decouple I/O from CPU)
# ---------------------------------------------------------------------------
def _make_photos(dirpath, n, seed0=0):
    import numpy as np
    from PIL import Image

    dirpath.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        arr = np.random.default_rng(seed0 + i).integers(0, 256, (48, 48, 3), dtype=np.uint8)
        Image.fromarray(arr).save(dirpath / f"p{i:03d}.png")


def test_pipeline_indexes_all_photos(queue_and_db, tmp_path):
    """Many distinct photos flow through producers→queue→consumers and all persist."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    _make_photos(lib, 40)
    root = register(database, str(lib))
    _run_scan(q, database, root["id"])
    c = _counts(database)
    assert c["assets"] == 40 and c["instances"] == 40 and c["phash"] == 40


def test_pipeline_result_matches_streamed(queue_and_db, tmp_path, monkeypatch):
    """Pipeline (buffered) vs forced path-decode produce identical hashes + phash."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    _make_photos(lib, 12, seed0=100)
    root = register(database, str(lib))
    _run_scan(q, database, root["id"])
    pipe = {r["filename"]: (r["content_hash"], r["bits"]) for r in database.query(
        "SELECT fi.filename, a.content_hash, p.bits FROM file_instances fi "
        "JOIN assets a ON a.id=fi.asset_id JOIN phash p ON p.asset_id=a.id")}

    # Force the streamed/path branch by capping the photo buffer to 0 bytes, wipe, re-scan.
    from dataclasses import replace
    from packrat.config import Config

    base = Config()
    forced = replace(base, smb=replace(base.smb, photo_buffer_max_bytes=0))
    monkeypatch.setattr(q, "_config_loader", lambda: forced)
    database.clear_catalog()
    root = register(database, str(lib))
    _run_scan(q, database, root["id"])
    streamed = {r["filename"]: (r["content_hash"], r["bits"]) for r in database.query(
        "SELECT fi.filename, a.content_hash, p.bits FROM file_instances fi "
        "JOIN assets a ON a.id=fi.asset_id JOIN phash p ON p.asset_id=a.id")}

    assert pipe == streamed and len(pipe) == 12


def test_pipeline_profiler_splits_photo_io_and_cpu(queue_and_db, tmp_path):
    q, database = queue_and_db
    lib = tmp_path / "lib"
    _make_photos(lib, 20)
    root = register(database, str(lib))
    logs = _run_scan_capture_logs(q, database, root["id"], profile=True)
    blob = "\n".join(logs)
    # Photo pipeline: I/O is pure producer byte transfer; hash/decode/pdq pure CPU.
    assert "PHOTOS  20 file(s)" in blob
    assert "I/O" in blob and "[io]" in blob
    # decode-from-RAM is tagged CPU (not mixed) in the photo section.
    assert "[cpu]" in blob


def test_oversized_photo_falls_back_to_path(queue_and_db, tmp_path, monkeypatch):
    """A photo above photo_buffer_max_bytes still indexes (via the streamed path)."""
    q, database = queue_and_db
    lib = tmp_path / "lib"
    _make_photos(lib, 3)
    root = register(database, str(lib))
    from dataclasses import replace
    from packrat.config import Config

    base = Config()
    tiny_cap = replace(base, smb=replace(base.smb, photo_buffer_max_bytes=1))  # everything oversized
    monkeypatch.setattr(q, "_config_loader", lambda: tiny_cap)
    _run_scan(q, database, root["id"])
    c = _counts(database)
    assert c["assets"] == 3 and c["phash"] == 3
