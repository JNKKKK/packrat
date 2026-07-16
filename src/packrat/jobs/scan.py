r"""The ``scan`` job (§8 A2) — walk a registered root and fingerprint it.

Scan is purely **per-asset**: for each file it writes the content hash (identity),
metadata, and the M2 perceptual signature (photo PDQ / video per-frame PDQ). It
resolves **exact** byte-identity (a second file of the same bytes becomes another
``file_instance`` of one asset) but computes **no** near-dup relationships — that
is ``dedup`` (M3). No embeddings unless ``--embed`` (the pass itself is M7).

Shape of a pass (mapped to §8 A2):
- **Phase 1 — enumerate.** Resolve the root (reject trash roots; honor per-root
  exclusivity), walk it with the root's ignore set, and record which directories
  were *cleanly* enumerated (the deletion-detection guard, §10.1).
- **Phase 2 — per file.** Fast-path skip (path + exact size + tolerant mtime, and
  the asset already "fully fingerprinted"); else hash, resolve against
  ``assets.content_hash`` (attach instance on a hit, create on a miss, backfill a
  not-yet-fingerprinted / undecodable-retry hit), decode + PDQ, and persist the
  asset + instance + phash/vphash in **one transaction**.
- **Phase 3 — deletion detection.** Any pre-existing instance this pass never
  touched, *and* whose parent directory was cleanly enumerated, has its row
  deleted; an ``active`` asset left with zero instances is forgotten (cascade).

**Concurrency — two engines, split by medium (§10.1, § profiler findings):**

- **Photos → producer/consumer pipeline** (:func:`_run_photo_pipeline`).
  ``smb.io_workers`` producer threads read each whole photo file into memory
  (pure I/O — disk or network); ``smb.cpu_workers`` consumers hash + decode + PDQ *from the
  buffer* (pure CPU — decode via ``BytesIO`` has no fd, so no hidden lazy reads).
  Decoupling I/O from CPU concurrency lets a scan saturate the link *and* the
  cores independently, and reads each photo **once** (not hash-then-re-read).
  Backpressure is by **bytes** (``smb.photo_buffer_budget_bytes``) so a burst of
  large HEIC/RAW can't balloon RAM.
- **Videos (and oversized photos) → per-file streamed pool**
  (:func:`_run_streamed`, ``smb.scan_workers``). Hashing streams the file in
  chunks (~1 MiB RAM regardless of size); video decode seeks to a few frames and
  reads only a fraction — so buffering (which would cost a whole clip × workers)
  is neither needed nor done.

Both release the GIL in C (BLAKE3 fully, Pillow/PyAV largely; PDQ mostly not).
DB writes go through the daemon's lock-guarded
:class:`~packrat.db.connection.Database`, so persists serialize safely; asset
creation is idempotent on ``content_hash`` so two workers racing on identical new
bytes converge on one asset. ``--profile`` reports per-medium I/O-vs-CPU splits
+ a photo pipeline bottleneck verdict (:mod:`packrat.profiling`).
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .. import fsutil, media, roots
from ..ignore import IgnoreSet, is_junk_dirent
from ..profiling import NULL_PROFILER, ScanProfiler
from ..util import now_iso
from .context import CancelledError, JobContext
from .registry import JobSpec, register_job

log = logging.getLogger("packrat.jobs.scan")


# ---------------------------------------------------------------------------
# problem-file collector — the retrievable list behind the undecodable/errors
# counters (persisted to scan_problem_files so `status <root>` can show paths+
# reasons, not just counts). Thread-safe; appended from the worker threads.
# ---------------------------------------------------------------------------
@dataclass
class ProblemFile:
    root_id: int
    path: str
    media_type: str | None
    problem: str            # 'undecodable' | 'read-error'
    content_hash: str | None
    detail: str | None


class ScanReport:
    """Accumulates problem files across the scan's worker threads."""

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._problems: list[ProblemFile] = []

    def add_problem(self, pf: ProblemFile) -> None:
        with self._lock:
            self._problems.append(pf)

    def problems(self) -> list[ProblemFile]:
        with self._lock:
            return list(self._problems)


# ---------------------------------------------------------------------------
# enumeration (Phase 1)
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    path: str          # canonical absolute path (stored form)
    rel: str           # root-relative posix path (for ignore-glob matching)
    size: int
    mtime: float


@dataclass
class Enumeration:
    candidates: list[Candidate] = field(default_factory=list)
    #: normcase'd canonical dirs whose *subtree* must NOT be reconciled this pass —
    #: either a listing errored (NAS blip, §10.1) or the dir was ignore-pruned. A
    #: genuinely-deleted folder is NOT here (its parent listed cleanly and simply
    #: didn't contain it), so its instances still get forgotten. See is_suppressed.
    suppressed: set[str] = field(default_factory=set)
    root_offline: bool = False

    def is_suppressed(self, path: str) -> bool:
        """True if ``path`` lies under any suppressed subtree (deletion guard)."""
        p = os.path.normcase(path)
        for s in self.suppressed:
            if p == s or p.startswith(s + os.sep):
                return True
        return False


