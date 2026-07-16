r"""The ``dedup`` operation (§8 B) — a stateful, three-stage review sequence.

Dedup targets **one registered folder** and works purely from the fingerprints
``scan`` stored (hashes + PDQ) plus a **lazy** liveness stat — no eager whole-pool
walk. It presents removable duplicates as Windows ``.lnk`` shortcuts **one stage at
a time** under ``<root>\_packrat_review\``, so each folder means exactly one thing:

  stage 1 ``_exact_dup_to_delete\``      byte-identical copies      default DELETE
  stage 2 ``_suspect_recompression\``    recompressions + all video default KEEP
  stage 3 ``_with_minor_edits\``         photo minor-edits/crops    default KEEP

``--confirm`` applies the current stage (to the Recycle Bin) and **auto-advances**
to the next non-empty stage; after the last it completes. One ``review_runs`` row
spans the whole sequence, carrying a ``stage`` cursor (1..3) and ``stage_phase``
(``staged`` | ``applied``) — the apply-then-advance crash marker (§8 B Phase 7).

Key simplifications vs. a two-folders-at-once design: stage 1 deletes only
*redundant instances* (never removes an asset), so by stages 2–3 every asset still
exists and can be matched perceptually **in the same run** — no edge-case-6
exclusion, no deferral. **Survivors exist only in stage 1**; stages 2–3 stage
distinct assets with no survivor concept (deleting a near-dup member never threatens
another asset's last copy).

Three review conventions: stage 1 present-shortcut = delete / remove-to-spare;
stages 2–3 present-shortcut = keep / remove-to-delete. A renamed shortcut counts as
removed (strict, §8 B Phase 5).
"""

from __future__ import annotations

import csv
import logging
import os

from .. import fsutil, matcher, paths, review, shortcuts
from ..config import RAW_EXTS
from ..ignore import ext_of
from ..util import now_iso
from .context import CancelledError, JobContext
from .registry import JobSpec, register_job

log = logging.getLogger("packrat.jobs.dedup")

#: Photo extensions that are lossless / an original master — ranked ABOVE lossy
#: siblings when picking a stage-2 keep-lead (§8 B). JPEG blocking artifacts can
#: inflate detail_score above a pristine master, so a lossless-format tier sits
#: above detail_score in the ranking key; detail_score only discriminates within a
#: tier. webp/avif/heic are usually lossy in practice → treated as lossy here.
_LOSSLESS_PHOTO_EXTS = frozenset({"png", "tif", "tiff", "bmp"}) | RAW_EXTS

# Stage identifiers (also the review_runs.stage / review_actions.stage values).
STAGE_EXACT = 1
STAGE_RECOMPRESS = 2
STAGE_EDIT = 3
_STAGE_FOLDER = {
    STAGE_EXACT: review.EXACT_DUP,
    STAGE_RECOMPRESS: review.SUSPECT_RECOMPRESSION,
    STAGE_EDIT: review.WITH_MINOR_EDITS,
}
_STAGE_DEFAULT_DELETE = {STAGE_EXACT: True, STAGE_RECOMPRESS: False, STAGE_EDIT: False}
_STAGE_LABEL = {
    STAGE_EXACT: "exact duplicates",
    STAGE_RECOMPRESS: "suspected recompressions",
    STAGE_EDIT: "minor edits",
}


# ---------------------------------------------------------------------------
# lazy DB cleanup (a plain delete is not trash, §4/§6)
# ---------------------------------------------------------------------------
def _delete_instance(conn, instance_id: int) -> None:
    conn.execute("DELETE FROM file_instances WHERE id=?", (instance_id,))


def _forget_if_orphaned(conn, asset_id: int) -> None:
    """Forget an ``active`` asset with zero instances (cascades fingerprints, §4)."""
    n = conn.execute(
        "SELECT COUNT(*) c FROM file_instances WHERE asset_id=?", (asset_id,)
    ).fetchone()["c"]
    if n:
        return
    st = conn.execute("SELECT status FROM assets WHERE id=?", (asset_id,)).fetchone()
    if st is not None and st["status"] == "active":
        conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))


# ---------------------------------------------------------------------------
# job dispatch
# ---------------------------------------------------------------------------
def _run_dedup(ctx: JobContext) -> None:
    params = ctx.params
    if params.get("confirm"):
        _confirm(ctx)
    elif params.get("cancel"):
        _cancel(ctx)
    elif params.get("dry_run"):
        _dry_run(ctx)
    else:
        _analyze(ctx)


def _resolve_library_root(ctx: JobContext) -> dict:
    row = ctx.db.query_one("SELECT * FROM roots WHERE id=?", (ctx.params.get("root_id"),))
    if row is None:
        raise ValueError(f"no such root id: {ctx.params.get('root_id')}")
    if row["kind"] != "library":
        raise ValueError(f"{row['name']!r} is a {row['kind']} root; dedup targets a library root")
    return dict(row)


