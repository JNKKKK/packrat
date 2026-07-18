r"""The ``merge`` operation (§8 C) — copy into a folder only what's new to the collection.

Headline use case: export the whole iPhone to a temp folder, then copy only the
genuinely-new items into a backup folder. **Merge is deliberately simple:**
``merge = discard trash + copy what's new``, decided **entirely by exact content
hash** — no perceptual/near-dup matching, no CLIP, no review folder, no interactive
pause (the opposite of ``dedup``/``cleanup``). It *does* collapse **byte-identical**
duplicates (within the source and against the collection); recompressed near-dup
cleanup is ``dedup``'s job, run afterward.

**Guarantees:** the **source is never modified** (read-only). The destination is
**copy-only** (no deletes/overwrites of existing content). "New" is judged against
the **entire collection** by exact hash, and files matching a **trashed** hash are
discarded.

Phase map (§8 C):
- **Phase 0** — validate source/dest; refresh the trash collection (§6.1, for real
  even under ``--dry-run``); opportunistically fast-path-scan the dest root so the
  comparison set is current; open a ``merge_runs`` header (the cross-op guard +
  resume anchor).
- **Phase 1** — fingerprint the transient source: **BLAKE3 only**, persisted to
  ``merge_plan_items`` so a resume skips re-hashing (the dominant SMB cost, §10.1).
- **Phase 2** — classify each rep by exact hash: ``dup-in-source`` / ``trashed`` /
  ``exact-known`` / ``new``. Once classified the plan is **frozen** (``status='copying'``).
- **Phase 3** — copy the ``new`` reps (hash-verify → atomic rename, structure-mirrored)
  and register them as **un-perceptual** assets (a later ``scan``/``dedup`` backfills
  ``phash``/``vphash``). A file landing on an *ignored* dest path is copied but left
  **uncatalogued** (``copied-unindexed``) — registering it would let the next scan
  silently forget it (§8 C step 11).
- **Phase 4** — report; warn per ignored dest subpath.

**Resume** (§8 C Safety & resume): re-running ``merge <source> --into <dest>`` while an
open (``planning``/``copying``) ``merge_runs`` row exists for this dest **silently
auto-resumes** it. A ``copying`` run replays the frozen plan verbatim (no re-hash, no
re-classify); a ``planning`` run (Phase 1 interrupted before the plan froze) rebuilds
the plan. Per-item ``progress`` closes the copied→registered crash gap.

**Cancel / stop** (§8 C interruption): merge has no interactive pause and no
``--cancel`` flag. A cooperative cancel / clean daemon stop / crash all leave the
``merge_runs`` row **open** (copy-only ⇒ a partial copy is safe), so re-running
resumes. *(Because the runtime can't distinguish a user cancel from a clean daemon
stop — both use the shared cancel flag — merge favors §3's "a stop never loses
in-flight progress": it leaves the plan resumable rather than discarding it. See
docs/M5.md.)*
"""

from __future__ import annotations

import logging
import os
import shutil

from .. import fsutil, media, paths, roots, trash
from ..ignore import IgnoreSet
from ..util import now_iso
from .context import JobContext
from .registry import JobSpec, register_job
from .scan import ScanReport, _scan_one_root, _upsert_instance, enumerate_root

log = logging.getLogger("packrat.jobs.merge")

_OPEN = ("planning", "copying")


# ===========================================================================
# job dispatch
# ===========================================================================
def _run_merge(ctx: JobContext) -> None:
    if ctx.params.get("dry_run"):
        _dry_run(ctx)
    else:
        _merge(ctx)