def enumerate_root(root_path: str, ignore: IgnoreSet) -> Enumeration:
    r"""Walk ``root_path`` applying the ignore set (§8 A2 step 2, §10.1).

    Uses ``os.scandir`` (one batched directory round-trip, ``DirEntry`` caches
    ``stat`` on Windows). Directories we could **not** authoritatively read — a
    listing that errored/timed out, or a subtree we deliberately pruned — go into
    ``suppressed``; deletion-detection then skips any instance under a suppressed
    subtree, so a NAS blip or an ignore rule never reads as "files deleted". A
    folder that was actually *deleted* is not suppressed (its parent enumerated
    cleanly), so its now-gone instances are correctly reconciled (§8 A2 step 11).
    """
    en = Enumeration()
    canon_root = fsutil.canonicalize(root_path)  # plain, prefix-free — the stored form
    # (canon_dir, rel_dir) work stack; rel_dir is "" at the root. We scandir the
    # *extended* form of canon_dir but store/compare the plain canonical path so
    # equality with the fast-path/deletion queries is well-defined (§4, §8 A1).
    stack: list[tuple[str, str]] = [(canon_root, "")]
    # If the very first listing fails the whole root is offline (§4 whole-root guard).
    first = True
    while stack:
        canon_dir, rel_dir = stack.pop()
        try:
            with os.scandir(fsutil.extended(canon_dir)) as it:
                entries = list(it)
        except OSError as exc:
            log.warning("enumeration error under %s: %s", canon_dir, exc)
            en.suppressed.add(os.path.normcase(canon_dir))  # subtree unreconcilable
            if first:
                en.root_offline = True
            first = False
            continue
        first = False
        for entry in entries:
            child_rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                child_abs = os.path.join(canon_dir, entry.name)
                if ignore.is_dir_pruned(child_rel):
                    # Deliberately not descended → its (untracked) contents must not
                    # be reconciled as deletions if a prior scan indexed them.
                    en.suppressed.add(os.path.normcase(child_abs))
                    continue
                stack.append((child_abs, child_rel))
                continue
            # A file: allowlist first (cheapest), then ignore globs, then attrs.
            if not ignore.is_media(entry.name):
                continue
            if ignore.is_file_ignored(child_rel):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            attrs = getattr(st, "st_file_attributes", 0)
            if is_junk_dirent(st.st_size, attrs) is not None:
                continue
            canon_path = os.path.join(canon_dir, entry.name)
            en.candidates.append(
                Candidate(path=canon_path, rel=child_rel, size=st.st_size, mtime=st.st_mtime)
            )
    return en


# ---------------------------------------------------------------------------
# fast-path predicate (§8 A2 step 4)
# ---------------------------------------------------------------------------
def _asset_fully_fingerprinted(undecodable: int, media_type: str, has_phash: int, has_vphash: int) -> bool:
    """The "fully fingerprinted" predicate (§8 A2 step 4, authoritative).

    ``undecodable`` assets are complete by design (hash-only, no perceptual — only
    ``--full`` retries them). Otherwise the media type's perceptual rows must exist.
    Embeddings are deliberately excluded.
    """
    if undecodable:
        return True
    if media_type == "video":
        return bool(has_vphash)
    return bool(has_phash)


# ---------------------------------------------------------------------------
# persistence helpers (all idempotent — safe under the parallel workers)
# ---------------------------------------------------------------------------
def _insert_perceptual(conn, asset_id: int, fp: media.Fingerprint) -> None:
    if fp.undecodable:
        return
    if fp.media_type == "video":
        for fr in fp.frames:
            conn.execute(
                "INSERT INTO vphash(asset_id, frame_index, t_offset_s, pdq_bits, quality) "
                "VALUES (?,?,?,?,?) ON CONFLICT(asset_id, frame_index) DO UPDATE SET "
                "t_offset_s=excluded.t_offset_s, pdq_bits=excluded.pdq_bits, quality=excluded.quality",
                (asset_id, fr.frame_index, fr.t_offset_s, fr.pdq_bits, fr.quality),
            )
    elif fp.phash_bits is not None:
        conn.execute(
            "INSERT INTO phash(asset_id, algo, bits, quality) VALUES (?, 'pdq', ?, ?) "
            "ON CONFLICT(asset_id, algo) DO UPDATE SET bits=excluded.bits, quality=excluded.quality",
            (asset_id, fp.phash_bits, fp.phash_quality),
        )


