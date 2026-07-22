r"""The ``cleanup`` operation (§6.2, §9.1) — cull trashed/undecodable files from a folder.

From the user's view: **delete files in ``<folder>`` that are junk.** ``cleanup``
requires exactly one **mode** (no bare default), dispatched by the ``mode`` param:

- **``--trash-exact``** (``mode='exact'``) — one-shot, CLI-orchestrated: a *preview*
  job refreshes the trash collection (§6.1) and counts library files whose asset is
  ``trashed`` by **exact hash**; the CLI prompts a typed confirmation showing the count
  (and any non-recyclable network paths, §10), then submits an *apply* job that recycles
  them. No staging folder, no ``review_runs`` row — exact-hash matching is
  false-positive-free.

- **``--trash-perceptual``** (``mode='perceptual'``) — stateful (analyze → pause →
  ``--confirm``). Adds *perceptual* trash matches (recompressed/resized copies of
  trashed content), staged as ``.lnk`` shortcuts in
  ``<root>\_packrat_review\_perceptually_identified_trash\`` for Explorer review.
  **delete-default** (like dedup's ``_exact_dup_to_delete\``): a staged shortcut =
  "will delete"; remove it to spare. Exact matches are **not** deleted inline in this
  mode — both exact and still-staged perceptual deletions apply together at ``--confirm``.

- **``--undecodable``** (``mode='undecodable'``) — one-shot (same preview → count-confirm
  → apply shape as ``--trash-exact``): delete the folder's ``undecodable=1`` files (§9.1
  — bytes hashed OK but the decoder rejected the pixels, so they carry no perceptual
  signature and can never dedup) **and mark each deleted asset ``trashed``**
  (``trash_reason='cleanup-undecodable'``) so a re-import of the same corrupt bytes is
  excluded from a future merge. Does **not** refresh the trash collection — it targets
  the folder's own undecodables, independent of the trashed set.

The two trash modes first **refresh the trash collection** (§6.1) so the trashed set
is current — this runs for real even under ``--dry-run`` (§6.1's always-absorb rule).
``--undecodable`` does not refresh.

Reuses the M3 machinery: the §5.3 :mod:`matcher` (here **active-vs-trashed**, the
single wider ``t_photo_edit`` cutoff, **no** recompress/edit banding), the
:mod:`review` plumbing (staging paths + audit trail), and the ``review_runs`` /
``review_actions`` state (single-stage: ``stage=1``). The reconcile analyze-rollback
(§3) already handles ``type='cleanup'``.
"""

from __future__ import annotations

import csv
import logging
import os

from .. import fsutil, matcher, review, shortcuts, trash
from ..util import now_iso
from . import _guards
from ._dbops import delete_instance as _delete_instance
from ._dbops import forget_if_orphaned as _forget_if_orphaned
from .context import JobContext
from .registry import JobSpec, register_job

log = logging.getLogger("packrat.jobs.cleanup")

RUN_TYPE = "cleanup-perceptual"


