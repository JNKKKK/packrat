r"""The ``untrash`` operation (§6.3) — forget content from trash memory.

The reversal for an accidental trash: **remove a fingerprint from the permanent
trashed-hash set** so the content is no longer excluded from future merges. You
*present the file* (packrat stores no pixels to preview, so the real thing is the
identifier); untrash hashes it and matches by **exact content hash**.

**It does not restore bytes** — that is the Recycle Bin's job (§10). untrash only
reads files (to hash) and writes DB rows; it moves/deletes nothing on disk.

The ``<path>`` is arbitrary bytes to hash — it **need not** be a registered root and
untrash never catalogs it (the key difference from ``scan``/``cleanup``). A file, or
a folder walked recursively with the same allowlist/ignore rules as scan (§8 A1).

Per matched **trashed** asset (§6.3 step 3, mirrors §4's forget/keep, inverted):
- **still has ≥1 live instance** → flip ``status`` back to ``active``, clear
  ``trashed_at``/``trash_reason``, retain fingerprints — it rejoins in place.
- **zero instances** → **forget it entirely** (delete the asset, cascade its
  fingerprints), so the content is treated as brand-new if it reappears.

An ``active`` match is a no-op (``already-active``); an unknown hash a no-op
(``unknown``) — untrash **never creates** an asset. Owns no root (never blocked by /
blocks a review or merge, §3); does **not** call trash-refresh, so ``--dry-run``
truly changes nothing (§6.1's always-absorb rule doesn't apply here).
"""

from __future__ import annotations

import logging
import os

from .. import fsutil, media
from ..ignore import IgnoreSet
from ..util import now_iso
from .context import JobContext
from .registry import JobSpec, register_job
from .scan import enumerate_root

log = logging.getLogger("packrat.jobs.untrash")


def _run_untrash(ctx: JobContext) -> None:
    db = ctx.db
    arg = ctx.params.get("path")
    dry_run = bool(ctx.params.get("dry_run"))
    if not arg:
        raise ValueError("untrash needs a <path> (a file or folder to hash).")

    files = _resolve_targets(ctx, arg)
    ctx.log(
        f"untrash {'(dry-run) ' if dry_run else ''}scanning {len(files)} media file(s) under {arg}"
    )

    summary = {"untrashed": 0, "forgotten": 0, "already_active": 0, "unknown": 0, "errors": 0}
    ctx.set_total(len(files))
    done = 0
    for path in files:
        ctx.check_cancelled()
        done += 1
        ctx.progress(done, message=os.path.basename(path))
        _untrash_one(db, path, summary, dry_run=dry_run)

    verb = "would forget" if dry_run else "forgot"
    ctx.log(
        f"untrash {'(dry-run) ' if dry_run else ''}done: {summary['untrashed']} reactivated in place, "
        f"{summary['forgotten']} {verb} (blocklist entry dropped), "
        f"{summary['already_active']} already active, {summary['unknown']} unknown"
        + (f", {summary['errors']} unreadable" if summary['errors'] else "")
        + ". Nothing on disk changed."
    )


def _resolve_targets(ctx: JobContext, arg: str) -> list[str]:
    r"""Resolve ``arg`` to a list of media file paths (§6.3 step 1).

    A single file (kept only if it clears the media allowlist), or a folder walked
    recursively with the scan ignore set. Errors if the path is missing/unreadable.
    ``arg`` is NOT resolved against roots — it is just bytes to hash (§6.3).
    """
    canon = fsutil.canonicalize(arg)
    ext = fsutil.extended(canon)
    if not os.path.exists(ext):
        raise ValueError(f"path does not exist: {canon}")
    # No per-root --ignore globs (arg isn't a root) — just config allowlist + built-ins.
    ignore = IgnoreSet.build(ctx.config)
    if os.path.isdir(ext):
        en = enumerate_root(canon, ignore)
        if en.root_offline:
            raise ValueError(f"cannot read folder: {canon}")
        return [c.path for c in en.candidates]
    # A single file: hash it only if it is allowlisted media (non-media is skipped —
    # it could never be in the catalog anyway, so a match is impossible).
    if not ignore.is_media(os.path.basename(canon)):
        return []
    return [canon]


def _untrash_one(db, path: str, summary: dict, *, dry_run: bool) -> None:
    """Hash one file and forget/reactivate its trashed asset (§6.3 steps 2–3)."""
    try:
        content_hash = media.hash_file(path)
    except OSError as exc:
        summary["errors"] += 1
        log.warning("cannot read %s: %s", path, exc)
        return

    asset = db.query_one("SELECT id, status FROM assets WHERE content_hash=?", (content_hash,))
    if asset is None:
        summary["unknown"] += 1
        return
    if asset["status"] == "active":
        summary["already_active"] += 1
        return

    # A trashed asset: reactivate in place if it still has live instances, else forget.
    asset_id = int(asset["id"])
    n = db.query_one(
        "SELECT COUNT(*) c FROM file_instances WHERE asset_id=?", (asset_id,)
    )["c"]
    if n:
        if not dry_run:
            db.execute(
                "UPDATE assets SET status='active', trashed_at=NULL, trash_reason=NULL WHERE id=?",
                (asset_id,),
            )
        summary["untrashed"] += 1
    else:
        if not dry_run:
            db.execute("DELETE FROM assets WHERE id=?", (asset_id,))  # cascade fingerprints
        summary["forgotten"] += 1


register_job(
    JobSpec(
        type="untrash",
        handler=_run_untrash,
        mutating=True,
        owned_root=None,  # targets no root — arbitrary bytes to hash (§6.3)
    )
)