def _upsert_instance(conn, asset_id: int, root_id: int, cand: Candidate, seen_at: str) -> None:
    """Insert/repoint the instance at ``(root_id, path)`` to ``asset_id`` (§8 A2 step 6).

    If this **repoints** an existing row from another asset (an in-place content
    edit: same path, new bytes → new/other asset), the prior asset may be left
    with zero instances — forget it here if it is ``active`` (§4: a plain edit is
    not trash), in the same transaction so the reconcile is atomic. Deletion
    detection (Phase 3) can't catch this case: it keys off the *path*, which is
    still present, just bound to a different asset now.
    """
    prev = conn.execute(
        "SELECT asset_id FROM file_instances WHERE root_id=? AND path=?", (root_id, cand.path)
    ).fetchone()
    conn.execute(
        "INSERT INTO file_instances(asset_id, root_id, path, filename, size, mtime, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(root_id, path) DO UPDATE SET "
        "asset_id=excluded.asset_id, size=excluded.size, mtime=excluded.mtime, "
        "last_seen_at=excluded.last_seen_at",
        (asset_id, root_id, cand.path, os.path.basename(cand.path), cand.size, cand.mtime, seen_at),
    )
    if prev is not None and prev["asset_id"] != asset_id:
        _forget_if_orphaned(conn, int(prev["asset_id"]))


def _forget_if_orphaned(conn, asset_id: int) -> None:
    """Delete an ``active`` asset that now has zero file instances (§4 forget rule).

    A ``trashed`` asset is kept at zero instances (its fingerprint is trash memory).
    """
    n = conn.execute("SELECT COUNT(*) c FROM file_instances WHERE asset_id=?", (asset_id,)).fetchone()["c"]
    if n:
        return
    st = conn.execute("SELECT status FROM assets WHERE id=?", (asset_id,)).fetchone()
    if st is not None and st["status"] == "active":
        conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))  # cascade fingerprints


def _persist_new(db, root_id: int, cand: Candidate, fp: media.Fingerprint, seen_at: str) -> bool:
    """Create the asset (idempotent on ``content_hash``) + instance + perceptual rows.

    Returns True if this call created the asset (a genuine new asset), False if it
    lost a race to a concurrent worker with identical bytes (then it only attaches
    the instance — the winner wrote the perceptual rows). §8 A2 step 9.
    """
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO assets(content_hash, media_type, size, width, height, duration_s, "
            "captured_at, status, undecodable, decode_error, codec, added_at) "
            "VALUES (?,?,?,?,?,?,?, 'active', ?, ?, ?, ?) ON CONFLICT(content_hash) DO NOTHING",
            (fp.content_hash, fp.media_type, fp.size, fp.width, fp.height, fp.duration_s,
             fp.captured_at, 1 if fp.undecodable else 0, fp.decode_error,
             fp.codec, now_iso()),
        )
        created = cur.rowcount == 1
        row = conn.execute("SELECT id FROM assets WHERE content_hash=?", (fp.content_hash,)).fetchone()
        asset_id = int(row["id"])
        if created:
            _insert_perceptual(conn, asset_id, fp)
        _upsert_instance(conn, asset_id, root_id, cand, seen_at)
    return created