# ---------------------------------------------------------------------------
# job dispatch
# ---------------------------------------------------------------------------
def _run_cleanup(ctx: JobContext) -> None:
    """Dispatch by mode + sub-verb (§6.2).

    ``mode`` ∈ ``exact`` | ``perceptual`` | ``undecodable`` (the three required
    ``cleanup`` modes). ``exact`` and ``undecodable`` are one-shot (CLI-orchestrated
    preview → count-confirm → ``apply``); ``perceptual`` is the stateful analyze →
    ``--confirm``/``--cancel`` review run (which also bundles exact-trash deletions).
    """
    p = ctx.params
    # §6.2: a cleanup that DELETES (or opens a review) requires an EXPLICIT, valid mode —
    # there is no bare default that deletes. The CLI enforces this, but the daemon is the
    # authoritative contract (§1.6), so validate here too: never silently fall into a real
    # delete mode on a mode-less API call. A confirm/cancel acts on an existing pending
    # perceptual run (no mode needed); a read-only PREVIEW is harmless (acts on nothing),
    # so a mode-less preview is allowed and treated as the exact-trash count.
    mode = p.get("mode")
    acts = p.get("apply") or (mode == "perceptual" and not p.get("dry_run"))
    if acts and mode not in ("exact", "perceptual", "undecodable"):
        raise ValueError(
            f"cleanup requires a mode (--trash-exact / --trash-perceptual / "
            f"--undecodable); got {mode!r}"
        )
    mode = mode or "exact"   # mode-less → the (read-only) exact-trash preview
    if p.get("cancel"):
        _cancel(ctx)                       # perceptual run only
        action = "cancel"
    elif p.get("confirm"):
        _confirm(ctx)                      # perceptual run only
        action = "confirm"
    elif p.get("apply"):
        # `apply` is the one-shot commit for the count-confirm modes only. Perceptual
        # deletions apply via --confirm (a review run), NOT apply — reject the mismatch
        # rather than silently exact-deleting under a perceptual request.
        if mode == "undecodable":
            _apply_undecodable(ctx)
        elif mode == "exact":
            _apply_default_exact(ctx)
        else:
            raise ValueError(
                f"cleanup --apply is only for the one-shot modes (exact/undecodable); "
                f"perceptual applies via --confirm, not apply (got mode={mode!r})"
            )
        action = "delete"
    elif mode == "perceptual" and not p.get("dry_run"):
        _analyze_perceptual(ctx)
        action = "analyze"
    else:
        # preview (a mode's step-1) or any --dry-run: refresh + report, act on nothing.
        _preview(ctx, mode=mode, dry_run=bool(p.get("dry_run")))
        action = "dry-run" if p.get("dry_run") else "preview"
    # Uniform outcome (§4). Each leaf sets ctx._cleanup_outcome with its numbers;
    # fall back to just the action/mode label if a path recorded none.
    outcome = getattr(ctx, "_cleanup_outcome", None) or {}
    ctx.set_result({"op": "cleanup", "mode": mode, "action": action,
                    "summary": outcome.get("summary", f"{mode} · {action}"), **outcome})


def _resolve_library_root(ctx: JobContext) -> dict:
    return _guards.resolve_library_root(ctx, "cleanup")


def _reject_if_held(ctx, root: dict) -> None:
    """§3/§6.2 shared lock: reject if the root has *another* active op (not ours).

    A pending dedup review, a pending cleanup from a different invocation, or an
    in-flight merge stages/plans against this root — cleanup must not delete files
    those plans reference. The queue enforces this for the analyze that *owns* the
    root; preview/apply own nothing, so they re-check here.
    """
    _guards.reject_if_held(ctx, root)


# ---------------------------------------------------------------------------
# match discovery (pure DB / fingerprint math)
# ---------------------------------------------------------------------------
def _exact_match_instances(db, root_id: int) -> list[dict]:
    """Library instances in ``root_id`` whose asset is ``trashed`` (exact hash, §6.2).

    A byte-identical copy of trashed content shares its content_hash → same asset →
    the instance's asset is ``trashed``. False-positive-free.
    """
    rows = db.query(
        "SELECT fi.id fid, fi.asset_id, fi.path FROM file_instances fi "
        "JOIN assets a ON a.id=fi.asset_id "
        "WHERE fi.root_id=? AND a.status='trashed' ORDER BY fi.id",
        (root_id,),
    )
    return [{"instance_id": int(r["fid"]), "asset_id": int(r["asset_id"]), "path": r["path"]}
            for r in rows]


def _undecodable_instances(db, root_id: int) -> list[dict]:
    """Library instances in ``root_id`` whose asset is ``undecodable=1`` (§9.1).

    These are files whose bytes hashed fine but whose pixels/frames the decoder
    rejected (corrupt, unsupported codec, missing wheel), so they carry no perceptual
    signature and can never dedup. ``status <root>`` lists exactly this set as
    undecodable problem files. Excludes assets that are *already* trashed — deleting
    their instance needs no status flip (handled by the exact path if wanted) and this
    mode is about culling *active* junk. Deterministic + false-positive-free.
    """
    rows = db.query(
        "SELECT fi.id fid, fi.asset_id, fi.path FROM file_instances fi "
        "JOIN assets a ON a.id=fi.asset_id "
        "WHERE fi.root_id=? AND a.undecodable=1 AND a.status='active' ORDER BY fi.id",
        (root_id,),
    )
    return [{"instance_id": int(r["fid"]), "asset_id": int(r["asset_id"]), "path": r["path"]}
            for r in rows]


