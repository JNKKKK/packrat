"""scan job (§8 A2) + M2 perceptual: new/dup, fast-path, deletion, undecodable, PDQ.

Drives the real scan handler through a ``JobQueue`` + ``Database`` (as test_jobs
does), against tiny real PNGs so the decode→hash→PDQ path actually runs. Requires
the ``media`` extra (blake3/pillow/pdqhash) — all confirmed by the M0 smoke test.
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
    # ... but does NOT stamp last_full_scan_at (only `scan --full` does, §8 A2 step 11).
    assert snap["last_full_scan_at"] is None
    det = queries.root_detail(root["name"])
    assert det["last_scan_at"] == snap["last_scan_at"]

    # A --full scan additionally stamps last_full_scan_at.
    _run_scan(q, database, root["id"], full=True)
    snap2 = queries.roots_snapshot()[0]
    assert snap2["last_full_scan_at"] is not None


def test_roots_snapshot_media_split_and_dedup_recency(queue_and_db, tiny_photos):
    """roots_snapshot exposes photos/videos + last_dedup_at for the M6 dot & sort (Open Q#1)."""
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
    # M2: a PDQ row per photo asset.
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
    # A per-entry stat()/is_dir() OSError (a NAS blip, §10.1) must SUPPRESS the
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
    # re-appears and attaches, but the asset stays trashed (§8 A2 Phase 4).
    a = database.query_one(
        "SELECT a.id FROM assets a JOIN file_instances fi ON fi.asset_id=a.id "
        "WHERE fi.filename='a.png' LIMIT 1"
    )
    database.execute("UPDATE assets SET status='trashed' WHERE id=?", (a["id"],))
    _run_scan(q, database, root["id"], full=True)
    row = database.query_one("SELECT status FROM assets WHERE id=?", (a["id"],))
    assert row["status"] == "trashed"


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
# producer/consumer photo pipeline (§ decouple I/O from CPU)
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