def _persist_backfill(db, asset_id: int, root_id: int, cand: Candidate, fp: media.Fingerprint, seen_at: str) -> None:
    """Update an existing asset in place with freshly-computed perceptual data (§8 A2 step 6 backfill)."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE assets SET size=?, width=?, height=?, duration_s=?, captured_at=?, "
            "undecodable=?, decode_error=?, codec=? WHERE id=?",
            (fp.size, fp.width, fp.height, fp.duration_s, fp.captured_at,
             1 if fp.undecodable else 0, fp.decode_error, fp.codec, asset_id),
        )
        conn.execute("DELETE FROM phash WHERE asset_id=?", (asset_id,))
        conn.execute("DELETE FROM vphash WHERE asset_id=?", (asset_id,))
        _insert_perceptual(conn, asset_id, fp)
        _upsert_instance(conn, asset_id, root_id, cand, seen_at)


def _attach_instance(db, asset_id: int, root_id: int, cand: Candidate, seen_at: str) -> None:
    with db.transaction() as conn:
        _upsert_instance(conn, asset_id, root_id, cand, seen_at)


# ---------------------------------------------------------------------------
# per-file resolution + persist (shared by the video path and the photo pipeline)
# ---------------------------------------------------------------------------
def _resolve_and_persist(ctx, root_id, cand, content_hash, decode, full, seen_at,
                         medium, profiler, report) -> str:
    """Given a file's ``content_hash``, resolve it against the catalog and persist.

    ``decode`` is a **lazy** ``() -> Fingerprint`` callable that decodes + PDQs the
    file; it is only invoked on a miss or a backfill (never on a plain dup hit), so
    an exact-dup instance never pays decode cost. Returns a report-counter outcome.
    A decode that yields ``undecodable`` is recorded to ``report`` (path + reason).
    """
    db = ctx.db

    def _note_undecodable(fp) -> None:
        report.add_problem(ProblemFile(
            root_id=root_id, path=cand.path, media_type=fp.media_type,
            problem="undecodable", content_hash=content_hash, detail=fp.decode_error,
        ))

    asset = db.query_one(
        "SELECT id, media_type, status, undecodable, "
        "EXISTS(SELECT 1 FROM phash p WHERE p.asset_id=assets.id) has_phash, "
        "EXISTS(SELECT 1 FROM vphash v WHERE v.asset_id=assets.id) has_vphash "
        "FROM assets WHERE content_hash=?",
        (content_hash,),
    )

    if asset is None:
        fp = decode()
        with profiler.timer("shared", "db"):
            created = _persist_new(db, root_id, cand, fp, seen_at)
        profiler.file_done(medium)
        if fp.undecodable:
            _note_undecodable(fp)
        if not created:
            return "exact_dup"
        return "undecodable" if fp.undecodable else "new"

    asset_id = int(asset["id"])
    # Backfill (§8 A2 step 6): (a) not-yet-fingerprinted, or (b) undecodable + --full.
    complete = _asset_fully_fingerprinted(
        asset["undecodable"], asset["media_type"], asset["has_phash"], asset["has_vphash"]
    )
    if ((not asset["undecodable"]) and not complete) or (bool(asset["undecodable"]) and full):
        fp = decode()
        with profiler.timer("shared", "db"):
            _persist_backfill(db, asset_id, root_id, cand, fp, seen_at)
        profiler.file_done(medium)
        if fp.undecodable:
            _note_undecodable(fp)
        return "undecodable" if fp.undecodable else "backfilled"

    # Plain exact-dup hit — attach the instance and stop (§8 A2 step 6 / Phase 4).
    with profiler.timer("shared", "db"):
        _attach_instance(db, asset_id, root_id, cand, seen_at)
    profiler.file_done(medium)
    return "matches_trashed" if asset["status"] == "trashed" else "exact_dup"


def _note_read_error(report, root_id, cand, medium, exc) -> None:
    """Record an unreadable-bytes file (no hash, no asset) — the 'read-error' problem."""
    log.warning("unreadable %s: %s", cand.path, exc)
    report.add_problem(ProblemFile(
        root_id=root_id, path=cand.path, media_type=medium,
        problem="read-error", content_hash=None, detail=f"{type(exc).__name__}: {exc}"[:500],
    ))


def _process_video(ctx, root_id, cand, full, seen_at, profiler, report) -> str:
    """Video path: hash-from-path (streamed), decode via seek-sampling from path."""
    try:
        content_hash = media.hash_file(cand.path, medium="video", profiler=profiler)
    except OSError as exc:
        _note_read_error(report, root_id, cand, "video", exc)
        return "errors"

    def decode():
        fp = media.Fingerprint(media_type="video", content_hash=content_hash, size=cand.size)
        return media.fill_perceptual(fp, cand.path, ctx.config, profiler=profiler)

    return _resolve_and_persist(ctx, root_id, cand, content_hash, decode, full, seen_at,
                                "video", profiler, report)


def _process_photo_bytes(ctx, root_id, cand, data, full, seen_at, profiler, report) -> str:
    """Photo pipeline consumer: hash + decode + PDQ from the producer's buffer (pure CPU)."""
    content_hash = media.hash_bytes(data, medium="photo", profiler=profiler)

    def decode():
        # Decode from the same in-memory buffer; reuses the hash already computed.
        fp = media.Fingerprint(media_type="photo", content_hash=content_hash, size=len(data))
        return media.fill_perceptual(fp, cand.path, ctx.config, data=data, profiler=profiler)

    return _resolve_and_persist(ctx, root_id, cand, content_hash, decode, full, seen_at,
                                "photo", profiler, report)


def _process_photo_path(ctx, root_id, cand, full, seen_at, profiler, report) -> str:
    """Photo fallback (oversized file): hash + decode from path (streamed, re-reads)."""
    try:
        content_hash = media.hash_file(cand.path, medium="photo", profiler=profiler)
    except OSError as exc:
        _note_read_error(report, root_id, cand, "photo", exc)
        return "errors"

    def decode():
        fp = media.Fingerprint(media_type="photo", content_hash=content_hash, size=cand.size)
        return media.fill_perceptual(fp, cand.path, ctx.config, profiler=profiler)

    return _resolve_and_persist(ctx, root_id, cand, content_hash, decode, full, seen_at,
                                "photo", profiler, report)