def _perceptual_candidates(ctx, root_id: int) -> list[dict]:
    """Folder instances whose (active) asset perceptually matches a *trashed* asset (§6.2).

    Runs the §5.3 matcher with the folder's active assets as targets and the whole
    trashed set as the pool (the wider ``t_photo_edit`` cutoff / video frame-vote —
    **no** dedup-style recompress/edit banding). Each matched target asset's
    library-folder instances become candidates; records which trashed asset it
    matched (``matched_trashed_asset_id``) and the distance for the manifest.
    """
    db = ctx.db
    cfg = ctx.config
    target_ids = {int(r["asset_id"]) for r in db.query(
        "SELECT DISTINCT fi.asset_id FROM file_instances fi JOIN assets a ON a.id=fi.asset_id "
        "WHERE fi.root_id=? AND a.status='active'", (root_id,))}
    if not target_ids:
        return []
    targets = matcher.load_signatures(db, asset_ids=target_ids, statuses=("active",))
    pool = matcher.load_signatures(db, asset_ids=None, statuses=("trashed",))
    edges = matcher.find_matches(targets, pool, cfg)
    if not edges:
        return []

    trashed_ids = {p.asset_id for p in pool.photos} | {v.asset_id for v in pool.videos}
    # Per target asset, keep the closest trashed match (smallest distance).
    best: dict[int, tuple[int, int]] = {}  # target_asset -> (trashed_asset, distance)
    for e in edges:
        if e.asset_a in trashed_ids:
            trashed_asset, target_asset = e.asset_a, e.asset_b
        else:
            trashed_asset, target_asset = e.asset_b, e.asset_a
        if target_asset not in target_ids:
            continue  # defensive: an edge whose active end isn't a folder target
        cur = best.get(target_asset)
        if cur is None or e.distance < cur[1]:
            best[target_asset] = (trashed_asset, e.distance)

    if not best:
        return []
    quals = _asset_qualities(db, set(best) | {t for t, _ in best.values()})
    hint = cfg.review.low_quality_hint

    candidates: list[dict] = []
    seq = 0
    for target_asset, (trashed_asset, distance) in best.items():
        # Every library-folder instance of this matched asset is a candidate (§6.2).
        insts = db.query(
            "SELECT id fid, path FROM file_instances WHERE asset_id=? AND root_id=? ORDER BY id",
            (target_asset, root_id),
        )
        my_q = quals.get(target_asset)
        tr_q = quals.get(trashed_asset)
        low_conf = (my_q is not None and my_q < hint) or (tr_q is not None and tr_q < hint)
        for inst in insts:
            seq += 1
            candidates.append({
                "asset_id": target_asset, "instance_id": int(inst["fid"]), "path": inst["path"],
                "matched_trashed_asset_id": trashed_asset, "distance": distance,
                "quality": my_q, "low_confidence": low_conf,
                "shortcut_name": f"{seq:04d}.lnk",
            })
    return candidates


def _asset_qualities(db, asset_ids):
    """Per-asset quality scalar (photo PDQ quality / video min comparable-frame quality)."""
    if not asset_ids:
        return {}
    ph = ",".join("?" for _ in asset_ids)
    q: dict[int, int] = {}
    for r in db.query(f"SELECT asset_id, quality FROM phash WHERE asset_id IN ({ph})", tuple(asset_ids)):
        if r["quality"] is not None:
            q[int(r["asset_id"])] = int(r["quality"])
    for r in db.query(
        f"SELECT asset_id, MIN(quality) mq FROM vphash WHERE asset_id IN ({ph}) GROUP BY asset_id",
        tuple(asset_ids),
    ):
        if r["mq"] is not None:
            q[int(r["asset_id"])] = int(r["mq"])
    return q


