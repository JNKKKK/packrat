r"""Refresh the trash collection (§6.1) — the shared trash-absorb procedure.

This is the step that turns files sitting in a registered ``kind='trash'`` root
into **permanent trashed fingerprints** and then empties the folder. It is invoked
automatically at the start of ``cleanup`` and ``merge`` (M5), and exposed directly
as the ``trash refresh`` job. Kept operation-agnostic here (like :mod:`review` /
:mod:`matcher`) so all three consumers share one implementation.

Steps (§6.1), for **every** registered trash root:
1. Enumerate its files (same allowlist/ignore rules as scan). For each file compute
   BLAKE3 (+ perceptual signature on a *miss*, so the trashed fingerprint supports
   ``cleanup --perceptual``). Resolve against ``assets.content_hash``:
   - **new content** → create an asset ``status='trashed'``, ``trash_reason='trash-folder'``,
     persisting its phash/vphash (no embedding).
   - **matches an ``active`` asset** → flip it to ``trashed`` (retain fingerprints);
     its library-folder instances stay on disk until a ``cleanup`` removes them.
   - **matches a ``trashed`` asset** → already remembered; nothing to add.
2. Physically remove the file (Recycle Bin — permanent on NAS/SMB, §10).

**Crash-safety ordering (required):** step 1 (record → DB, committed) completes
**before** step 2 deletes that file — never delete first, or a crash between would
lose the trashed fingerprint. Recording is idempotent (re-hashing the same file
yields the same asset), so a crash mid-refresh just re-processes survivors next run.

**No dry-run variant (§6.1).** Putting a file in a trash folder *is* the act of
trashing it, so refresh always absorbs-and-empties — even when its caller
(``cleanup``/``merge``) is running ``--dry-run``. Trash roots are **never** indexed
by ``scan`` (§8 A2 step 1), so refresh is the only writer of trash-folder state.
"""

from __future__ import annotations

import logging
import os

from . import fsutil, media, roots, shortcuts
from .ignore import IgnoreSet
from .jobs import scan as _scan
from .util import now_iso

log = logging.getLogger("packrat.trash")


def _new_summary() -> dict:
    return {
        "roots": 0,
        "candidates": 0,
        "new_trashed": 0,      # novel content → new trashed asset
        "flipped": 0,          # matched an active asset → flipped to trashed
        "already_trashed": 0,  # matched a trashed asset → nothing to add
        "undecodable": 0,      # a new trashed asset whose pixels wouldn't decode
        "emptied": 0,          # files moved to the Recycle Bin
        "undeletable": 0,      # recorded but the file could not be removed (locked/denied)
        "errors": 0,           # unreadable bytes → could not record → left in place
    }


def refresh_trash(ctx) -> dict:
    r"""Absorb + empty every registered trash root (§6.1). Returns a summary dict.

    Always runs for real (there is no dry-run refresh, §6.1). Cooperatively
    cancellable at the per-file checkpoint; a cancel leaves already-processed files
    absorbed+emptied and the rest untouched (re-run re-processes survivors).
    """
    db = ctx.db
    summary = _new_summary()
    trash_roots = [
        dict(r)
        for r in db.query("SELECT * FROM roots WHERE kind='trash' AND enabled=1 ORDER BY id")
    ]
    if not trash_roots:
        ctx.log("trash refresh: no trash roots registered — nothing to absorb.")
        return summary

    # Enumerate every trash root up front (one scandir round-trip per directory,
    # §10.1), then process the combined worklist so progress spans all roots.
    worklist: list[tuple[dict, _scan.Candidate]] = []
    for root in trash_roots:
        summary["roots"] += 1
        ignore = IgnoreSet.build(ctx.config, roots.ignore_globs_of(root))
        en = _scan.enumerate_root(root["path"], ignore)
        if en.root_offline:
            # Unreachable trash root (share down / drive unplugged). Skipping it is
            # safe — nothing is deleted — but it must NOT read as "absorbed and
            # emptied": whatever is in it stays untracked (§10.1). Surface it.
            summary["offline_roots"] = summary.get("offline_roots", 0) + 1
            ctx.log(f"trash refresh: skipped offline/unreadable trash root {root['name']!r} "
                    "(nothing absorbed from it).")
            continue
        for cand in en.candidates:
            worklist.append((root, cand))
    summary["candidates"] = len(worklist)

    ctx.set_total(len(worklist))
    done = 0
    for root, cand in worklist:
        ctx.check_cancelled()
        recorded = _absorb_file(ctx, cand, summary)
        done += 1
        ctx.progress(done, message=os.path.basename(cand.path))
        # Record-then-delete: only remove the file once its fingerprint is
        # committed (§6.1). An unreadable file was never recorded → leave it.
        if recorded:
            _empty_file(cand.path, summary)

    ctx.log(
        f"trash refresh: {summary['new_trashed']} new trashed · {summary['flipped']} flipped "
        f"active→trashed · {summary['already_trashed']} already known · {summary['emptied']} "
        f"file(s) emptied ({summary['undeletable']} could not delete, {summary['errors']} unreadable)."
    )
    return summary