# ---------------------------------------------------------------------------
# per-root scan routine
# ---------------------------------------------------------------------------
def _scan_one_root(ctx: JobContext, root_row: dict, en: "Enumeration", *, full: bool,
                   dry_run: bool, seen_at: str, done: int, profiler=NULL_PROFILER,
                   collector=None) -> tuple[dict, int]:
    """Scan a single already-validated library root against a prior enumeration.

    ``done`` is the absolute progress counter carried across roots; returns the
    per-root report plus the advanced counter. Fast-path skips advance progress
    too, so the bar reaches ``total`` (= all candidates) even on a no-op re-scan.
    """
    db = ctx.db
    root_id = int(root_row["id"])
    report = {
        "root_id": root_id, "name": root_row["name"], "path": root_row["path"],
        "candidates": len(en.candidates), "new": 0, "exact_dup": 0, "matches_trashed": 0,
        "backfilled": 0, "undecodable": 0, "errors": 0, "skipped_fastpath": 0,
        "deleted_instances": 0, "forgotten_assets": 0, "root_offline": en.root_offline,
    }

    # Preload existing instances for the root → in-memory fast-path (no per-file DB read).
    existing: dict[str, dict] = {}
    with profiler.timer("shared", "db"):
        rows = db.query(
            "SELECT fi.id fid, fi.path, fi.size, fi.mtime, fi.asset_id, "
            "a.undecodable, a.media_type, "
            "EXISTS(SELECT 1 FROM phash p WHERE p.asset_id=a.id) has_phash, "
            "EXISTS(SELECT 1 FROM vphash v WHERE v.asset_id=a.id) has_vphash "
            "FROM file_instances fi JOIN assets a ON a.id=fi.asset_id WHERE fi.root_id=?",
            (root_id,),
        )
    for r in rows:
        existing[os.path.normcase(r["path"])] = dict(r)

    seen_fids: set[int] = set()
    to_process: list[Candidate] = []
    fastpath_fids: list[int] = []
    tol = ctx.config.fastpath.mtime_tolerance_s

    for cand in en.candidates:
        rec = existing.get(os.path.normcase(cand.path))
        if (
            not full
            and rec is not None
            and rec["size"] == cand.size
            and rec["mtime"] is not None
            and abs(rec["mtime"] - cand.mtime) <= tol
            and _asset_fully_fingerprinted(
                rec["undecodable"], rec["media_type"], rec["has_phash"], rec["has_vphash"]
            )
        ):
            seen_fids.add(rec["fid"])
            fastpath_fids.append(rec["fid"])
            report["skipped_fastpath"] += 1
        else:
            to_process.append(cand)
            if rec is not None:
                seen_fids.add(rec["fid"])  # a pre-existing row we're re-touching this pass

    if dry_run:
        report["would_index"] = len(to_process)
        return report, done + len(en.candidates)

    # Fast-path bumps advance progress without any byte work (§8 A2 step 4).
    with profiler.timer("shared", "db"):
        for i in range(0, len(fastpath_fids), 900):
            chunk = fastpath_fids[i : i + 900]
            db.execute(
                f"UPDATE file_instances SET last_seen_at=? WHERE id IN ({','.join('?' * len(chunk))})",
                (seen_at, *chunk),
            )
    done += report["skipped_fastpath"]
    if report["skipped_fastpath"]:
        ctx.progress(done, message=f"{report['skipped_fastpath']} unchanged")

    # Split the worklist: photos small enough to buffer go through the
    # producer/consumer pipeline (I/O ‖ CPU decoupled, read-once); videos and
    # oversized photos take the per-file streamed path.
    cap = ctx.config.smb.photo_buffer_max_bytes
    photos = [c for c in to_process
              if media.media_type_of(c.path) == "photo" and c.size <= cap]
    others = [c for c in to_process
              if media.media_type_of(c.path) != "photo" or c.size > cap]

    # `_record` owns the `done` counter (via nonlocal) + progress + cancel; the
    # pipeline/pool helpers call it per completed file and never touch `done`.
    def _record(outcome: str, cand: Candidate) -> None:
        nonlocal done
        report[outcome] = report.get(outcome, 0) + 1
        done += 1
        ctx.progress(done, message=os.path.basename(cand.path))
        if ctx.cancelled:
            raise CancelledError()

    _run_photo_pipeline(ctx, root_id, photos, full, seen_at, profiler, _record, collector)
    _run_streamed(ctx, root_id, others, full, seen_at, profiler, _record, collector)

    # Phase 3 — deletion detection (guarded per-subtree, §8 A2 step 11, §10.1).
    if not en.root_offline:
        _detect_deletions(ctx, root_id, existing, seen_fids, en, report)

    return report, done


def _run_streamed(ctx, root_id, cands, full, seen_at, profiler, record, collector) -> None:
    """Videos + oversized photos: one bounded pool, per-file hash+decode from path."""
    if not cands:
        return
    workers = max(1, ctx.config.smb.scan_workers)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="packrat-vid") as pool:
        futs = {}
        for c in cands:
            fn = _process_video if media.media_type_of(c.path) == "video" else _process_photo_path
            futs[pool.submit(fn, ctx, root_id, c, full, seen_at, profiler, collector)] = c
        try:
            for fut in as_completed(futs):
                record(fut.result(), futs[fut])
        except CancelledError:
            pool.shutdown(wait=False, cancel_futures=True)
            raise