# ===========================================================================
# PREVIEW (default step-1 + any --dry-run) — refresh + report, act on nothing
# ===========================================================================
def _preview(ctx: JobContext, *, mode: str, dry_run: bool) -> None:
    root = _resolve_library_root(ctx)
    _reject_if_held(ctx, root)
    root_id = int(root["id"])
    tag = "dry-run" if dry_run else "preview"

    if mode == "undecodable":
        # No trash refresh — this mode is about culling the folder's own undecodable
        # files, independent of the trashed set.
        undec = _undecodable_instances(ctx.db, root_id)
        n_net = sum(1 for m in undec if fsutil.is_network_path(m["path"]))
        net = f" ({n_net} on a network share → permanent, no Recycle Bin)" if n_net else ""
        ctx.log(
            f"cleanup --undecodable {tag} for {root['name']}: {len(undec)} undecodable file(s)"
            f"{net} would be deleted + marked trashed. Nothing deleted."
        )
        ctx._cleanup_outcome = {"would_delete": len(undec),
                                "summary": f"undecodable · {len(undec)} would delete"}
        return

    # exact / perceptual both need the trashed set current → refresh (real even in dry-run, §6.1).
    trash.refresh_trash(ctx)
    exact = _exact_match_instances(ctx.db, root_id)
    n_net = sum(1 for m in exact if fsutil.is_network_path(m["path"]))
    net = f" ({n_net} on a network share → permanent, no Recycle Bin)" if n_net else ""
    if mode == "perceptual":
        cands = _perceptual_candidates(ctx, root_id)
        ctx.log(
            f"cleanup --trash-perceptual {tag} for {root['name']}: {len(exact)} exact-trash match(es)"
            f"{net}, {len(cands)} perceptual candidate(s) — nothing staged or deleted."
        )
        ctx._cleanup_outcome = {"exact": len(exact), "perceptual": len(cands),
                                "summary": f"perceptual · {len(exact)} exact, {len(cands)} candidates"}
    else:
        ctx.log(
            f"cleanup --trash-exact {tag} for {root['name']}: {len(exact)} file(s) match trashed "
            f"content (exact hash){net}. Nothing deleted."
        )
        ctx._cleanup_outcome = {"would_delete": len(exact),
                                "summary": f"exact · {len(exact)} would delete"}


# ===========================================================================
# DEFAULT EXACT APPLY (CLI submits after the user confirms the preview count)
# ===========================================================================
def _apply_default_exact(ctx: JobContext) -> None:
    root = _resolve_library_root(ctx)
    _reject_if_held(ctx, root)
    root_id = int(root["id"])
    exact = _exact_match_instances(ctx.db, root_id)
    if not exact:
        ctx.log(f"cleanup {root['name']}: no exact trash matches to delete.")
        return
    ctx.db.backup_labeled(f"precleanup-{root_id}")
    out = _new_out()
    ctx.set_total(len(exact))
    done = 0
    for m in exact:
        ctx.check_cancelled()
        done += 1
        ctx.progress(done, message=os.path.basename(m["path"]))
        _delete_one(ctx.db, m, out, trash_reason=None)  # asset already trashed
    ctx.log(
        f"cleanup {root['name']}: deleted {out['deleted']} exact-trash file(s) "
        f"({out['network']} permanent on network), {out['already_gone']} already gone."
    )
    ctx._cleanup_outcome = {"deleted": out["deleted"], "already_gone": out["already_gone"],
                            "summary": f"exact · {out['deleted']} deleted"}


# ===========================================================================
# UNDECODABLE APPLY (§9.1) — delete the folder's undecodable files + mark trashed
# ===========================================================================
def _apply_undecodable(ctx: JobContext) -> None:
    root = _resolve_library_root(ctx)
    _reject_if_held(ctx, root)
    root_id = int(root["id"])
    undec = _undecodable_instances(ctx.db, root_id)
    if not undec:
        ctx.log(f"cleanup {root['name']}: no undecodable files to delete.")
        return
    ctx.db.backup_labeled(f"precleanup-undec-{root_id}")
    out = _new_out()
    ctx.set_total(len(undec))
    done = 0
    for m in undec:
        ctx.check_cancelled()
        done += 1
        ctx.progress(done, message=os.path.basename(m["path"]))
        # Recycle the file, then mark its asset trashed (fingerprints—only the hash—
        # retained, so a re-import of the same corrupt bytes is excluded from merge).
        _delete_one(ctx.db, m, out, trash_reason="cleanup-undecodable")
    ctx.log(
        f"cleanup --undecodable {root['name']}: deleted {out['undecodable_deleted']} "
        f"undecodable file(s) ({out['network']} permanent on network), "
        f"{out['already_gone']} already gone — their assets are now trashed."
    )
    ctx._cleanup_outcome = {"deleted": out["undecodable_deleted"], "already_gone": out["already_gone"],
                            "summary": f"undecodable · {out['undecodable_deleted']} deleted"}