# ===========================================================================
# ANALYZE (§8 B Phases 0–4) — open the run and stage the first non-empty stage
# ===========================================================================
def _analyze(ctx: JobContext) -> None:
    db = ctx.db
    root = _resolve_library_root(ctx)
    root_id, root_path = int(root["id"]), root["path"]
    ctx.log(f"dedup analyze: {root['name']} ({root_path})")
    if root["last_full_scan_at"] is None:
        ctx.log("note: this root has never had a `scan --full`; run `scan` first for current liveness.")

    # Compute stage 1 up front; if the ENTIRE run would be empty (no stage has any
    # candidate) auto-complete without leaving a dangling pending run (§8 B Phase 0).
    stage1 = _plan_stage(ctx, root_id, root_path, STAGE_EXACT)
    probe = _first_nonempty_stage(ctx, root_id, root_path, start=STAGE_EXACT, precomputed={STAGE_EXACT: stage1})
    if probe is None:
        ctx.log("already clean: no exact duplicates or near-dup groups to review.")
        return

    # Open the run (owns the root until confirmed/cancelled).
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, created_at) "
            "VALUES (?, 'dedup', 'pending', ?, 'staged', ?)",
            (root_id, probe["stage"], now_iso()),
        )
        run_id = int(cur.lastrowid)
    audit_dir = review.audit_run_dir("dedup", root["name"], run_id)

    _stage_and_pause(ctx, root, run_id, audit_dir, probe)


# ===========================================================================
# CONFIRM (§8 B Phases 5–7) — apply current stage, auto-advance
# ===========================================================================
def _confirm(ctx: JobContext) -> None:
    db = ctx.db
    root = _resolve_library_root(ctx)
    root_id, root_path = int(root["id"]), root["path"]
    run = db.query_one(
        "SELECT * FROM review_runs WHERE root_id=? AND run_type='dedup' AND status='pending'",
        (root_id,),
    )
    if run is None:
        raise ValueError(f"nothing to confirm for {root['name']!r}; run `dedup <folder>` first.")
    run_id = int(run["id"])
    stage = int(run["stage"])
    audit_dir = review.audit_run_dir("dedup", root["name"], run_id)

    # Resume the apply-then-advance crash window (§8 B Phase 7): if the current stage
    # was already applied (crash before staging the next), skip straight to advancing.
    if run["stage_phase"] != "applied":
        stage_dir = review.staging_folder(root_path, _STAGE_FOLDER[stage])
        actions = [dict(r) for r in db.query(
            "SELECT * FROM review_actions WHERE run_id=? AND stage=? ORDER BY id", (run_id, stage))]
        # Phase 5 guard: the stage folder must exist (never read "gone" as "delete all").
        if actions and not review.path_exists(stage_dir):
            raise ValueError(
                f"{_STAGE_FOLDER[stage]} staging folder is missing — aborting (did you delete it?)."
            )
        intended = [a for a in actions if _intends_delete(a, stage, stage_dir)]
        _backup_db(db, run_id, stage)
        outcomes = _apply_stage(ctx, stage, intended)
        review.write_audit(
            audit_dir, f"applied_stage{stage}.json",
            _applied_json(root, run_id, stage, actions, intended, outcomes, cancelled=False),
        )
        review.remove_tree(stage_dir)
        with db.transaction() as conn:
            conn.execute("UPDATE review_runs SET stage_phase='applied' WHERE id=?", (run_id,))
        _report_stage_confirm(ctx, stage, outcomes)

    # Advance to the next non-empty stage, or finalize after the last.
    nxt = _first_nonempty_stage(ctx, root_id, root_path, start=stage + 1)
    if nxt is None:
        _finalize_completed(ctx, root, run_id)
        return
    _stage_and_pause(ctx, root, run_id, audit_dir, nxt, advancing=True)


# ===========================================================================
# CANCEL — discard the whole run's staging, delete nothing (§8 B Phase 7)
# ===========================================================================
def _cancel(ctx: JobContext) -> None:
    db = ctx.db
    root = _resolve_library_root(ctx)
    run = db.query_one(
        "SELECT * FROM review_runs WHERE root_id=? AND run_type='dedup' AND status='pending'",
        (root["id"],),
    )
    if run is None:
        raise ValueError(f"nothing to cancel for {root['name']!r}; no pending dedup run.")
    run_id = int(run["id"])
    actions = [dict(r) for r in db.query(
        "SELECT * FROM review_actions WHERE run_id=? ORDER BY id", (run_id,))]
    audit_dir = review.audit_run_dir("dedup", root["name"], run_id)
    review.write_audit(audit_dir, "applied.json",
                       _applied_json(root, run_id, None, actions, [], None, cancelled=True))
    for name in review.DEDUP_STAGE_FOLDERS:
        review.remove_tree(review.staging_folder(root["path"], name))
    with db.transaction() as conn:
        conn.execute("UPDATE review_runs SET status='cancelled', confirmed_at=? WHERE id=?",
                     (now_iso(), run_id))
    ctx.log(f"dedup cancelled for {root['name']}: staging discarded, nothing deleted "
            f"(files removed in already-confirmed stages stay).")


# ===========================================================================
# DRY-RUN — compute all 3 stages read-only; stage/write nothing (§8 B)
# ===========================================================================
def _dry_run(ctx: JobContext) -> None:
    root = _resolve_library_root(ctx)
    root_id, root_path = int(root["id"]), root["path"]
    ctx.log(f"dedup dry-run: {root['name']} ({root_path})")
    for stage in (STAGE_EXACT, STAGE_RECOMPRESS, STAGE_EDIT):
        plan = _plan_stage(ctx, root_id, root_path, stage)
        n_members = len(plan["actions"])
        if stage == STAGE_EXACT:
            ctx.log(f"  stage 1 ({_STAGE_LABEL[stage]}): {n_members} file(s) would be staged for deletion.")
        else:
            ctx.log(f"  stage {stage} ({_STAGE_LABEL[stage]}): {plan['n_groups']} group(s), "
                    f"{n_members} member(s) would be staged for review.")
    ctx.log("dry-run: no staging folders, shortcuts, or DB rows written.")