def _run_photo_pipeline(ctx, root_id, cands, full, seen_at, profiler, record, collector) -> None:
    """Producer/consumer photo pipeline (§ decouple I/O from CPU concurrency).

    ``io_workers`` producer threads read whole photo files into a **bounded** queue
    (memory backpressure); ``cpu_workers`` consumers hash + decode + PDQ from the
    buffer (pure CPU). Results are drained on the calling thread via ``record`` so
    progress/cancel and the (single-writer) DB stay in one place. The profiler
    records producer-block vs consumer-idle time → the bottleneck verdict.
    """
    if not cands:
        return
    import queue as _queue
    import threading

    smb = ctx.config.smb
    n_io = smb.resolved_io_workers()
    n_cpu = smb.resolved_cpu_workers()
    budget = max(1, smb.photo_buffer_budget_bytes)

    read_q: "_queue.Queue" = _queue.Queue()    # (cand, data) buffers (byte-gated below)
    result_q: "_queue.Queue" = _queue.Queue()  # (outcome, cand) results
    cand_iter = iter(cands)
    iter_lock = threading.Lock()
    cancel = threading.Event()
    live_producers = [n_io]                     # countdown; guarded by iter_lock
    _POLL = 0.1                                 # queue get() timeout for clean shutdown

    # Backpressure by BYTES, not item count: producers block until the in-flight
    # buffer total fits the budget, so a burst of large photos can't balloon RAM.
    budget_cv = threading.Condition()
    inflight = [0]  # bytes currently read-but-not-yet-consumed; guarded by budget_cv

    def _acquire(nbytes: int) -> bool:
        """Reserve budget for a buffer; block until it fits (or we're the only one)."""
        with budget_cv:
            while not cancel.is_set():
                # Always admit at least one buffer even if it alone exceeds budget,
                # else a >budget photo (under the max_bytes cap) would deadlock.
                if inflight[0] == 0 or inflight[0] + nbytes <= budget:
                    inflight[0] += nbytes
                    return True
                budget_cv.wait(timeout=_POLL)
            return False

    def _release(nbytes: int) -> None:
        with budget_cv:
            inflight[0] -= nbytes
            budget_cv.notify_all()

    def producer():
        try:
            while not cancel.is_set():
                with iter_lock:
                    cand = next(cand_iter, None)
                if cand is None:
                    return
                # Every candidate MUST yield exactly one result (the drain loop
                # counts on it) — so any read failure emits an error result rather
                # than dropping the file, which would hang the drain.
                try:
                    t = time.perf_counter()
                    with open(fsutil.extended(cand.path), "rb") as f:
                        data = f.read()
                    profiler.add("photo", "io", time.perf_counter() - t)
                    profiler.add_bytes("photo", len(data))
                except Exception as exc:  # noqa: BLE001 - I/O or anything else
                    _note_read_error(collector, root_id, cand, "photo", exc)
                    result_q.put(("errors", cand))
                    continue
                # Byte-budget backpressure: block here when consumers lag → the
                # wait time is the CPU-bound signal.
                bt = time.perf_counter()
                admitted = _acquire(len(data))
                profiler.producer_blocked(time.perf_counter() - bt)
                if not admitted:  # cancelled before budget freed
                    result_q.put(("errors", cand))
                    continue
                read_q.put((cand, data, len(data)))
        finally:
            with iter_lock:
                live_producers[0] -= 1

    def _feeding() -> bool:
        with iter_lock:
            return live_producers[0] > 0

    def consumer():
        while True:
            it = time.perf_counter()
            try:
                item = read_q.get(timeout=_POLL)
            except _queue.Empty:
                # Exit once producers are done (queue truly drained) or on cancel.
                if cancel.is_set() or not _feeding():
                    return
                continue
            profiler.consumer_idle(time.perf_counter() - it)
            cand, data, nbytes = item
            try:
                outcome = "errors" if cancel.is_set() else _process_photo_bytes(
                    ctx, root_id, cand, data, full, seen_at, profiler, collector
                )
            except Exception:  # noqa: BLE001 - never let a worker die silently
                log.exception("photo worker failed on %s", cand.path)
                outcome = "errors"
            finally:
                _release(nbytes)  # free budget so a blocked producer can proceed
            result_q.put((outcome, cand))

    producers = [threading.Thread(target=producer, name=f"packrat-io-{i}", daemon=True) for i in range(n_io)]
    consumers = [threading.Thread(target=consumer, name=f"packrat-cpu-{i}", daemon=True) for i in range(n_cpu)]
    for th in producers + consumers:
        th.start()

    # Drain exactly len(cands) results on this thread (progress + cancel live here).
    try:
        for _ in range(len(cands)):
            outcome, cand = result_q.get()
            record(outcome, cand)
    finally:
        cancel.set()  # timeouts above let every worker notice and exit cleanly
        with budget_cv:
            budget_cv.notify_all()  # wake any producer parked on the byte budget
        for th in producers + consumers:
            th.join(timeout=10)


def _detect_deletions(ctx, root_id, existing, seen_fids, en: "Enumeration", report) -> None:
    """Forget instances gone from disk, unless under a suppressed (errored/pruned) subtree.

    An instance this pass never touched is a deletion candidate; we skip it only if
    its path lies under a subtree we couldn't authoritatively read (§10.1). Then any
    ``active`` asset left with zero instances is forgotten (§4 / §8 A2 step 11).
    """
    db = ctx.db
    gone: list[dict] = []
    for rec in existing.values():
        if rec["fid"] in seen_fids:
            continue
        if en.is_suppressed(rec["path"]):
            continue
        gone.append(rec)
    if not gone:
        return
    affected_assets = {rec["asset_id"] for rec in gone}
    with db.transaction() as conn:
        for rec in gone:
            conn.execute("DELETE FROM file_instances WHERE id=?", (rec["fid"],))
        report["deleted_instances"] += len(gone)
        before = conn.execute(
            "SELECT COUNT(*) c FROM assets WHERE status='active'"
        ).fetchone()["c"]
        for asset_id in affected_assets:
            _forget_if_orphaned(conn, asset_id)
        after = conn.execute(
            "SELECT COUNT(*) c FROM assets WHERE status='active'"
        ).fetchone()["c"]
        report["forgotten_assets"] += before - after