# ===========================================================================
# PERCEPTUAL ANALYZE (§6.2) — open pending run + stage the candidates
# ===========================================================================
def _analyze_perceptual(ctx: JobContext) -> None:
    db = ctx.db
    root = _resolve_library_root(ctx)
    _reject_if_held(ctx, root)
    root_id = int(root["id"])
    ctx.log(f"cleanup --trash-perceptual analyze: {root['name']} ({root['path']})")

    trash.refresh_trash(ctx)
    exact = _exact_match_instances(db, root_id)
    cands = _perceptual_candidates(ctx, root_id)
    if not exact and not cands:
        ctx.log("no trashed content found in this folder (exact or perceptual) — nothing to clean.")
        return

    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
            "VALUES (?, ?, 'pending', 1, 'staged', ?)",
            (root_id, RUN_TYPE, now_iso()),
        )
        run_id = int(cur.lastrowid)
    audit_dir = review.audit_run_dir(RUN_TYPE, root["name"], run_id)

    # Exact matches: recorded in the plan (deleted at --confirm), NOT staged.
    exact_actions = [
        {"kind": "exact", "reason": "cleanup-exact", "default_action": "delete",
         "asset_id": m["asset_id"], "instance_id": m["instance_id"], "path": m["path"],
         "matched_trashed_asset_id": m["asset_id"], "distance": None,
         "quality": None, "low_confidence": False, "shortcut_name": None}
        for m in exact
    ]
    staged, skipped = _materialize(ctx, root["path"], run_id, cands)

    with db.transaction() as conn:
        for a in exact_actions:
            _insert_action(conn, run_id, a)

    review.write_audit(
        audit_dir, "proposed.json",
        _proposed_json(ctx, root, run_id, exact_actions, staged, skipped),
    )
    # Outcome carries `review_status='pending'` (mirrors dedup analyze, §8 B) so the
    # M6 result card / root-detail Review box detect the awaiting-review state and
    # surface the [o]/[g]/[k] actions — otherwise a paused cleanup looked "done".
    ctx._cleanup_outcome = {
        "review_status": "pending", "stage": 1, "run_id": run_id,
        "to_delete_exact": len(exact_actions),
        "groups": len(staged), "members": len(staged),
        "summary": f"{len(exact_actions)} exact + {len(staged)} perceptual staged for review",
    }
    _report_analyze(ctx, root, len(exact_actions), len(staged), skipped)


# ===========================================================================
# CONFIRM (§6.2) — apply exact + still-staged perceptual deletions together
# ===========================================================================
def _confirm(ctx: JobContext) -> None:
    db = ctx.db
    root = _resolve_library_root(ctx)
    root_id = int(root["id"])
    run = db.query_one(
        "SELECT * FROM review_runs WHERE root_id=? AND run_type=? AND status='pending'",
        (root_id, RUN_TYPE),
    )
    if run is None:
        raise ValueError(
            f"nothing to confirm for {root['name']!r}; run `cleanup <folder> --trash-perceptual` first."
        )
    run_id = int(run["id"])
    audit_dir = review.audit_run_dir(RUN_TYPE, root["name"], run_id)
    actions = [dict(r) for r in db.query(
        "SELECT * FROM review_actions WHERE run_id=? ORDER BY id", (run_id,))]
    perceptual = [a for a in actions if a["kind"] == "perceptual"]
    exact = [a for a in actions if a["kind"] == "exact"]
    stage_dir = review.staging_folder(root["path"], review.PERCEPTUAL_TRASH)

    # Phase-5 guard: if perceptual candidates were staged, the folder must still
    # exist — never read "folder gone" as "delete everything" (delete-default).
    if perceptual and not review.path_exists(stage_dir):
        raise ValueError(
            f"{review.PERCEPTUAL_TRASH} staging folder is missing — aborting (did you delete it?)."
        )

    # delete-default: a still-present shortcut → delete; removed/renamed → spare (§6.2).
    perceptual_del = [a for a in perceptual
                      if review.path_exists(os.path.join(stage_dir, a["shortcut_name"]))]
    intended = exact + perceptual_del

    db.backup_labeled(f"precleanup-run{run_id}")
    out = _new_out()
    ctx.set_total(len(intended))
    done = 0
    for a in exact:
        ctx.check_cancelled()
        done += 1
        ctx.progress(done, message=os.path.basename(a["path"]))
        _delete_one(db, a, out, trash_reason=None)  # asset already trashed
    for a in perceptual_del:
        ctx.check_cancelled()
        done += 1
        ctx.progress(done, message=os.path.basename(a["path"]))
        _delete_one(db, a, out, trash_reason="cleanup-perceptual")

    review.write_audit(audit_dir, "applied.json",
                       _applied_json(root, run_id, actions, perceptual_del, out, cancelled=False))
    review.remove_tree(stage_dir)
    with db.transaction() as conn:
        conn.execute("UPDATE review_runs SET status='completed', confirmed_at=? WHERE id=?",
                     (now_iso(), run_id))
    n_spared = len(perceptual) - len(perceptual_del)
    ctx.log(
        f"cleanup --trash-perceptual confirmed for {root['name']}: {out['exact_deleted']} exact + "
        f"{out['perceptual_deleted']} perceptual file(s) deleted "
        f"({out['network']} permanent on network); {n_spared} perceptual spared, "
        f"{out['already_gone']} already gone."
    )