def _resolve_and_validate(ctx: JobContext) -> tuple[dict, str, str]:
    r"""Resolve the dest root + validate source/dest (§8 C Phase 0 steps 1–2).

    Returns ``(dest_root_row, source_canonical, dest_canonical)``. The daemon already
    resolved ``--into`` to a library root (``root_id`` + ``dest_path`` in params); here
    we re-read that root and validate the transient source. Raises ``ValueError`` on a
    missing/empty/unreadable source or a source⇄dest overlap.
    """
    p = ctx.params
    root = ctx.db.query_one("SELECT * FROM roots WHERE id=?", (p.get("root_id"),))
    if root is None:
        raise ValueError(f"no such root id: {p.get('root_id')}")
    root = dict(root)
    if root["kind"] != "library":
        raise ValueError(f"{root['name']!r} is a {root['kind']} root; merge --into targets a library root")

    source = fsutil.canonicalize(p.get("source") or "")
    dest = fsutil.canonicalize(p.get("dest_path") or root["path"])
    src_ext = fsutil.extended(source)
    if not source or not os.path.exists(src_ext):
        raise ValueError(f"source does not exist: {source}")
    if not os.path.isdir(src_ext):
        raise ValueError(f"source is not a directory: {source}")
    try:
        with os.scandir(src_ext) as it:
            if next(it, None) is None:
                raise ValueError(f"source is empty: {source}")
    except OSError as exc:
        raise ValueError(f"source is not readable: {source} ({exc})") from exc
    if fsutil.is_within(source, dest) or fsutil.is_within(dest, source):
        raise ValueError(f"source {source} and dest {dest} overlap; they must be disjoint")
    return root, source, dest


def _reject_if_held(ctx: JobContext, root: dict) -> None:
    """§3/§8 C Phase 0 step 2a defense: refuse if *another* op holds the dest root.

    The dequeue gate already holds a fresh merge behind a pending review / another
    open merge, and the single-worker slot serializes everything — so with the queue
    this can no longer fire. Kept as belt-and-suspenders (a resuming merge ignores its
    *own* open ``merge_runs`` row via ``ignore_merge_holder``, so only a pending review
    trips this).
    """
    holder = roots.root_holder(ctx.db, int(root["id"]), ignore_merge=True)
    if holder is not None:
        raise ValueError(
            f"dest root {root['name']!r} busy: {holder['what']} — confirm/cancel it before merging"
        )


# ===========================================================================
# the merge (real; §8 C Phases 0–4)
# ===========================================================================
def _merge(ctx: JobContext) -> None:
    db = ctx.db
    root, source, dest = _resolve_and_validate(ctx)
    _reject_if_held(ctx, root)
    root_id = int(root["id"])
    ctx.log(f"merge {source} → {root['name']} ({dest})")

    # Phase 0 step 3 — refresh the trash collection (real, even here). The trashed set
    # must be current before we classify incoming files as `trashed` (§6.1/§8 C).
    trash.refresh_trash(ctx)

    # Phase 0 step 5 — find an open run to resume, else open a fresh one.
    run = db.query_one(
        "SELECT * FROM merge_runs WHERE dest_root_id=? AND status IN ('planning','copying')",
        (root_id,),
    )
    if run is not None:
        run = dict(run)
        n_done = db.query_one(
            "SELECT COUNT(*) c FROM merge_plan_items WHERE run_id=? "
            "AND progress IN ('registered','copied-unindexed')",
            (run["id"],),
        )["c"]
        n_all = db.query_one(
            "SELECT COUNT(*) c FROM merge_plan_items WHERE run_id=?", (run["id"],)
        )["c"]
        ctx.log(f"resuming interrupted merge from {run['created_at']}: "
                f"{n_done} of {n_all} plan item(s) already done")
        db.execute("UPDATE merge_runs SET job_id=? WHERE id=?", (ctx.job_id, run["id"]))
    else:
        cur = db.execute(
            "INSERT INTO merge_runs(job_id, source_path, dest_path, dest_root_id, status, created_at) "
            "VALUES (?, ?, ?, ?, 'planning', ?)",
            (ctx.job_id, source, dest, root_id, now_iso()),
        )
        run = dict(db.query_one("SELECT * FROM merge_runs WHERE id=?", (int(cur.lastrowid),)))

    # Phase 0 step 4 + Phases 1–2 — (re)build the frozen plan unless already `copying`.
    if run["status"] != "copying":
        _scan_dest(ctx, root)                       # opportunistic fast-path scan (step 4)
        _build_plan(ctx, run)                        # Phase 1 (fingerprint) + Phase 2 (classify)
        run = dict(db.query_one("SELECT * FROM merge_runs WHERE id=?", (run["id"],)))

    # Phase 3 — copy the `new` reps + register (DB backup first, §8 C / §10). Copy to the
    # run's FROZEN dest_path (authoritative on resume — the plan owns source+dest).
    _backup_db(db, f"premerge-{root_id}")
    out = _copy_and_register(ctx, run, root, run["dest_path"])

    # Finalize (§8 C Safety & resume) — retained as queryable merge history (§14 #5).
    db.execute("UPDATE merge_runs SET status='done', finished_at=? WHERE id=?", (now_iso(), run["id"]))
    _report(ctx, root, source, out, dry_run=False)