# ---------------------------------------------------------------------------
# job handler (§8 A2)
# ---------------------------------------------------------------------------
def _run_scan(ctx: JobContext) -> None:
    params = ctx.params
    full = bool(params.get("full"))
    embed = bool(params.get("embed"))
    dry_run = bool(params.get("dry_run"))
    is_all = bool(params.get("all"))
    profiler = ScanProfiler() if params.get("profile") else NULL_PROFILER
    collector = ScanReport()  # always-on problem-file capture (persisted per §scan-results)
    db = ctx.db
    seen_at = now_iso()

    # Resolve the target roots.
    if is_all:
        target_rows = [dict(r) for r in db.query("SELECT * FROM roots WHERE enabled=1 ORDER BY id")]
    else:
        row = db.query_one("SELECT * FROM roots WHERE id=?", (params.get("root_id"),))
        if row is None:
            raise ValueError(f"no such root id: {params.get('root_id')}")
        target_rows = [dict(row)]

    reports: list[dict] = []
    skipped_roots: list[dict] = []

    # Filter out trash/busy roots, then enumerate each survivor once (§10.1: one
    # round-trip per directory). We keep each Enumeration so we don't walk twice.
    plan: list[tuple[dict, Enumeration]] = []
    for root_row in target_rows:
        if root_row["kind"] == "trash":
            if is_all:
                skipped_roots.append({"name": root_row["name"], "reason": "trash root (never scanned)"})
                continue
            raise ValueError(
                f"{root_row['name']!r} is a trash root; scan never indexes trash folders (§6.1)"
            )
        holder = roots.root_holder(db, int(root_row["id"]))
        if holder is not None:
            # Manual scan is already rejected by the queue (guarantee 2); this
            # covers --all, which skips + logs rather than failing the sweep.
            skipped_roots.append({"name": root_row["name"], "reason": f"busy: {holder['what']}"})
            ctx.log(f"skip {root_row['name']}: {holder['what']}")
            continue
        ctx.check_cancelled()
        ignore = IgnoreSet.build(ctx.config, roots.ignore_globs_of(root_row))
        with profiler.timer("shared", "enumerate"):
            en = enumerate_root(root_row["path"], ignore)
        plan.append((root_row, en))

    if embed:
        ctx.log("note: --embed pass is deferred to M7; scan wrote no embeddings.")

    if not dry_run:
        ctx.set_total(sum(len(en.candidates) for _row, en in plan))

    done = 0
    for root_row, en in plan:
        ctx.check_cancelled()
        ctx.log(f"scanning {root_row['name']} ({root_row['path']})")
        rep, done = _scan_one_root(ctx, root_row, en, full=full, dry_run=dry_run,
                                   seen_at=seen_at, done=done, profiler=profiler,
                                   collector=collector)
        if not dry_run and full:
            db.execute("UPDATE roots SET last_full_scan_at=? WHERE id=?", (seen_at, root_row["id"]))
        reports.append(rep)

    # Persist the scan result (per (job, root)) + problem files so `status <root>`
    # and the M6 TUI can re-render this scan. Dry-run writes nothing.
    if not dry_run:
        _persist_scan_result(ctx, reports, full=full, embed=embed, profiler=profiler,
                             collector=collector, created_at=seen_at)

    _emit_summary(ctx, reports, skipped_roots, collector, dry_run=dry_run, full=full, embed=embed)
    if profiler.enabled:
        for line in profiler.report_lines():
            ctx.log(line)