# ---------------------------------------------------------------------------
# stage planning (pure DB + fingerprint math; no stat, no writes)
# ---------------------------------------------------------------------------
def _plan_stage(ctx: JobContext, root_id: int, root_path: str, stage: int) -> dict:
    """Return ``{stage, actions, n_groups, edges}`` for one stage (no I/O, no writes)."""
    if stage == STAGE_EXACT:
        actions = _plan_exact(ctx, root_id)
        return {"stage": stage, "actions": actions, "n_groups": 0, "edges": []}
    return _plan_perceptual(ctx, root_id, stage)


def _plan_exact(ctx: JobContext, root_id: int) -> list[dict]:
    """Stage 1: exact-duplicate resolution among the target root's active assets (§8 B Phase 2)."""
    db = ctx.db
    rows = db.query(
        "SELECT fi.id fid, fi.asset_id, fi.root_id, fi.path, fi.mtime "
        "FROM file_instances fi JOIN assets a ON a.id=fi.asset_id "
        "WHERE a.status='active' AND fi.asset_id IN "
        "  (SELECT DISTINCT asset_id FROM file_instances WHERE root_id=?)",
        (root_id,),
    )
    by_asset: dict[int, list[dict]] = {}
    for r in rows:
        by_asset.setdefault(int(r["asset_id"]), []).append(
            {"fid": int(r["fid"]), "root_id": int(r["root_id"]), "path": r["path"], "mtime": r["mtime"]}
        )

    actions: list[dict] = []
    seq = 0
    for asset_id, insts in by_asset.items():
        internal = [i for i in insts if i["root_id"] == root_id]
        external = [i for i in insts if i["root_id"] != root_id]
        if external:
            survivor = sorted(external, key=lambda i: os.path.normcase(i["path"]))[0]
            for inst in internal:
                seq += 1
                actions.append(_exact_action(asset_id, inst, survivor, "exact-external", seq))
        elif len(internal) >= 2:
            kept = sorted(internal, key=lambda i: (i["mtime"] if i["mtime"] is not None else 0.0,
                                                   os.path.normcase(i["path"])))[0]
            for inst in internal:
                if inst["fid"] == kept["fid"]:
                    continue
                seq += 1
                actions.append(_exact_action(asset_id, inst, kept, "exact-internal", seq))
        # else: lone survivor, nothing to delete.
    return actions


def _exact_action(asset_id: int, inst: dict, survivor: dict, reason: str, seq: int) -> dict:
    return {
        "stage": STAGE_EXACT, "folder": review.EXACT_DUP, "kind": "exact", "reason": reason,
        "default_action": "delete", "asset_id": asset_id, "instance_id": inst["fid"],
        "path": inst["path"], "survivor_instance_id": survivor["fid"],
        "survivor_path": survivor["path"], "group_no": None, "member_no": None,
        "is_external": False, "distance": None, "quality": None, "low_confidence": False,
        "shortcut_name": f"{seq:03d}.lnk",
    }


def _plan_perceptual(ctx: JobContext, root_id: int, stage: int) -> dict:
    """Stage 2/3: perceptual grouping, banded by PDQ distance (§8 B Phase 3).

    Runs the §5 matcher (target-root active assets vs. all active assets), then keeps
    the edges whose distance falls in this stage's band:
    - stage 2 (recompression): photo ``d ≤ t_photo_recompress`` **plus all video** edges;
    - stage 3 (minor edit): photo ``t_photo_recompress < d ≤ t_photo_edit`` (no video).
    Clusters are built from the banded edges; each member is one action. Also returns
    the full edge list so the caller can persist ``similarity_edges`` once.
    """
    db = ctx.db
    cfg = ctx.config
    # Match this root's active assets against the whole active collection.
    target_ids = {int(r["asset_id"]) for r in db.query(
        "SELECT DISTINCT fi.asset_id FROM file_instances fi JOIN assets a ON a.id=fi.asset_id "
        "WHERE fi.root_id=? AND a.status='active'", (root_id,))}
    targets = matcher.load_signatures(db, asset_ids=target_ids, statuses=("active",))
    pool = matcher.load_signatures(db, asset_ids=None, statuses=("active",))
    edges = matcher.find_matches(targets, pool, cfg)

    recompress = cfg.match.t_photo_recompress
    banded = []
    for e in edges:
        if e.media_type == "video":
            if stage == STAGE_RECOMPRESS:  # all video near-dups go to stage 2
                banded.append(e)
        elif stage == STAGE_RECOMPRESS:
            if e.distance <= recompress:
                banded.append(e)
        else:  # STAGE_EDIT — photo only, the wider band above recompress
            if e.distance > recompress:
                banded.append(e)

    actions, n_groups = _group_actions(ctx, db, root_id, stage, banded)
    return {"stage": stage, "actions": actions, "n_groups": n_groups, "edges": edges}