def _scan_dest(ctx: JobContext, root: dict) -> None:
    r"""Opportunistically fast-path-scan the dest root (§8 C Phase 0 step 4).

    Runs under merge's ownership (no other op can touch the root), so its
    deletion-detection is safe. Best-effort: a scan failure only makes the comparison
    set stale (⇒ a redundant byte-dup `dedup` collapses later), never wrong — so we log
    and continue rather than abort the merge.
    """
    try:
        ignore = IgnoreSet.build(ctx.config, roots.ignore_globs_of(root))
        en = enumerate_root(root["path"], ignore)
        if en.root_offline:
            ctx.log(f"merge: dest root {root['name']!r} enumeration failed — comparison set may be stale.")
            return
        _scan_one_root(ctx, root, en, full=False, dry_run=False, seen_at=now_iso(),
                       done=0, collector=ScanReport())
    except Exception as exc:  # noqa: BLE001 - the scan is an optimization, not a gate
        log.warning("opportunistic dest scan failed: %s", exc)
        ctx.log(f"merge: opportunistic dest scan skipped ({exc}); comparison set may be stale.")


# ---------------------------------------------------------------------------
# Phase 1 + 2 — fingerprint the source, classify each rep, freeze the plan
# ---------------------------------------------------------------------------
def _build_plan(ctx: JobContext, run: dict) -> None:
    db = ctx.db
    run_id = int(run["id"])
    # Enumerate the run's FROZEN source_path — the same path _place_copy copies from
    # (the plan owns source+dest, §8 C), so a `planning`-resume can't drift to a
    # different --source arg pointing at the same dest root.
    source = run["source_path"]
    # A `planning`-resume rebuilds: nothing was copied yet (copy starts only after the
    # flip to `copying`), so wiping partial Phase-1 items and redoing is safe + simplest.
    db.execute("DELETE FROM merge_plan_items WHERE run_id=?", (run_id,))

    ignore = IgnoreSet.build(ctx.config)  # source isn't a root → config allowlist + built-ins
    en = enumerate_root(source, ignore)
    if en.root_offline:
        raise ValueError(f"source is not readable: {source}")
    cands = en.candidates

    # Phase 1 — BLAKE3 each source file (the resume-avoids-this SMB cost, §10.1).
    ctx.set_total(len(cands))
    items: list[dict] = []
    for i, cand in enumerate(cands, 1):
        ctx.check_cancelled()
        ctx.progress(i, message=os.path.basename(cand.path))
        try:
            content_hash = media.hash_file(cand.path)
            items.append({"rel": cand.rel, "size": cand.size, "mtime": cand.mtime,
                          "content_hash": content_hash, "progress": "pending"})
        except OSError as exc:
            log.warning("merge: unreadable source file %s: %s", cand.path, exc)
            items.append({"rel": cand.rel, "size": cand.size, "mtime": cand.mtime,
                          "content_hash": None, "progress": "error",
                          "error": f"{type(exc).__name__}: {exc}"[:500]})

    # Phase 2 step 8 — collapse exact-within-source dups (keep oldest-mtime rep).
    by_hash: dict[str, list[dict]] = {}
    for it in items:
        if it["content_hash"] is not None:
            by_hash.setdefault(it["content_hash"], []).append(it)
    for group in by_hash.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda x: (x["mtime"] if x["mtime"] is not None else 0.0, x["rel"]))
        for dup in group[1:]:
            dup["classification"] = "dup-in-source"
            dup["rep_of_hash"] = dup["content_hash"]

    # Phase 2 step 9 — classify each representative by exact hash against the DB.
    for group in by_hash.values():
        rep = min(group, key=lambda x: (x["mtime"] if x["mtime"] is not None else 0.0, x["rel"]))
        rep["classification"] = _classify_hash(db, rep["content_hash"])

    # Persist the frozen plan + flip to `copying` in one transaction.
    with db.transaction() as conn:
        for it in items:
            conn.execute(
                "INSERT INTO merge_plan_items(run_id, source_rel_path, size, mtime, content_hash, "
                "classification, rep_of_hash, progress, error) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, it["rel"], it["size"], it["mtime"], it["content_hash"],
                 it.get("classification"), it.get("rep_of_hash"),
                 it["progress"], it.get("error")),
            )
        conn.execute("UPDATE merge_runs SET status='copying' WHERE id=?", (run_id,))