def _persist_scan_result(ctx, reports, *, full, embed, profiler, collector, created_at) -> None:
    """Write one scan_results row per root + its problem files, in one transaction.

    Keyed to this job (``ctx.job_id``); cascades away if the jobs row is deleted
    (e.g. dev clear-db). profile_json is stored only when profiling was on.

    **Resume-proof problem set (§ interrupted-scan review).** The report must
    describe the *root's current state*, not just what this pass touched — because
    resuming an interrupted scan is "re-run the same command", and the fast-path
    then skips already-fingerprinted files (undecodables included, §8 A2 step 4).
    So a per-pass problem list would empty out on every re-run. Instead:
    - **undecodable** problem files + count are **re-derived from the catalog**
      (``assets.undecodable=1`` with a live instance in the root) — cumulative and
      identical across re-runs. This overrides the per-pass ``undecodable`` counter.
    - **read-error** files stay per-pass (an unreadable file has no asset to query,
      and leaves no row to fast-path-skip, so it is re-detected on every pass).

    Runs after all scan work has committed, so it is a *reporting* write. If the
    daemon is shutting down and the connection is already closed, swallow it (same
    contract as the queue's worker writes) rather than flip a successful scan to
    ``error`` — the catalog writes that matter already committed per-file.
    """
    import json
    import sqlite3

    profile_json = json.dumps(profiler.snapshot_json()) if profiler.enabled else None
    read_errors = [p for p in collector.problems() if p.problem == "read-error"]
    db = ctx.db
    try:
        with db.transaction() as conn:
            for rep in reports:
                root_id = rep["root_id"]
                # Re-derive the root's current undecodable files from the catalog.
                undec_rows = conn.execute(
                    "SELECT DISTINCT fi.path, a.media_type, a.content_hash, a.decode_error "
                    "FROM assets a JOIN file_instances fi ON fi.asset_id=a.id "
                    "WHERE fi.root_id=? AND a.undecodable=1 ORDER BY fi.path",
                    (root_id,),
                ).fetchall()
                undecodable_count = len(undec_rows)
                conn.execute(
                    "INSERT INTO scan_results("
                    "job_id, root_id, root_name, full, embed, profiled, candidates, new, "
                    "exact_dup, backfilled, matches_trashed, skipped_fastpath, undecodable, "
                    "errors, deleted_instances, forgotten_assets, root_offline, profile_json, "
                    "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ctx.job_id, root_id, rep["name"], 1 if full else 0,
                     1 if embed else 0, 1 if profiler.enabled else 0,
                     rep.get("candidates", 0), rep.get("new", 0), rep.get("exact_dup", 0),
                     rep.get("backfilled", 0), rep.get("matches_trashed", 0),
                     rep.get("skipped_fastpath", 0), undecodable_count,
                     rep.get("errors", 0), rep.get("deleted_instances", 0),
                     rep.get("forgotten_assets", 0), 1 if rep.get("root_offline") else 0,
                     profile_json, created_at),
                )
                for r in undec_rows:
                    conn.execute(
                        "INSERT INTO scan_problem_files("
                        "job_id, root_id, path, media_type, problem, content_hash, detail) "
                        "VALUES (?,?,?,?, 'undecodable', ?, ?)",
                        (ctx.job_id, root_id, r["path"], r["media_type"],
                         r["content_hash"], r["decode_error"]),
                    )
            # read-errors are per-pass (no catalog row to re-derive).
            for pf in read_errors:
                conn.execute(
                    "INSERT INTO scan_problem_files("
                    "job_id, root_id, path, media_type, problem, content_hash, detail) "
                    "VALUES (?,?,?,?, 'read-error', ?, ?)",
                    (ctx.job_id, pf.root_id, pf.path, pf.media_type, pf.content_hash, pf.detail),
                )
    except sqlite3.ProgrammingError as exc:
        if "closed database" in str(exc).lower():
            log.debug("db closed during shutdown; dropping scan-result persist")
            return
        raise


def _emit_summary(ctx, reports, skipped_roots, collector, *, dry_run, full, embed) -> None:
    agg = {k: 0 for k in ("new", "exact_dup", "matches_trashed", "backfilled",
                          "undecodable", "errors", "skipped_fastpath",
                          "deleted_instances", "forgotten_assets", "candidates")}
    for rep in reports:
        for k in agg:
            agg[k] += rep.get(k, 0)
    if dry_run:
        would = sum(r.get("would_index", 0) for r in reports)
        ctx.log(f"dry-run: {agg['candidates']} candidate file(s), {would} would be (re)fingerprinted.")
    else:
        ctx.log(
            f"scan done: {agg['new']} new · {agg['exact_dup']} exact-dup instances · "
            f"{agg['backfilled']} filled in missing fingerprints · {agg['matches_trashed']} identified trash · "
            f"{agg['skipped_fastpath']} skipped (fast-path) · {agg['undecodable']} undecodable · "
            f"{agg['errors']} errors · {agg['deleted_instances']} instances gone "
            f"({agg['forgotten_assets']} assets forgotten)."
        )
        # The persisted report lists the root's *current* undecodables (re-derived
        # from the catalog, so a resume/re-run stays accurate) plus this pass's
        # read-errors — `status <root>` shows the full set with paths + reasons.
        n_read_err = sum(1 for p in collector.problems() if p.problem == "read-error")
        if agg["undecodable"] or n_read_err:
            extra = f" · {n_read_err} unreadable this pass" if n_read_err else ""
            ctx.log(
                f"problem files recorded{extra} — `packrat status <root>` lists paths + reasons."
            )
    for sk in skipped_roots:
        ctx.log(f"skipped root {sk['name']}: {sk['reason']}")
    # (Per-file outcomes are surfaced via these log lines + the persisted
    # scan_results/scan_problem_files, which `status <root>` re-renders — §4.)


register_job(
    JobSpec(
        type="scan",
        handler=_run_scan,
        mutating=True,
        # Manual `scan <root>` owns its root → queue rejects if busy (guarantee 2).
        # `scan --all` has no root_id → owns nothing → iterates + skips busy roots.
        owned_root=lambda params: params.get("root_id"),
    )
)