def _group_actions(ctx, db, root_id, stage, banded_edges):
    """Build clusters from this stage's banded edges; return ``(actions, n_groups)``."""
    if not banded_edges:
        return [], 0
    adj: dict[int, set[int]] = {}
    dist: dict[tuple[int, int], int] = {}
    for e in banded_edges:
        adj.setdefault(e.asset_a, set()).add(e.asset_b)
        adj.setdefault(e.asset_b, set()).add(e.asset_a)
        dist[(e.asset_a, e.asset_b)] = e.distance
    clusters: list[list[int]] = []
    seen: set[int] = set()
    for start in adj:
        if start in seen:
            continue
        stack, comp = [start], []
        seen.add(start)
        while stack:
            n = stack.pop()
            comp.append(n)
            for m in adj[n]:
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        if len(comp) >= 2:
            clusters.append(sorted(comp))
    if not clusters:
        return [], 0

    all_ids = {aid for c in clusters for aid in c}
    insts = _surviving_instances(db, all_ids, root_id)
    quals = _asset_qualities(db, all_ids)
    rank = _asset_rank_fields(db, all_ids)  # pixels/detail_score/size/duration/codec/media_type
    hint = ctx.config.review.low_quality_hint
    folder = _STAGE_FOLDER[stage]
    # Suggest a keep-lead only in stage 2 (recompression: members are essentially the
    # same content at differing compression, so "keep the least-compressed" is
    # meaningful — for photos AND video). Stage 3 (minor edits) is deliberately
    # unranked — the edited copy may be the one to keep (§8 B).
    suggest_lead = stage == STAGE_RECOMPRESS

    actions: list[dict] = []
    group_no = 0
    for comp in clusters:
        group_no += 1
        # Members with a live representative instance, in stable order.
        members = [(aid, insts[aid]) for aid in comp if insts.get(aid) is not None]
        lead_id = _pick_lead(members, rank, ctx.config) if suggest_lead else None
        member_no = 0
        for asset_id, inst in members:
            member_no += 1
            neighbors = adj.get(asset_id, set())
            near_d = min((dist.get((min(asset_id, m), max(asset_id, m)), 256) for m in neighbors),
                         default=None)
            my_q = quals.get(asset_id)
            neigh_q = [quals.get(m) for m in neighbors if quals.get(m) is not None]
            low_conf = (my_q is not None and my_q < hint) or any(q < hint for q in neigh_q)
            is_lead = asset_id == lead_id
            # `_suggested` marks packrat's keep recommendation; `_external` marks a file
            # in another root (deleting it reaches cross-root). Both are advisory.
            suffix = ("_suggested" if is_lead else "") + ("_external" if inst["root_id"] != root_id else "")
            r = rank.get(asset_id, {})
            actions.append({
                "stage": stage, "folder": folder, "kind": "perceptual", "reason": "perceptual",
                "default_action": "keep", "asset_id": asset_id, "instance_id": inst["fid"],
                "path": inst["path"], "survivor_instance_id": None, "survivor_path": None,
                "group_no": group_no, "member_no": member_no,
                "is_external": inst["root_id"] != root_id, "distance": near_d,
                "quality": my_q, "low_confidence": low_conf, "is_lead": is_lead,
                "media_type": r.get("media_type"), "width": r.get("width"), "height": r.get("height"),
                "detail_score": r.get("detail_score"), "size": r.get("size"),
                "duration_s": r.get("duration_s"), "codec": r.get("codec"),
                "shortcut_name": f"group{group_no:04d}_{member_no:04d}{suffix}.lnk",
            })
    return actions, group_no


def _pick_lead(members, rank, config):
    """Pick the keep-lead asset among a stage-2 group (§8 B).

    A group is homogeneous (all photo, or all video — a photo never matches a video),
    so the group's media type picks the ranking key. Both keys lead with **resolution**
    (a downscaled re-export loses outright); a member with no rank row sorts last.

    - **Photo** (best first, all DESC): pixels → lossless-format tier → `detail_score`
      BAND → file size. The lossless tier sits ABOVE `detail_score` because JPEG
      blocking artifacts can inflate `detail_score` above a pristine master, so
      `detail_score` is trusted only WITHIN a lossy/lossless tier. Within that tier
      `detail_score` is *banded* (`detail_tie_pct`): the residual-entropy measure is
      noisy in the high-quality band (a slightly-more-compressed copy can score
      higher), so near-equal scores tie and **file size** — the clean monotonic
      quality proxy at fixed resolution+format — breaks the tie. Heavy compression
      still spans bands, so `detail_score` separates it. (Symmetric with video's
      bitrate band below.)
    - **Video** (best first, all DESC): pixels → effective-bitrate BAND → codec weight.
      Effective bitrate = `size/duration_s × codec_weight` (§8 B): a more-efficient
      codec's bits are worth more, so an HEVC master beats an H.264 re-export at equal
      resolution+quality. Bitrates within `video_bitrate_tie_pct` share a log-scale
      band (a "tie"), so the codec weight then the path decide — not a coin-flip on a
      noisy diff. No `duration_s` → fall back to raw size (still ×weight).

    Final stable tiebreak (both): smallest normcase path — deterministic across runs.
    """
    def photo_key(item):
        aid, inst = item
        r = rank.get(aid, {})
        pixels = (r.get("width") or 0) * (r.get("height") or 0)
        lossless = 1 if ext_of(inst["path"]) in _LOSSLESS_PHOTO_EXTS else 0
        detail = r.get("detail_score") or 0
        band = _log_band(detail, config.match.detail_tie_pct)
        size = r.get("size") or 0
        return (pixels, lossless, band, size)

    def video_key(item):
        aid, _inst = item
        r = rank.get(aid, {})
        pixels = (r.get("width") or 0) * (r.get("height") or 0)
        weight = config.match.codec_weights.get((r.get("codec") or "").lower(), 1.0)
        eff = _effective_bitrate(r.get("size"), r.get("duration_s"), weight)
        band = _log_band(eff, config.match.video_bitrate_tie_pct)
        return (pixels, band, weight)

    # A group is homogeneous; sample any member's media type.
    is_video = any(rank.get(aid, {}).get("media_type") == "video" for aid, _ in members)
    key = video_key if is_video else photo_key

    best = None
    best_key = None
    for item in members:
        k = key(item)
        if best is None or k > best_key or (
            k == best_key and os.path.normcase(item[1]["path"]) < os.path.normcase(best[1]["path"])
        ):
            best, best_key = item, k
    return best[0] if best else None