def _absorb_file(ctx, cand: "_scan.Candidate", summary: dict) -> bool:
    """Record one trash file's fingerprint to the trashed set (§6.1 step 1).

    Returns True if the fingerprint is committed (so the file may now be deleted),
    False if the bytes could not even be read (leave the file in place, report it).
    Decode (perceptual) runs **only on a content-hash miss** — a hit already has its
    signatures from scan, so re-decoding would be wasted work over SMB.
    """
    db = ctx.db
    mtype = media.media_type_of(cand.path) or "photo"
    try:
        content_hash = media.hash_file(cand.path, medium=mtype)
    except OSError as exc:
        summary["errors"] += 1
        log.warning("cannot read trash file %s: %s", cand.path, exc)
        return False

    asset = db.query_one("SELECT id, status FROM assets WHERE content_hash=?", (content_hash,))
    if asset is None:
        # New content → decode for perceptual, then create a trashed asset.
        fp = media.Fingerprint(media_type=mtype, content_hash=content_hash, size=cand.size)
        media.fill_perceptual(fp, cand.path, ctx.config)
        _create_trashed_asset(db, fp)
        summary["new_trashed"] += 1
        if fp.undecodable:
            summary["undecodable"] += 1
    elif asset["status"] == "active":
        # The user is telling us this content is junk — flip it (retain fingerprints).
        # Its library-folder instances stay on disk until a `cleanup` removes them.
        db.execute(
            "UPDATE assets SET status='trashed', trashed_at=?, trash_reason='trash-folder' WHERE id=?",
            (now_iso(), int(asset["id"])),
        )
        summary["flipped"] += 1
    else:  # already trashed — remembered forever, nothing to add.
        summary["already_trashed"] += 1
    return True


def _create_trashed_asset(db, fp: media.Fingerprint) -> None:
    """Insert a new ``status='trashed'`` asset + its perceptual rows (§6.1).

    Idempotent on ``content_hash`` (``ON CONFLICT DO NOTHING``): a crash-then-re-run
    that re-hashes a not-yet-deleted file becomes a hit (``already_trashed``), never a
    second asset — but the guard also protects a same-run duplicate within the folder.
    """
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO assets(content_hash, media_type, size, width, height, duration_s, "
            "captured_at, status, undecodable, decode_error, detail_score, codec, added_at, "
            "trashed_at, trash_reason) "
            "VALUES (?,?,?,?,?,?,?, 'trashed', ?, ?, ?, ?, ?, ?, 'trash-folder') "
            "ON CONFLICT(content_hash) DO NOTHING",
            (fp.content_hash, fp.media_type, fp.size, fp.width, fp.height, fp.duration_s,
             fp.captured_at, 1 if fp.undecodable else 0, fp.decode_error, fp.detail_score,
             fp.codec, now_iso(), now_iso()),
        )
        if cur.rowcount == 1:
            row = conn.execute(
                "SELECT id FROM assets WHERE content_hash=?", (fp.content_hash,)
            ).fetchone()
            _scan._insert_perceptual(conn, int(row["id"]), fp)


def _empty_file(path: str, summary: dict) -> None:
    """Move an absorbed trash file to the Recycle Bin (§6.1 step 2).

    Its fingerprint is already committed, so a delete that fails (locked / permission
    denied) is non-fatal — leave the file and report it; a later refresh retries the
    delete (a DB no-op). Permanent on a NAS/SMB trash root (no Recycle Bin, §10).
    """
    try:
        shortcuts.recycle(path)
        summary["emptied"] += 1
    except FileNotFoundError:
        pass  # already gone (e.g. a prior interrupted refresh removed it)
    except Exception as exc:  # noqa: BLE001 - never block the whole refresh on one stuck file
        summary["undeletable"] += 1
        log.warning("could not empty trash file %s: %s", path, exc)