def _classify_hash(db, content_hash: str) -> str:
    """Exact-hash classification of one rep (§8 C Phase 2 step 9). No perceptual compare."""
    asset = db.query_one("SELECT status FROM assets WHERE content_hash=?", (content_hash,))
    if asset is None:
        return "new"
    return "trashed" if asset["status"] == "trashed" else "exact-known"


# ---------------------------------------------------------------------------
# Phase 3 — copy the `new` reps + register (resume-aware per progress)
# ---------------------------------------------------------------------------
def _new_out() -> dict:
    return {"new": 0, "exact_known": 0, "trashed": 0, "dup_in_source": 0,
            "collisions": 0, "unindexed": 0, "errors": 0, "ignored_subpaths": {}}


def _copy_and_register(ctx: JobContext, run: dict, root: dict, dest: str) -> dict:
    db = ctx.db
    run_id = int(run["id"])
    root_id = int(root["id"])
    ignore = IgnoreSet.build(ctx.config, roots.ignore_globs_of(root))
    items = [dict(r) for r in db.query(
        "SELECT * FROM merge_plan_items WHERE run_id=? ORDER BY id", (run_id,))]
    out = _new_out()

    ctx.set_total(len(items))
    for i, it in enumerate(items, 1):
        ctx.check_cancelled()
        ctx.progress(i, message=os.path.basename(it["source_rel_path"]))
        cls = it["classification"]
        prog = it["progress"]

        if cls == "error" or it["content_hash"] is None:
            out["errors"] += 1
            continue
        if cls != "new":
            # Nothing to copy — count it, mark skipped (idempotent for resume).
            key = {"exact-known": "exact_known", "trashed": "trashed",
                   "dup-in-source": "dup_in_source"}.get(cls)
            if key is not None:
                out[key] += 1
            if prog != "skipped":
                db.execute("UPDATE merge_plan_items SET progress='skipped' WHERE id=?", (it["id"],))
            continue

        # cls == 'new'
        if prog in ("registered", "copied-unindexed"):
            # Terminal (resume): count without touching disk (matters over SMB, §8 C resume).
            # Both were physically copied → count as `new`; an unindexed one is ALSO flagged
            # (same as the fresh path, where a `new` file marked copied-unindexed counts both).
            out["new"] += 1
            if prog == "copied-unindexed":
                out["unindexed"] += 1
                _note_ignored(out, root["path"], it["dest_path"])
            continue

        try:
            if prog == "copied" and it["dest_path"]:
                dest_path = it["dest_path"]              # crash between rename + register: finish it
            else:
                dest_path, kind = _place_copy(ctx, run, dest, it)
                if kind == "renamed":
                    out["collisions"] += 1
            _register_or_mark(ctx, root_id, root["path"], ignore, it, dest_path, out)
            out["new"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad file must not sink the whole merge
            log.warning("merge: could not copy/register %s: %s", it["source_rel_path"], exc)
            db.execute("UPDATE merge_plan_items SET progress='error', error=? WHERE id=?",
                       (f"{type(exc).__name__}: {exc}"[:500], it["id"]))
            out["errors"] += 1
    return out


def _place_copy(ctx: JobContext, run: dict, dest: str, it: dict) -> tuple[str, str]:
    r"""Copy one `new` source file into ``dest``, mirroring its structure (§8 C Phase 3 step 10).

    Preserves the source-relative path (``<dest>\<rel>``), creating intermediate dirs.
    A same-rel-path collision is resolved by content: identical bytes → reuse in place;
    different → numeric-suffix rename (``name (1).ext``). Writes to a temp name, verifies
    BLAKE3 == the frozen source hash, then atomically renames. Sets the item
    ``progress='copied'`` + its final ``dest_path``. Returns ``(dest_path, kind)`` where
    ``kind`` ∈ ``new`` | ``identical`` | ``renamed``.
    """
    db = ctx.db
    rel_native = it["source_rel_path"].replace("/", os.sep)
    src_abs = os.path.join(run["source_path"], rel_native)
    target = os.path.join(dest, rel_native)
    kind = "new"

    if os.path.exists(fsutil.extended(target)):  # collision at this rel path
        if _hash_or_none(target) == it["content_hash"]:
            kind = "identical"  # already there (unscanned) — register the existing file
        else:
            base, ext = os.path.splitext(target)
            n = 1
            while os.path.exists(fsutil.extended(f"{base} ({n}){ext}")):
                n += 1
            target = f"{base} ({n}){ext}"
            kind = "renamed"

    if kind != "identical":
        os.makedirs(fsutil.extended(os.path.dirname(target)), exist_ok=True)
        tmp = target + ".packrat-part"
        shutil.copyfile(fsutil.extended(src_abs), fsutil.extended(tmp))
        actual = media.hash_file(tmp)
        if actual != it["content_hash"]:
            try:
                os.remove(fsutil.extended(tmp))
            except OSError:
                pass
            raise ValueError(f"hash mismatch after copy ({actual} != {it['content_hash']})")
        os.replace(fsutil.extended(tmp), fsutil.extended(target))

    db.execute("UPDATE merge_plan_items SET progress='copied', dest_path=? WHERE id=?",
               (target, it["id"]))
    return target, kind


def _hash_or_none(path: str) -> str | None:
    """BLAKE3 of an existing file for a collision check, or None if unreadable."""
    try:
        return media.hash_file(path)
    except OSError:
        return None


def _register_or_mark(ctx: JobContext, root_id: int, root_path: str, ignore: IgnoreSet,
                      it: dict, dest_path: str, out: dict) -> None:
    r"""Register a copied `new` file, or mark it uncatalogued if under an ignored path.

    §8 C Phase 3 step 11 — the silent-forget fix: a file living under the dest root's
    ignore rules must NOT be registered (a later scan wouldn't enumerate it → deletion-
    detection would forget the asset while the file sits on disk). Such a file is copied
    but left ``copied-unindexed`` (terminal). Otherwise it becomes an ``active``,
    **un-perceptual** asset (metadata probed now; phash/vphash backfilled by a later
    scan/dedup), catalogued so a future merge recognizes it. Idempotent (asset keyed by
    ``content_hash``, instance by ``(root_id, path)``), so a resume replay is safe.

    The ignore check runs on the path **relative to the root** (not to ``<dest>``) —
    exactly what a later ``scan`` of the root will test (§8 C step 11).
    """
    db = ctx.db
    rel_to_root = os.path.relpath(dest_path, root_path).replace(os.sep, "/")
    if _dest_ignored(ignore, rel_to_root):
        db.execute("UPDATE merge_plan_items SET progress='copied-unindexed' WHERE id=?", (it["id"],))
        out["unindexed"] += 1
        _note_ignored(out, root_path, dest_path)
        return

    mtype = media.media_type_of(os.path.basename(dest_path)) or "photo"
    meta = media.probe_metadata(dest_path, mtype, ctx.config)
    from .scan import Candidate

    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO assets(content_hash, media_type, size, width, height, duration_s, "
            "captured_at, status, undecodable, decode_error, codec, added_at) "
            "VALUES (?,?,?,?,?,?,?, 'active', 0, NULL, ?, ?) ON CONFLICT(content_hash) DO NOTHING",
            (it["content_hash"], mtype, it["size"], meta.width, meta.height, meta.duration_s,
             meta.captured_at, meta.codec, now_iso()),
        )
        asset_id = int(conn.execute(
            "SELECT id FROM assets WHERE content_hash=?", (it["content_hash"],)).fetchone()["id"])
        cand = Candidate(path=dest_path, rel=rel_to_root,
                         size=it["size"], mtime=it["mtime"])
        _upsert_instance(conn, asset_id, root_id, cand, now_iso())
        conn.execute("UPDATE merge_plan_items SET progress='registered' WHERE id=?", (it["id"],))


def _dest_ignored(ignore: IgnoreSet, rel_to_root: str) -> bool:
    r"""True if a dest-relative path would be excluded by the dest root's ignore rules.

    Mirrors exactly what a later ``scan`` of the root would do to this file (§8 A1,
    §8 C step 11), so "registered ⟺ a scan would enumerate it":
    - **allowlist** — a non-media extension is never enumerated;
    - **file glob** — an ignore glob matching the file itself;
    - **directory prune** — an ignore glob matching **any ancestor directory** (e.g.
      ``Screenshots/`` or ``**/cache/**`` prunes the whole subtree in scan's walker via
      :meth:`IgnoreSet.is_dir_pruned`, so files beneath it are never seen). Merge isn't
      walking the dest, so it must reproduce that ancestor check itself.
    """
    name = os.path.basename(rel_to_root)
    if not ignore.is_media(name):
        return True
    if ignore.is_file_ignored(rel_to_root):
        return True
    # Would scan have pruned any ancestor directory before reaching this file?
    parts = rel_to_root.split("/")[:-1]  # directory segments above the file
    for i in range(1, len(parts) + 1):
        if ignore.is_dir_pruned("/".join(parts[:i])):
            return True
    return False


def _note_ignored(out: dict, root_path: str | None, dest_path: str) -> None:
    r"""Tally a ``copied-unindexed`` file under its ignored **dest subpath** (§8 C step 13).

    Grouping per distinct parent directory (not per file) makes the usual cause — a
    whole excluded subtree like ``Screenshots\`` or ``**/cache/**`` — obvious at a glance.
    """
    parent = os.path.dirname(dest_path) or dest_path
    out["ignored_subpaths"][parent] = out["ignored_subpaths"].get(parent, 0) + 1


# ===========================================================================
# DRY-RUN (§8 C Safety & resume) — classify in memory, write nothing
# ===========================================================================
def _dry_run(ctx: JobContext) -> None:
    r"""Preview the classification counts / would-copy list — copy nothing, write no rows.

    Opens **no** ``merge_runs``/``merge_plan_items`` (so it neither trips the cross-op
    guard nor leaves a resumable run), skips the opportunistic dest scan (must not mutate
    the catalog), and computes the would-be-ignored destinations up front so the user
    learns about an ignored ``--into`` *before* copying. **But Phase 0's trash refresh
    still runs for real** (§6.1) — trash inboxes are absorbed + emptied even in dry-run.
    """
    db = ctx.db
    root, source, dest = _resolve_and_validate(ctx)
    ctx.log(f"merge (dry-run) {source} → {root['name']} ({dest})")
    trash.refresh_trash(ctx)  # real even under --dry-run (§6.1)

    ignore = IgnoreSet.build(ctx.config)
    en = enumerate_root(source, ignore)
    if en.root_offline:
        raise ValueError(f"source is not readable: {source}")
    cands = en.candidates
    ctx.set_total(len(cands))

    # Phase 1 (hash) + Phase 2 (classify), in memory.
    hashed: list[dict] = []
    for i, cand in enumerate(cands, 1):
        ctx.check_cancelled()
        ctx.progress(i, message=os.path.basename(cand.path))
        try:
            ch = media.hash_file(cand.path)
        except OSError as exc:
            log.warning("merge dry-run: unreadable %s: %s", cand.path, exc)
            hashed.append({"cand": cand, "hash": None})
            continue
        hashed.append({"cand": cand, "hash": ch})

    dest_ignore = IgnoreSet.build(ctx.config, roots.ignore_globs_of(root))
    out = _new_out()
    seen_reps: set[str] = set()
    for h in hashed:
        if h["hash"] is None:
            out["errors"] += 1
            continue
        if h["hash"] in seen_reps:
            out["dup_in_source"] += 1
            continue
        seen_reps.add(h["hash"])
        cls = _classify_hash(db, h["hash"])
        if cls == "new":
            out["new"] += 1
            # Project the dest path + test it against the dest root's ignore rules.
            rel = h["cand"].rel
            projected = os.path.join(dest, rel.replace("/", os.sep))
            rel_to_root = os.path.relpath(projected, root["path"]).replace(os.sep, "/")
            if _dest_ignored(dest_ignore, rel_to_root):
                out["unindexed"] += 1
                _note_ignored(out, root["path"], projected)
        elif cls == "exact-known":
            out["exact_known"] += 1
        elif cls == "trashed":
            out["trashed"] += 1
    _report(ctx, root, source, out, dry_run=True)


# ===========================================================================
# report + backup (§8 C Phase 4, §10)
# ===========================================================================
def _report(ctx: JobContext, root: dict, source: str, out: dict, *, dry_run: bool) -> None:
    tag = "merge (dry-run)" if dry_run else "merge"
    verb = "would copy" if dry_run else "copied"
    ctx.log(
        f"{tag}: {out['new']} {verb} (new) · {out['exact_known']} exact-known · "
        f"{out['trashed']} trashed · {out['dup_in_source']} dup-in-source · "
        f"{out['collisions']} renamed · {out['errors']} error(s). Source unchanged."
    )
    # §8 C step 13 — ignored-destination warning, grouped per distinct subpath.
    for subpath, n in out["ignored_subpaths"].items():
        ctx.log(
            f"⚠ {n} file(s) {'would be copied' if dry_run else 'copied'} to an ignored path "
            f"({subpath}) — NOT catalogued; packrat won't track them, and a future merge "
            "will re-copy them as new. Move them to a non-ignored location (then `scan`), or "
            "adjust the root's ignore rules if the exclusion was unintended."
        )
    if not dry_run:
        ctx.log(f"suggest: `packrat scan {root['name']}` then `packrat dedup {root['name']}` "
                "to fingerprint the new files and clean recompressed near-dups.")

    ctx.set_result({
        "op": "merge", "dry_run": dry_run, "source": source, "dest_root": root["name"],
        "new": out["new"], "exact_known": out["exact_known"], "trashed": out["trashed"],
        "dup_in_source": out["dup_in_source"], "collisions": out["collisions"],
        "unindexed": out["unindexed"], "errors": out["errors"],
        "summary": f"{out['new']} {verb} · {out['exact_known']} known · "
                   f"{out['trashed']} trashed · {out['dup_in_source']} dup-in-source"
                   + (f" · {out['unindexed']} uncatalogued (ignored dest)" if out["unindexed"] else ""),
    })


def _backup_db(db, label: str) -> str:
    ts = now_iso().replace(":", "").replace("-", "")
    dest = paths.backups_dir() / f"{label}-{ts}.db"
    db.backup_to(dest)
    return str(dest)


register_job(
    JobSpec(
        type="merge",
        handler=_run_merge,
        mutating=True,
        # Merge OWNS the dest root (§8 C Phase 0 step 2a): the dequeue gate holds a
        # fresh merge behind a pending review / another open merge. `ignore_merge_holder`
        # lets a RESUMING merge past its OWN open merge_runs row (else it would deadlock
        # waiting on itself, §8 C). A --dry-run opens no run and owns None (writes nothing).
        owned_root=lambda p: None if p.get("dry_run") else p.get("root_id"),
        ignore_merge_holder=True,
    )
)