def _effective_bitrate(size, duration_s, weight: float) -> float:
    """size/duration × codec weight (§8 B video keep-lead); raw size × weight if no duration."""
    if not size:
        return 0.0
    if duration_s and duration_s > 0:
        return (size / duration_s) * weight
    return size * weight  # no duration → raw size (still weighted); consistent within a group


def _log_band(value: float, tie_pct: float) -> int:
    """Quantize a value to a log-scale band so ~equal values tie (§8 B keep-lead).

    Two values within ``tie_pct`` percent land in the same band → the next ranking
    key decides, instead of a coin-flip on a noisy diff. Log scale so "within X%"
    means the same at any magnitude. ``value<=0`` → a sentinel low band. Shared by
    the video keep-lead (effective bitrate → codec weight breaks the tie) and the
    photo keep-lead (detail_score → file size breaks the tie).
    """
    import math

    if value <= 0 or tie_pct <= 0:
        return -1
    return round(math.log(value) / math.log(1.0 + tie_pct / 100.0))


def _asset_rank_fields(db, asset_ids):
    """Load ranking fields for the keep-lead (§8 B): photo detail + video bitrate/codec."""
    if not asset_ids:
        return {}
    ph = ",".join("?" for _ in asset_ids)
    out: dict[int, dict] = {}
    for r in db.query(
        f"SELECT id, media_type, width, height, size, detail_score, duration_s, codec "
        f"FROM assets WHERE id IN ({ph})",
        tuple(asset_ids),
    ):
        out[int(r["id"])] = {"media_type": r["media_type"], "width": r["width"],
                             "height": r["height"], "size": r["size"],
                             "detail_score": r["detail_score"], "duration_s": r["duration_s"],
                             "codec": r["codec"]}
    return out


def _surviving_instances(db, asset_ids, root_id):
    """Each asset's representative instance: prefer target-folder, else external (stable by path)."""
    if not asset_ids:
        return {}
    ph = ",".join("?" for _ in asset_ids)
    rows = db.query(
        f"SELECT id fid, asset_id, root_id, path FROM file_instances WHERE asset_id IN ({ph})",
        tuple(asset_ids),
    )
    chosen: dict[int, dict] = {}
    for r in rows:
        aid = int(r["asset_id"])
        cand = {"fid": int(r["fid"]), "root_id": int(r["root_id"]), "path": r["path"]}
        cur = chosen.get(aid)
        if cur is None:
            chosen[aid] = cand
            continue
        cur_target = cur["root_id"] == root_id
        cand_target = cand["root_id"] == root_id
        if (cand_target and not cur_target) or (
            cand_target == cur_target and os.path.normcase(cand["path"]) < os.path.normcase(cur["path"])
        ):
            chosen[aid] = cand
    return chosen


def _asset_qualities(db, asset_ids):
    """Per-asset quality scalar for the manifest hint (photo PDQ q / video min frame q)."""
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


def _first_nonempty_stage(ctx, root_id, root_path, *, start, precomputed=None):
    """Return the first stage ≥ ``start`` whose plan has actions, or ``None``.

    ``precomputed`` lets analyze reuse stage 1's already-built plan. Returned dict is
    the full ``_plan_stage`` result so the caller can stage it without recomputing.
    """
    precomputed = precomputed or {}
    for stage in range(max(start, STAGE_EXACT), STAGE_EDIT + 1):
        plan = precomputed.get(stage) or _plan_stage(ctx, root_id, root_path, stage)
        if plan["actions"]:
            return plan
    return None


# ---------------------------------------------------------------------------
# staging (materialize one stage's folder + review_actions + manifest + audit)
# ---------------------------------------------------------------------------
def _stage_and_pause(ctx, root, run_id, audit_dir, plan, *, advancing=False):
    """Materialize ``plan``'s stage, persist edges + rows, pause. Auto-advance if empty."""
    db = ctx.db
    root_id, root_path = int(root["id"]), root["path"]
    stage = plan["stage"]

    # Persist this run's near-dup edges (dedup is the writer of similarity_edges, §4).
    if plan["edges"]:
        _upsert_edges(db, plan["edges"])

    staged, skipped = _materialize(ctx, root_path, run_id, stage, plan["actions"])
    review.write_audit(audit_dir, f"proposed_stage{stage}.json",
                       _proposed_json(ctx, root, run_id, plan, skipped))

    # Move the run cursor to this stage (advancing from a prior confirm).
    with db.transaction() as conn:
        conn.execute("UPDATE review_runs SET stage=?, stage_phase='staged' WHERE id=?", (stage, run_id))

    if staged == 0:
        # Every target vanished since scan → nothing to review here; try the next stage.
        review.remove_tree(review.staging_folder(root_path, _STAGE_FOLDER[stage]))
        ctx.log(f"stage {stage} ({_STAGE_LABEL[stage]}): all {skipped} candidate(s) already gone; skipping.")
        nxt = _first_nonempty_stage(ctx, root_id, root_path, start=stage + 1)
        if nxt is None:
            _finalize_completed(ctx, root, run_id)
            return
        _stage_and_pause(ctx, root, run_id, audit_dir, nxt, advancing=True)
        return

    _report_staged(ctx, root, run_id, stage, staged, skipped, advancing=advancing)