# ===========================================================================
# CANCEL (§6.2) — discard the pending perceptual run, delete nothing
# ===========================================================================
def _cancel(ctx: JobContext) -> None:
    db = ctx.db
    root = _resolve_library_root(ctx)
    run = db.query_one(
        "SELECT * FROM review_runs WHERE root_id=? AND run_type=? AND status='pending'",
        (root["id"], RUN_TYPE),
    )
    if run is None:
        raise ValueError(f"nothing to cancel for {root['name']!r}; no pending cleanup run.")
    run_id = int(run["id"])
    actions = [dict(r) for r in db.query(
        "SELECT * FROM review_actions WHERE run_id=? ORDER BY id", (run_id,))]
    audit_dir = review.audit_run_dir(RUN_TYPE, root["name"], run_id)
    review.write_audit(audit_dir, "applied.json",
                       _applied_json(root, run_id, actions, [], None, cancelled=True))
    review.remove_tree(review.staging_folder(root["path"], review.PERCEPTUAL_TRASH))
    with db.transaction() as conn:
        conn.execute("UPDATE review_runs SET status='cancelled', confirmed_at=? WHERE id=?",
                     (now_iso(), run_id))
    ctx.log(f"cleanup cancelled for {root['name']}: staging discarded, nothing deleted.")


# ---------------------------------------------------------------------------
# staging (perceptual candidates only — delete-default; §6.2 step 4)
# ---------------------------------------------------------------------------
def _materialize(ctx, root_path, run_id, candidates):
    """Stat-before-create ``.lnk`` for each perceptual candidate; persist rows.

    Returns ``(staged, skipped)``. A vanished target is skipped + lazily forgotten
    (its active asset at zero instances is forgotten — a plain delete, §6). Only a
    shortcut actually on disk is persisted: in this **delete-default** folder confirm
    reads a *present* shortcut as "delete", so an unwritten shortcut simply means the
    file isn't offered — safe (it re-surfaces on a later cleanup), never a silent delete.
    """
    db = ctx.db
    stage_dir = review.staging_folder(root_path, review.PERCEPTUAL_TRASH)
    resolved: list[dict] = []
    skipped = 0
    ctx.set_total(len(candidates))
    done = 0
    for c in candidates:
        ctx.check_cancelled()
        done += 1
        ctx.progress(done, message=c["shortcut_name"])
        if not review.path_exists(c["path"]):
            with db.transaction() as conn:
                _delete_instance(conn, c["instance_id"])
                _forget_if_orphaned(conn, c["asset_id"])
            skipped += 1
            continue
        resolved.append(c)

    if not resolved:
        return [], skipped

    review.ensure_dir(stage_dir)
    staged: list[dict] = []
    for c in resolved:
        lnk = os.path.join(stage_dir, c["shortcut_name"])
        try:
            shortcuts.create_shortcut(lnk, c["path"])
            staged.append(c)
        except Exception as exc:  # noqa: BLE001 - a shortcut we can't write is NOT persisted
            log.warning("could not stage %s -> %s: %s", c["shortcut_name"], c["path"], exc)

    with db.transaction() as conn:
        for c in staged:
            _insert_action(conn, run_id, {
                "kind": "perceptual", "reason": "cleanup-perceptual", "default_action": "delete",
                "asset_id": c["asset_id"], "instance_id": c["instance_id"], "path": c["path"],
                "matched_trashed_asset_id": c["matched_trashed_asset_id"], "distance": c["distance"],
                "quality": c["quality"], "low_confidence": c["low_confidence"],
                "shortcut_name": c["shortcut_name"],
            })
    _write_manifest(stage_dir, staged)
    return staged, skipped