def _materialize(ctx, root_path, run_id, stage, actions):
    """Stat-before-create shortcuts + persist review_actions for ONE stage (§8 B Phase 4).

    Returns ``(staged, skipped)``. A vanished target is skipped + lazily forgotten; a
    stage-1 target whose survivor vanished is promoted early. Only rows whose shortcut
    is actually on disk are persisted (a phantom row in a default-KEEP stage would read
    as "delete" at confirm — silent data loss).
    """
    db = ctx.db
    stage_dir = review.staging_folder(root_path, _STAGE_FOLDER[stage])

    survivor_override: dict[int, dict] = {}
    resolved: list[dict] = []
    skipped = 0
    ctx.set_total(len(actions))
    done = 0
    for act in actions:
        ctx.check_cancelled()
        done += 1
        ctx.progress(done, message=act["shortcut_name"])
        if not review.path_exists(act["path"]):
            _lazy_forget_target(db, act)
            skipped += 1
            continue
        if stage == STAGE_EXACT:
            surv = survivor_override.get(act["asset_id"])
            surv_path = surv["path"] if surv else act.get("survivor_path")
            if surv_path is None or not review.path_exists(surv_path):
                # Survivor vanished → promote THIS target to survivor; skip its shortcut.
                _lazy_forget_survivor(db, act)
                survivor_override[act["asset_id"]] = {"fid": act["instance_id"], "path": act["path"]}
                skipped += 1
                continue
        resolved.append(act)

    # Redirect surviving exact deletions at any promoted survivor.
    if stage == STAGE_EXACT:
        for act in resolved:
            ov = survivor_override.get(act["asset_id"])
            if ov is not None:
                act["survivor_instance_id"] = ov["fid"]
                act["survivor_path"] = ov["path"]

    if not resolved:
        return 0, skipped

    review.ensure_dir(stage_dir)
    staged: list[dict] = []
    for act in resolved:
        lnk = os.path.join(stage_dir, act["shortcut_name"])
        try:
            shortcuts.create_shortcut(lnk, act["path"])
            staged.append(act)
        except Exception as exc:  # noqa: BLE001 - a shortcut we can't write is NOT persisted
            # In a default-KEEP stage confirm reads an ABSENT shortcut as "delete", so a
            # persisted stage-error would silently delete an unreviewed file. Drop it →
            # the dup simply re-surfaces on a later dedup run.
            log.warning("could not stage %s -> %s: %s", act["shortcut_name"], act["path"], exc)

    with db.transaction() as conn:
        for act in staged:
            conn.execute(
                "INSERT INTO review_actions(run_id, stage, folder, kind, reason, default_action, "
                "asset_id, instance_id, path, survivor_instance_id, group_no, member_no, "
                "is_external, matched_trashed_asset_id, distance, shortcut_name) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, stage, act["folder"], act["kind"], act["reason"], act["default_action"],
                 act["asset_id"], act["instance_id"], act["path"], act["survivor_instance_id"],
                 act["group_no"], act["member_no"], 1 if act["is_external"] else 0,
                 None, act["distance"], act["shortcut_name"]),
            )
    _write_manifest(stage, stage_dir, staged)
    return len(staged), skipped


def _lazy_forget_target(db, act) -> None:
    with db.transaction() as conn:
        _delete_instance(conn, act["instance_id"])
        _forget_if_orphaned(conn, act["asset_id"])


def _lazy_forget_survivor(db, act) -> None:
    if act.get("survivor_instance_id") is None:
        return
    with db.transaction() as conn:
        _delete_instance(conn, act["survivor_instance_id"])
        _forget_if_orphaned(conn, act["asset_id"])