def _insert_action(conn, run_id: int, a: dict) -> None:
    """Persist one cleanup review_action (single-stage → ``stage=1``)."""
    conn.execute(
        "INSERT INTO review_actions(run_id, stage, folder, kind, reason, default_action, "
        "asset_id, instance_id, path, survivor_instance_id, group_no, member_no, "
        "is_external, matched_trashed_asset_id, distance, shortcut_name) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?, ?, ?)",
        (run_id, review.PERCEPTUAL_TRASH, a["kind"], a["reason"], a["default_action"],
         a["asset_id"], a["instance_id"], a["path"], a["matched_trashed_asset_id"],
         a["distance"], a["shortcut_name"]),
    )


def _write_manifest(stage_dir, staged) -> None:
    if not staged:
        return
    with open(os.path.join(stage_dir, "manifest.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["shortcut", "target_path", "asset_id", "matched_trashed_asset_id",
                    "distance", "quality", "low_confidence"])
        for a in staged:
            w.writerow([a["shortcut_name"], a["path"], a["asset_id"],
                        a["matched_trashed_asset_id"],
                        a["distance"] if a["distance"] is not None else "",
                        a["quality"] if a["quality"] is not None else "",
                        1 if a["low_confidence"] else 0])


# ---------------------------------------------------------------------------
# apply one deletion (lazy liveness gate, §6.2 step 6 / §8 B Phase 6)
# ---------------------------------------------------------------------------
def _new_out() -> dict:
    return {"exact_deleted": 0, "perceptual_deleted": 0, "undecodable_deleted": 0, "deleted": 0,
            "already_gone": 0, "network": 0, "dispositions": []}


# Which `out` counter each trash_reason increments (§6.2/§9.1).
_REASON_COUNTER = {
    None: "exact_deleted",                          # exact-trash: asset already trashed
    "cleanup-perceptual": "perceptual_deleted",     # confirmed near-dup of trashed content
    "cleanup-undecodable": "undecodable_deleted",   # undecodable file culled
}


def _delete_one(db, action: dict, out: dict, *, trash_reason: str | None) -> None:
    """Recycle one matched file under the lazy-liveness gate, then update the DB.

    ``trash_reason`` controls the asset's fate after its instance is deleted:
    - **None (exact-trash)** → the asset is *already* ``trashed`` (that's how it
      matched); just drop the instance row. Never forget (trashed fingerprints kept).
    - **a reason string** (``cleanup-perceptual`` / ``cleanup-undecodable``) → the user
      confirmed this content is junk; drop the instance and, if the asset now has zero
      instances, flip it to ``trashed`` with that reason (fingerprints retained) so a
      future merge excludes a re-import. If the file was *already gone* on disk, a
      reason still means "forget an orphaned active asset" (a plain delete, §6) — we
      don't resurrect it as trashed from a stale row.
    """
    marks_trash = trash_reason is not None
    path = action["path"]

    def _gone_cleanup(conn):
        _delete_instance(conn, action["instance_id"])
        if marks_trash:
            _forget_if_orphaned(conn, action["asset_id"])

    if not review.path_exists(path):
        with db.transaction() as conn:
            _gone_cleanup(conn)
        out["already_gone"] += 1
        out["dispositions"].append({"path": path, "disposition": "already-gone"})
        return
    is_net = fsutil.is_network_path(path)
    try:
        shortcuts.recycle(path)
    except FileNotFoundError:
        with db.transaction() as conn:
            _gone_cleanup(conn)
        out["already_gone"] += 1
        out["dispositions"].append({"path": path, "disposition": "already-gone"})
        return
    except Exception as exc:  # noqa: BLE001 - a delete that fails is reported, not fatal
        log.warning("could not recycle %s: %s", path, exc)
        out["dispositions"].append({"path": path, "disposition": f"error: {exc}"})
        return
    with db.transaction() as conn:
        _delete_instance(conn, action["instance_id"])
        if marks_trash:
            n = conn.execute("SELECT COUNT(*) c FROM file_instances WHERE asset_id=?",
                             (action["asset_id"],)).fetchone()["c"]
            if n == 0:
                conn.execute(
                    "UPDATE assets SET status='trashed', trashed_at=?, trash_reason=? WHERE id=?",
                    (now_iso(), trash_reason, action["asset_id"]),
                )
        out[_REASON_COUNTER[trash_reason]] += 1
    out["deleted"] += 1
    if is_net:
        out["network"] += 1
    out["dispositions"].append({"path": path, "disposition": "deleted",
                                "recycle": "permanent" if is_net else "recycle-bin"})


# ---------------------------------------------------------------------------
# audit JSON + reporting (§8.1, §10)
# ---------------------------------------------------------------------------

def _proposed_json(ctx, root, run_id, exact_actions, staged, skipped) -> dict:
    cfg = ctx.config
    def _a(a, kind):
        return {"kind": kind, "path": a["path"], "asset_id": a["asset_id"],
                "matched_trashed_asset_id": a.get("matched_trashed_asset_id"),
                "distance": a.get("distance"), "quality": a.get("quality"),
                "low_confidence": a.get("low_confidence"), "shortcut_name": a.get("shortcut_name")}
    return {
        "run_type": RUN_TYPE, "run_id": run_id, "root": root["name"], "root_path": root["path"],
        "created_at": now_iso(),
        "thresholds": {"t_photo_edit": cfg.match.t_photo_edit,
                       "t_match_video": cfg.match.t_match_video,
                       "low_quality_hint": cfg.review.low_quality_hint,
                       "video": {"sample_frames": cfg.video.sample_frames,
                                 "frame_match_fraction": cfg.video.frame_match_fraction,
                                 "min_frame_quality": cfg.video.min_frame_quality,
                                 "min_comparable_frames": cfg.video.min_comparable_frames,
                                 "duration_tol_s": cfg.video.duration_tol_s,
                                 "duration_tol_pct": cfg.video.duration_tol_pct}},
        "exact_matches": [_a(a, "exact") for a in exact_actions],
        "perceptual_candidates": [_a(a, "perceptual") for a in staged],
        "skipped_at_staging": skipped,
    }


def _applied_json(root, run_id, actions, perceptual_del, out, *, cancelled: bool) -> dict:
    del_paths = {a["path"] for a in perceptual_del}
    disp = {}
    if out:
        for d in out["dispositions"]:
            disp[d["path"]] = d["disposition"]
    result = []
    for a in actions:
        if cancelled:
            state = "cancelled"
        elif a["kind"] == "exact":
            state = disp.get(a["path"], "deleted")
        elif a["path"] in del_paths:
            state = disp.get(a["path"], "deleted")
        else:
            state = "spared"
        result.append({"path": a["path"], "asset_id": a["asset_id"], "kind": a["kind"],
                       "reason": a["reason"], "shortcut_name": a["shortcut_name"], "state": state})
    return {
        "run_type": RUN_TYPE, "run_id": run_id, "confirmed_at": now_iso(),
        "cancelled": cancelled, "totals": out if out else {}, "actions": result,
    }


def _report_analyze(ctx, root, n_exact, n_staged, skipped) -> None:
    parent = review.staging_parent(root["path"])
    ctx.log(
        f"cleanup --trash-perceptual staged for {root['name']}: {n_exact} exact-trash match(es) "
        f"(will delete on confirm), {n_staged} perceptual candidate(s) in "
        f"{os.path.join(parent, review.PERCEPTUAL_TRASH)}"
    )
    ctx.log("  delete-default — a staged shortcut WILL be deleted; remove it to SPARE that file.")
    if skipped:
        ctx.log(f"  {skipped} candidate(s) skipped at staging (already gone).")
    ctx.log(f"review in Explorer, then: `packrat cleanup {root['name']} --confirm` (or --cancel).")


register_job(
    JobSpec(
        type="cleanup",
        handler=_run_cleanup,
        # owned_root drives the DEQUEUE gate (§3): a cleanup that TOUCHES the root —
        # analyze, preview, one-shot apply, dry-run — must not run while another op
        # holds it, so it declares the root and the queue holds it in the backlog
        # until the holder clears (§6.2). Only confirm/cancel own None: they act on
        # this run's OWN pending review_run, so gating them on it would deadlock
        # (the gate would see their own holder). `_reject_if_held` stays as
        # gate-backed defense (with the single-worker slot it can no longer fire).
        owned_root=lambda p: None if (p.get("confirm") or p.get("cancel")) else p.get("root_id"),
    )
)