def _write_manifest(stage, stage_dir, staged) -> None:
    if not staged:
        return
    if stage == STAGE_EXACT:
        with open(os.path.join(stage_dir, "manifest.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["shortcut", "target_path", "asset_id", "reason", "survivor_path"])
            for a in staged:
                w.writerow([a["shortcut_name"], a["path"], a["asset_id"], a["reason"],
                            a.get("survivor_path") or ""])
    else:
        with open(os.path.join(stage_dir, "manifest.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["shortcut", "target_path", "asset_id", "group_no", "member_no",
                        "suggested_lead", "media_type", "width", "height", "detail_score",
                        "size", "duration_s", "codec", "bitrate",
                        "is_external", "distance", "quality", "low_confidence"])
            for a in staged:
                w.writerow([a["shortcut_name"], a["path"], a["asset_id"], a["group_no"],
                            a["member_no"], 1 if a.get("is_lead") else 0,
                            a.get("media_type") or "",
                            a.get("width") if a.get("width") is not None else "",
                            a.get("height") if a.get("height") is not None else "",
                            a.get("detail_score") if a.get("detail_score") is not None else "",
                            a.get("size") if a.get("size") is not None else "",
                            a.get("duration_s") if a.get("duration_s") is not None else "",
                            a.get("codec") or "",
                            _fmt_bitrate(a.get("size"), a.get("duration_s")),
                            1 if a["is_external"] else 0,
                            a["distance"] if a["distance"] is not None else "",
                            a["quality"] if a["quality"] is not None else "",
                            1 if a["low_confidence"] else 0])


def _fmt_bitrate(size, duration_s) -> str:
    """Human-readable Mb/s for the manifest (bits/s = size·8/duration); '' if unknown."""
    if not size or not duration_s or duration_s <= 0:
        return ""
    return f"{(size * 8) / duration_s / 1e6:.2f} Mb/s"


def _upsert_edges(db, edges) -> None:
    """Upsert canonical-ordered near-dup edges into ``similarity_edges`` (§4)."""
    ts = now_iso()
    with db.transaction() as conn:
        for e in edges:
            conn.execute(
                "INSERT INTO similarity_edges(asset_a, asset_b, media_type, distance, algo, created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(asset_a, asset_b) DO UPDATE SET "
                "distance=excluded.distance, algo=excluded.algo, created_at=excluded.created_at",
                (e.asset_a, e.asset_b, e.media_type, e.distance, e.algo, ts),
            )


# ---------------------------------------------------------------------------
# confirm — read the user's edits + apply one stage (§8 B Phases 5–6)
# ---------------------------------------------------------------------------
def _intends_delete(action: dict, stage: int, stage_dir: str) -> bool:
    """Strict shortcut-presence rule (§8 B Phase 5; rename == removed)."""
    present = review.path_exists(os.path.join(stage_dir, action["shortcut_name"]))
    if _STAGE_DEFAULT_DELETE[stage]:
        return present            # default-delete: present shortcut → delete
    return not present            # default-keep: absent shortcut → delete


def _apply_stage(ctx, stage, intended):
    """Recycle each intended file under the lazy liveness (+ stage-1 survivor) gate (§8 B Phase 6)."""
    db = ctx.db
    out = {"exact_deleted": 0, "perceptual_deleted": 0, "already_gone": 0,
           "survivor_vanished": 0, "external_deleted": 0, "network_deleted": 0,
           "spared": 0, "dispositions": []}
    ctx.set_total(len(intended))
    done = 0

    if stage == STAGE_EXACT:
        by_asset: dict[int, list[dict]] = {}
        for a in intended:
            by_asset.setdefault(a["asset_id"], []).append(a)
        for asset_id, group in by_asset.items():
            ctx.check_cancelled()
            survivor_path = _survivor_path(db, group[0])
            survivor_live = survivor_path is not None and review.path_exists(survivor_path)
            live_targets = [a for a in group if review.path_exists(a["path"])]
            promote = None
            if not survivor_live:
                if group[0].get("survivor_instance_id") is not None:
                    with db.transaction() as conn:
                        _delete_instance(conn, group[0]["survivor_instance_id"])
                promote = live_targets[0] if live_targets else None
                if promote is not None:
                    out["survivor_vanished"] += 1
                    out["spared"] += 1
                    out["dispositions"].append({"path": promote["path"], "disposition": "survivor-vanished-promoted"})
            for a in group:
                done += 1
                ctx.progress(done, message=os.path.basename(a["path"]))
                if promote is not None and a["id"] == promote["id"]:
                    continue
                if not review.path_exists(a["path"]):
                    _record_already_gone(db, a, out)
                    continue
                _recycle_and_delete(db, a, out, perceptual=False)
    else:
        for a in intended:
            ctx.check_cancelled()
            done += 1
            ctx.progress(done, message=os.path.basename(a["path"]))
            if not review.path_exists(a["path"]):
                _record_already_gone(db, a, out)
                continue
            _recycle_and_delete(db, a, out, perceptual=True)
    return out


def _survivor_path(db, action: dict) -> str | None:
    sid = action.get("survivor_instance_id")
    if sid is None:
        return None
    row = db.query_one("SELECT path FROM file_instances WHERE id=?", (sid,))
    return row["path"] if row else None


def _record_already_gone(db, action, out) -> None:
    with db.transaction() as conn:
        _delete_instance(conn, action["instance_id"])
        _forget_if_orphaned(conn, action["asset_id"])
    out["already_gone"] += 1
    out["dispositions"].append({"path": action["path"], "disposition": "already-gone"})


def _recycle_and_delete(db, action, out, *, perceptual: bool) -> None:
    """Move the file to the Recycle Bin, then update the DB (§8 B Phase 6 step 18c)."""
    path = action["path"]
    is_net = fsutil.is_network_path(path)
    try:
        shortcuts.recycle(path)
    except FileNotFoundError:
        _record_already_gone(db, action, out)
        return
    except Exception as exc:  # noqa: BLE001 - a delete that fails is reported, not fatal
        log.warning("could not recycle %s: %s", path, exc)
        out["dispositions"].append({"path": path, "disposition": f"error: {exc}"})
        return
    with db.transaction() as conn:
        _delete_instance(conn, action["instance_id"])
        if perceptual:
            n = conn.execute("SELECT COUNT(*) c FROM file_instances WHERE asset_id=?",
                             (action["asset_id"],)).fetchone()["c"]
            if n == 0:
                conn.execute(
                    "UPDATE assets SET status='trashed', trashed_at=?, trash_reason='dedup-perceptual' "
                    "WHERE id=?", (now_iso(), action["asset_id"]),
                )
            out["perceptual_deleted"] += 1
        else:
            out["exact_deleted"] += 1
    if action["is_external"]:
        out["external_deleted"] += 1
    if is_net:
        out["network_deleted"] += 1
    out["dispositions"].append({"path": path, "disposition": "deleted",
                                "recycle": "permanent" if is_net else "recycle-bin"})


# ---------------------------------------------------------------------------
# finalize + reporting
# ---------------------------------------------------------------------------
def _finalize_completed(ctx, root, run_id) -> None:
    db = ctx.db
    audit_dir = review.audit_run_dir("dedup", root["name"], run_id)
    actions = [dict(r) for r in db.query(
        "SELECT * FROM review_actions WHERE run_id=? ORDER BY id", (run_id,))]
    review.write_audit(audit_dir, "applied.json",
                       _applied_json(root, run_id, None, actions, [], None, cancelled=False,
                                     completed=True))
    for name in review.DEDUP_STAGE_FOLDERS:
        review.remove_tree(review.staging_folder(root["path"], name))
    with db.transaction() as conn:
        conn.execute("UPDATE review_runs SET status='completed', confirmed_at=? WHERE id=?",
                     (now_iso(), run_id))
    ctx.log(f"dedup complete for {root['name']}: all stages reviewed.")


def _report_staged(ctx, root, run_id, stage, staged, skipped, *, advancing) -> None:
    parent = review.staging_parent(root["path"])
    verb = "advanced to" if advancing else "staged"
    if stage == STAGE_EXACT:
        ctx.log(f"{verb} stage 1 ({_STAGE_LABEL[stage]}): {staged} shortcut(s) in "
                f"{os.path.join(parent, review.EXACT_DUP)}")
        ctx.log("  default DELETE — remove a shortcut to SPARE that file.")
    else:
        ctx.log(f"{verb} stage {stage} ({_STAGE_LABEL[stage]}): {staged} shortcut(s) in "
                f"{os.path.join(parent, _STAGE_FOLDER[stage])}")
        ctx.log("  default KEEP — remove a shortcut to DELETE that file.")
    if skipped:
        ctx.log(f"  {skipped} candidate(s) skipped at staging (already gone / promoted).")
    ctx.log(f"review in Explorer, then: `packrat dedup {root['name']} --confirm` (or --cancel).")


def _report_stage_confirm(ctx, stage, out) -> None:
    if stage == STAGE_EXACT:
        ctx.log(f"stage 1 confirmed: {out['exact_deleted']} exact-dup file(s) deleted "
                f"({out['external_deleted']} external, {out['network_deleted']} permanent on network); "
                f"{out['spared']} spared (survivor vanished), {out['already_gone']} already gone.")
    else:
        ctx.log(f"stage {stage} confirmed: {out['perceptual_deleted']} file(s) deleted "
                f"({out['external_deleted']} external, {out['network_deleted']} permanent on network); "
                f"{out['already_gone']} already gone.")


# ---------------------------------------------------------------------------
# backup + audit JSON (§8.1, §10)
# ---------------------------------------------------------------------------
def _backup_db(db, run_id: int, stage: int) -> str:
    ts = now_iso().replace(":", "").replace("-", "")
    dest = paths.backups_dir() / f"prededup-run{run_id}-stage{stage}-{ts}.db"
    db.backup_to(dest)
    return str(dest)


def _proposed_json(ctx, root, run_id, plan, skipped) -> dict:
    cfg = ctx.config
    stage = plan["stage"]
    return {
        "run_type": "dedup", "run_id": run_id, "root": root["name"], "root_path": root["path"],
        "stage": stage, "stage_label": _STAGE_LABEL[stage], "created_at": now_iso(),
        "thresholds": {
            "t_photo_recompress": cfg.match.t_photo_recompress, "t_photo_edit": cfg.match.t_photo_edit,
            "t_match_video": cfg.match.t_match_video, "low_quality_hint": cfg.review.low_quality_hint,
            "video_bitrate_tie_pct": cfg.match.video_bitrate_tie_pct,
            "detail_tie_pct": cfg.match.detail_tie_pct,
            "codec_weights": cfg.match.codec_weights,
            "video": {"sample_frames": cfg.video.sample_frames,
                      "frame_match_fraction": cfg.video.frame_match_fraction,
                      "min_frame_quality": cfg.video.min_frame_quality,
                      "min_comparable_frames": cfg.video.min_comparable_frames,
                      "duration_tol_s": cfg.video.duration_tol_s,
                      "duration_tol_pct": cfg.video.duration_tol_pct},
        },
        "n_groups": plan["n_groups"], "skipped_at_staging": skipped,
        "actions": [
            {k: a.get(k) for k in ("folder", "kind", "reason", "default_action", "asset_id",
                                   "instance_id", "path", "survivor_instance_id", "survivor_path",
                                   "group_no", "member_no", "is_external", "distance", "quality",
                                   "low_confidence", "is_lead", "media_type", "width", "height",
                                   "detail_score", "size", "duration_s", "codec", "shortcut_name")}
            for a in plan["actions"]
        ],
    }


def _applied_json(root, run_id, stage, actions, intended, outcomes, *, cancelled: bool,
                  completed: bool = False) -> dict:
    intended_ids = {a["id"] for a in intended}
    disp = {}
    if outcomes:
        for d in outcomes["dispositions"]:
            disp[d["path"]] = d["disposition"]
    result = []
    for a in actions:
        if cancelled:
            # Rows for already-applied stages retain their real outcome only in that
            # stage's applied_stage{N}.json; here (whole-run cancel) mark them cancelled.
            state = "cancelled"
        elif completed:
            state = "reviewed"  # final disposition lives in per-stage applied_stage{N}.json
        elif a["id"] in intended_ids:
            state = disp.get(a["path"], "deleted")
        else:
            state = "spared" if _STAGE_DEFAULT_DELETE.get(a["stage"], False) else "kept"
        result.append({"stage": a["stage"], "path": a["path"], "asset_id": a["asset_id"],
                       "folder": a["folder"], "reason": a["reason"],
                       "shortcut_name": a["shortcut_name"], "state": state})
    return {
        "run_type": "dedup", "run_id": run_id, "stage": stage,
        "confirmed_at": now_iso(), "cancelled": cancelled, "completed": completed,
        "totals": outcomes if outcomes else {}, "actions": result,
    }


register_job(
    JobSpec(
        type="dedup",
        handler=_run_dedup,
        mutating=True,
        # analyze OWNS the root (per-root exclusivity); confirm/cancel/dry-run act on
        # the already-owned pending run (or open nothing), so they acquire no root —
        # the global slot + the existing pending row already serialize them (§3).
        owned_root=lambda p: None if (p.get("confirm") or p.get("cancel") or p.get("dry_run"))
        else p.get("root_id"),
    )
)
