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

from .. import fsutil, matcher, review, shortcuts
from ..util import now_iso
from . import _guards
from ._dbops import delete_instance as _delete_instance
from ._dbops import forget_if_orphaned as _forget_if_orphaned
# Keep-lead ranking (§8 B) lives in dedup_rank; re-exported names keep the call sites
# (and tests/test_video_lead.py, which drives dedup._pick_lead/_log_band/…) unchanged.
from .dedup_rank import (  # noqa: F401
    _PATH_TIEBREAK,
    _PHOTO_LEAD_LEVELS,
    _PREFERENCE_TIEBREAK,
    _VIDEO_LEAD_LEVELS,
    _effective_bitrate,
    _group_lead_and_level,
    _log_band,
    _photo_format_rank,
    _pick_lead,
    ordered_lead_levels,
)
from .context import JobContext
from .registry import JobSpec, register_job

log = logging.getLogger("packrat.jobs.dedup")

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
#: Marker embedded in a stage-2 keep-lead's shortcut name (§8 B step 9). Both the
#: staging code and `--keep-suggested` confirm key off this, so keep them in sync.
_SUGGESTED_MARK = "_suggested"

#: Text width the stage-2 stats block is laid out to in the CLI staging log. The daemon
#: streams logs to whatever client is attached (no known terminal size), so it uses the
#: TUI reference frame's text width rather than reflowing to a live terminal.
_CLI_STATS_WIDTH = 92


# ---------------------------------------------------------------------------
# lazy DB cleanup (a plain delete is not trash, §4/§6) — see jobs._dbops.
# ---------------------------------------------------------------------------
# job dispatch
# ---------------------------------------------------------------------------
def _run_dedup(ctx: JobContext) -> None:
    params = ctx.params
    if params.get("confirm"):
        _confirm(ctx)
        action = "confirm"
    elif params.get("cancel"):
        _cancel(ctx)
        action = "cancel"
    elif params.get("dry_run"):
        _dry_run(ctx)
        action = "dry-run"
    else:
        _analyze(ctx)
        action = "analyze"
    _set_dedup_result(ctx, action)


def _set_dedup_result(ctx: JobContext, action: str) -> None:
    """Uniform outcome (§4 result_json) derived from the run's durable state.

    Read AFTER the mode ran, so it reflects committed state: a pending run's current
    stage + count summary (analyze/staged, or confirm that advanced to a next stage),
    or the terminal disposition (completed/cancelled). Best-effort — never raises.
    """
    root_id = ctx.params.get("root_id")
    run = ctx.db.query_one(
        "SELECT id, status, stage, deleted_count FROM review_runs "
        "WHERE root_id=? AND run_type='dedup' ORDER BY id DESC LIMIT 1", (root_id,),
    )
    result = {"op": "dedup", "action": action}
    if run is not None:
        rows = ctx.db.query(
            "SELECT kind, group_no FROM review_actions WHERE run_id=? AND stage=?",
            (int(run["id"]), run["stage"]),
        )
        exact = sum(1 for r in rows if r["kind"] == "exact")
        groups = {r["group_no"] for r in rows if r["kind"] == "perceptual" and r["group_no"] is not None}
        members = sum(1 for r in rows if r["kind"] == "perceptual")
        result.update({"review_status": run["status"], "stage": run["stage"],
                       "run_id": int(run["id"]),
                       "to_delete_exact": exact, "groups": len(groups), "members": members})
        # A confirm records the number of files it recycled into result_json.deleted;
        # the lifetime-deduped metric SUMS that across every completed dedup job (§12).
        # `deleted_count` is an "applied-but-not-yet-reported" accumulator on the run:
        # each stage's apply bumps it (§8 B Phase 7), and here — when a confirm job lands
        # its result — we DRAIN it (report the value, then reset to 0 durably). This
        # (a) never double-counts across the per-stage confirm jobs of one auto-advancing
        # run, and (b) lets a crash-resumed confirm (which skipped the apply block) still
        # credit the deletions its crashed predecessor applied but never reported.
        if action == "confirm":
            result["deleted"] = int(run["deleted_count"] or 0)
            if result["deleted"]:
                ctx.db.execute(
                    "UPDATE review_runs SET deleted_count=0 WHERE id=?", (int(run["id"]),)
                )
        # The count phrase for run["stage"] (the CURRENT cursor): stage 1 has only exact
        # rows, stages 2/3 only near-dup groups — so show the one that stage actually has,
        # never a "0 exact" for a stage that structurally can't hold exact dups (§8 B).
        cur_counts = (f"{exact} exact" if run["stage"] == STAGE_EXACT
                      else f"{len(groups)} grp/{members} mbr")
        if run["status"] == "pending":
            if action == "confirm":
                # A confirm APPLIES its stage, then auto-advances the cursor to the next
                # non-empty stage and stages it — so run["stage"] here is the stage the
                # run ADVANCED TO, not the one this job acted on. Report both, keyed off
                # the applied stage recorded before the advance, so a stage-2
                # keep-suggested confirm never reads as "staged stage 3" (§8 B).
                applied = getattr(ctx, "_dedup_confirmed_stage", run["stage"])
                result["confirmed_stage"] = applied
                result["summary"] = (
                    f"{action}: applied stage {applied} ({result['deleted']} deleted) · "
                    f"advanced to stage {run['stage']} · {cur_counts}")
            else:
                result["summary"] = (f"{action}: staged stage {run['stage']} · {cur_counts}")
        elif run["status"] == "completed" and action == "analyze":
            # An analyze that completed immediately = already clean (no stages to review).
            result["summary"] = f"{action}: already clean (nothing to review)"
        elif run["status"] == "completed" and action == "confirm":
            # The confirm applied the LAST non-empty stage → run finished. Report the
            # stage it applied + its deleted total, not a bare "run completed".
            applied = getattr(ctx, "_dedup_confirmed_stage", run["stage"])
            result["confirmed_stage"] = applied
            result["summary"] = (
                f"{action}: applied stage {applied} ({result['deleted']} deleted) · "
                f"run completed")
        else:
            result["summary"] = f"{action}: run {run['status']}"
    else:
        # Defensive: no run row at all (shouldn't happen now the already-clean path
        # records a completed run, but keep a safe fallback).
        result["summary"] = f"{action}: nothing to review (already clean)"
    ctx.set_result(result)


def _resolve_library_root(ctx: JobContext) -> dict:
    return _guards.resolve_library_root(ctx, "dedup")


# ===========================================================================
# ANALYZE (§8 B Phases 0–4) — open the run and stage the first non-empty stage
# ===========================================================================
def _analyze(ctx: JobContext) -> None:
    db = ctx.db
    root = _resolve_library_root(ctx)
    root_id, root_path = int(root["id"]), root["path"]
    prefer_internal = bool(ctx.params.get("prefer_internal"))
    ctx.log(f"dedup analyze: {root['name']} ({root_path})"
            + ("  [--prefer-internal]" if prefer_internal else ""))
    if root["last_full_scan_at"] is None:
        ctx.log("note: this root has never had a `scan --full`; run `scan` first for current liveness.")

    # Compute stage 1 up front; if the ENTIRE run would be empty (no stage has any
    # candidate) auto-complete without leaving a dangling pending run (§8 B Phase 0).
    stage1 = _plan_stage(ctx, root_id, root_path, STAGE_EXACT, prefer_internal=prefer_internal)
    probe = _first_nonempty_stage(ctx, root_id, root_path, start=STAGE_EXACT,
                                  precomputed={STAGE_EXACT: stage1}, prefer_internal=prefer_internal)
    if probe is None:
        # "Already clean" — nothing to review, so the folder IS deduped as of now.
        # Record a completed dedup run (no pending row, no staging, no review_actions)
        # so the last-successful-dedup timestamp (§11 "deduped <age>") is set. A run that
        # went through zero non-empty stages is as "fully reviewed" as one confirmed
        # through all of them — both leave the folder with no actionable duplicates.
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, "
                "created_at, confirmed_at) VALUES (?, 'dedup', 'completed', 1, 'applied', ?, ?)",
                (root_id, now_iso(), now_iso()),
            )
        ctx.log("already clean: no exact duplicates or near-dup groups to review.")
        return

    # Open the run (owns the root until confirmed/cancelled). prefer_internal is stored
    # here ONCE and read from the run row by every later --confirm — the policy is locked
    # for the whole 3-stage sequence, since a bare confirm stages stage 2 and must apply
    # the same preference stage 1 used (§8 B; see the run-scoped design note).
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO review_runs(root_id, run_type, status, stage, stage_phase, "
            "prefer_internal, created_at) VALUES (?, 'dedup', 'pending', ?, 'staged', ?, ?)",
            (root_id, probe["stage"], 1 if prefer_internal else 0, now_iso()),
        )
        run_id = int(cur.lastrowid)
    audit_dir = review.audit_run_dir("dedup", root["name"], run_id)

    _stage_and_pause(ctx, root, run_id, audit_dir, probe, prefer_internal=prefer_internal)


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
    # prefer_internal is a RUN-WIDE policy fixed at analyze — read it from the run row,
    # NOT this confirm's params (a bare --confirm stages stage 2 and must apply the same
    # preference stage 1 used). A --prefer-internal on the confirm command that CONFLICTS
    # with the run's stored value is rejected: the run is already partly applied under one
    # policy, so it can't flip mid-sequence (§8 B run-scoped design).
    prefer_internal = bool(run["prefer_internal"])
    if ctx.params.get("prefer_internal") and not prefer_internal:
        raise ValueError(
            f"this run opened WITHOUT --prefer-internal; the preference is fixed when the "
            f"run opens and cannot change mid-sequence. `packrat dedup {root['name']} "
            f"--cancel` and re-run with --prefer-internal to change it."
        )
    # The stage this confirm APPLIES — stash it for the result summary, since a
    # successful confirm auto-advances the run cursor to the NEXT non-empty stage
    # (below), so run["stage"] read afterward is no longer the stage we acted on.
    ctx._dedup_confirmed_stage = stage
    audit_dir = review.audit_run_dir("dedup", root["name"], run_id)

    keep_suggested = bool(ctx.params.get("keep_suggested"))
    if keep_suggested and stage != STAGE_RECOMPRESS:
        raise ValueError(
            f"--keep-suggested applies only to stage 2 (recompression); this run is on "
            f"stage {stage} ({_STAGE_LABEL[stage]}), which has no suggested leads. "
            f"Confirm it normally: `packrat dedup {root['name']} --confirm`."
        )

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
        if keep_suggested:
            intended = _keep_suggested_intended(ctx, actions)
        else:
            intended = [a for a in actions if _intends_delete(a, stage, stage_dir)]
        db.backup_labeled(f"prededup-run{run_id}-stage{stage}")
        outcomes = _apply_stage(ctx, stage, intended)
        review.write_audit(
            audit_dir, f"applied_stage{stage}.json",
            _applied_json(root, run_id, stage, actions, intended, outcomes, cancelled=False),
        )
        review.remove_tree(stage_dir)
        stage_deleted = outcomes["exact_deleted"] + outcomes["perceptual_deleted"]
        # Commit the apply marker AND accumulate the recycled-file total onto the run
        # in ONE transaction. Persisting the count durably (not just on the ctx) is what
        # lets a crash-resumed --confirm — which skips this apply block entirely (the
        # `stage_phase == 'applied'` guard above) — still report the right deleted total
        # into the lifetime-deduped metric (§8 B Phase 7). Otherwise the resumed run
        # read 0 and the metric silently undercounted every crash-interrupted confirm.
        with db.transaction() as conn:
            conn.execute(
                "UPDATE review_runs SET stage_phase='applied', "
                "deleted_count = deleted_count + ? WHERE id=?",
                (stage_deleted, run_id),
            )
        if keep_suggested:
            ctx.log("  (--keep-suggested: kept each group's suggested lead, ignored shortcut edits.)")
        _report_stage_confirm(ctx, stage, outcomes)

    # Advance to the next non-empty stage, or finalize after the last. The run's stored
    # prefer_internal carries into staging the next stage (e.g. stage 1 → stage 2's leads).
    nxt = _first_nonempty_stage(ctx, root_id, root_path, start=stage + 1,
                                prefer_internal=prefer_internal)
    if nxt is None:
        _finalize_completed(ctx, root, run_id)
        return
    _stage_and_pause(ctx, root, run_id, audit_dir, nxt, advancing=True,
                     prefer_internal=prefer_internal)


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
    prefer_internal = bool(ctx.params.get("prefer_internal"))
    ctx.log(f"dedup dry-run: {root['name']} ({root_path})"
            + ("  [--prefer-internal]" if prefer_internal else ""))
    for stage in (STAGE_EXACT, STAGE_RECOMPRESS, STAGE_EDIT):
        plan = _plan_stage(ctx, root_id, root_path, stage, prefer_internal=prefer_internal)
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
def _plan_stage(ctx: JobContext, root_id: int, root_path: str, stage: int,
                *, prefer_internal: bool = False) -> dict:
    """Return ``{stage, actions, n_groups, lead_levels, edges}`` for one stage (no I/O)."""
    if stage == STAGE_EXACT:
        actions = _plan_exact(ctx, root_id, prefer_internal=prefer_internal)
        return {"stage": stage, "actions": actions, "n_groups": 0, "lead_levels": {}, "edges": []}
    return _plan_perceptual(ctx, root_id, stage, prefer_internal=prefer_internal)


def _plan_exact(ctx: JobContext, root_id: int, *, prefer_internal: bool = False) -> list[dict]:
    """Stage 1: exact-duplicate resolution among the target root's active assets (§8 B Phase 2).

    When an asset has copies both inside the target root and in another root, the
    default keeps the EXTERNAL copy and deletes the internal ones (``exact-external``).
    Under ``prefer_internal`` the roles flip: an internal copy survives and the external
    copies are deleted (``exact-internal-preferred``, marked ``is_external`` so the
    network-permanent-delete warning still fires, §10 / [[review-network-count]]).
    """
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

    def _mtime_path_key(i):
        return (i["mtime"] if i["mtime"] is not None else 0.0, os.path.normcase(i["path"]))

    actions: list[dict] = []
    seq = 0
    for asset_id, insts in by_asset.items():
        internal = [i for i in insts if i["root_id"] == root_id]
        external = [i for i in insts if i["root_id"] != root_id]
        if external and prefer_internal:
            # --prefer-internal: keep an internal copy (oldest, then path), delete every
            # other copy — the external ones AND any surplus internal duplicates.
            survivor = sorted(internal, key=_mtime_path_key)[0]
            for inst in insts:
                if inst["fid"] == survivor["fid"]:
                    continue
                seq += 1
                actions.append(_exact_action(asset_id, inst, survivor, "exact-internal-preferred",
                                             seq, is_external=inst["root_id"] != root_id))
        elif external:
            survivor = sorted(external, key=lambda i: os.path.normcase(i["path"]))[0]
            for inst in internal:
                seq += 1
                actions.append(_exact_action(asset_id, inst, survivor, "exact-external", seq))
        elif len(internal) >= 2:
            kept = sorted(internal, key=_mtime_path_key)[0]
            for inst in internal:
                if inst["fid"] == kept["fid"]:
                    continue
                seq += 1
                actions.append(_exact_action(asset_id, inst, kept, "exact-internal", seq))
        # else: lone survivor, nothing to delete.
    return actions


def _exact_action(asset_id: int, inst: dict, survivor: dict, reason: str, seq: int,
                  *, is_external: bool = False) -> dict:
    return {
        "stage": STAGE_EXACT, "folder": review.EXACT_DUP, "kind": "exact", "reason": reason,
        "default_action": "delete", "asset_id": asset_id, "instance_id": inst["fid"],
        "path": inst["path"], "survivor_instance_id": survivor["fid"],
        "survivor_path": survivor["path"], "group_no": None, "member_no": None,
        # is_external marks that the file being DELETED lives outside the target root —
        # true only under --prefer-internal (default stage 1 always deletes internal
        # copies). Drives the network-permanent-delete warning (§10).
        "is_external": is_external, "is_lead": False, "lead_reason": None,
        "distance": None, "quality": None, "low_confidence": False,
        "shortcut_name": f"{seq:03d}.lnk",
    }


def _plan_perceptual(ctx: JobContext, root_id: int, stage: int,
                     *, prefer_internal: bool = False) -> dict:
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

    actions, n_groups, lead_levels = _group_actions(ctx, db, root_id, stage, banded,
                                                     prefer_internal=prefer_internal)
    return {"stage": stage, "actions": actions, "n_groups": n_groups,
            "lead_levels": lead_levels, "edges": edges}


def _group_actions(ctx, db, root_id, stage, banded_edges, *, prefer_internal=False):
    """Build clusters from this stage's banded edges.

    Returns ``(actions, n_groups, lead_levels)`` where ``lead_levels`` is a
    ``{level_label: count}`` tally of *why* each group's keep-lead won (§8 B stage-2
    lead-pick stats), empty unless this stage suggests leads (stage 2 only).
    ``prefer_internal`` reaches the keep-lead so a full-key tie in a mixed group favors
    the internal copy (§8 B).
    """
    if not banded_edges:
        return [], 0, {}
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
        return [], 0, {}

    all_ids = {aid for c in clusters for aid in c}
    insts = _surviving_instances(db, all_ids, root_id)
    quals = matcher.asset_qualities(db, all_ids, min_frame_quality=ctx.config.video.min_frame_quality)
    rank = _asset_rank_fields(db, all_ids)  # pixels/size/duration/codec/media_type
    hint = ctx.config.review.low_quality_hint
    folder = _STAGE_FOLDER[stage]
    # Suggest a keep-lead only in stage 2 (recompression: members are essentially the
    # same content at differing compression, so "keep the least-compressed" is
    # meaningful — for photos AND video). Stage 3 (minor edits) is deliberately
    # unranked — the edited copy may be the one to keep (§8 B).
    suggest_lead = stage == STAGE_RECOMPRESS

    actions: list[dict] = []
    lead_levels: dict[str, int] = {}
    group_no = 0
    for comp in clusters:
        group_no += 1
        # Members with a live representative instance, in stable order.
        members = [(aid, insts[aid]) for aid in comp if insts.get(aid) is not None]
        lead_level = None
        if suggest_lead:
            lead_id, lead_level = _group_lead_and_level(
                members, rank, ctx.config, root_id=root_id, prefer_internal=prefer_internal)
            if lead_level is not None:
                lead_levels[lead_level] = lead_levels.get(lead_level, 0) + 1
        else:
            lead_id = None
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
            suffix = (_SUGGESTED_MARK if is_lead else "") + ("_external" if inst["root_id"] != root_id else "")
            r = rank.get(asset_id, {})
            actions.append({
                "stage": stage, "folder": folder, "kind": "perceptual", "reason": "perceptual",
                "default_action": "keep", "asset_id": asset_id, "instance_id": inst["fid"],
                "path": inst["path"], "survivor_instance_id": None, "survivor_path": None,
                "group_no": group_no, "member_no": member_no,
                "is_external": inst["root_id"] != root_id, "distance": near_d,
                "quality": my_q, "low_confidence": low_conf, "is_lead": is_lead,
                # Why this member was chosen lead (the ranking-key decision level);
                # only the lead carries it, others get "" in the manifest (§8 B).
                "lead_reason": lead_level if is_lead else None,
                "media_type": r.get("media_type"), "width": r.get("width"), "height": r.get("height"),
                "size": r.get("size"),
                "duration_s": r.get("duration_s"), "codec": r.get("codec"),
                "shortcut_name": f"group{group_no:04d}_{member_no:04d}{suffix}.lnk",
            })
    return actions, group_no, lead_levels


def _asset_rank_fields(db, asset_ids):
    """Load ranking fields for the keep-lead (§8 B): photo format/size + video bitrate/codec."""
    if not asset_ids:
        return {}
    ph = ",".join("?" for _ in asset_ids)
    out: dict[int, dict] = {}
    for r in db.query(
        f"SELECT id, media_type, width, height, size, duration_s, codec "
        f"FROM assets WHERE id IN ({ph})",
        tuple(asset_ids),
    ):
        out[int(r["id"])] = {"media_type": r["media_type"], "width": r["width"],
                             "height": r["height"], "size": r["size"],
                             "duration_s": r["duration_s"], "codec": r["codec"]}
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


def _first_nonempty_stage(ctx, root_id, root_path, *, start, precomputed=None,
                          prefer_internal=False):
    """Return the first stage ≥ ``start`` whose plan has actions, or ``None``.

    ``precomputed`` lets analyze reuse stage 1's already-built plan. Returned dict is
    the full ``_plan_stage`` result so the caller can stage it without recomputing.
    """
    precomputed = precomputed or {}
    for stage in range(max(start, STAGE_EXACT), STAGE_EDIT + 1):
        plan = precomputed.get(stage) or _plan_stage(
            ctx, root_id, root_path, stage, prefer_internal=prefer_internal)
        if plan["actions"]:
            return plan
    return None


# ---------------------------------------------------------------------------
# staging (materialize one stage's folder + review_actions + manifest + audit)
# ---------------------------------------------------------------------------
def _stage_and_pause(ctx, root, run_id, audit_dir, plan, *, advancing=False,
                     prefer_internal=False):
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
        nxt = _first_nonempty_stage(ctx, root_id, root_path, start=stage + 1,
                                    prefer_internal=prefer_internal)
        if nxt is None:
            _finalize_completed(ctx, root, run_id)
            return
        _stage_and_pause(ctx, root, run_id, audit_dir, nxt, advancing=True,
                         prefer_internal=prefer_internal)
        return

    _report_staged(ctx, root, run_id, stage, staged, skipped, plan, advancing=advancing)


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
        # Idempotent re-materialize: clear any rows already persisted for this
        # (run_id, stage) before inserting. The apply-then-advance crash window
        # (§8 B Phase 7) can commit a stage's review_actions, then crash before the
        # stage-cursor UPDATE — on resume this stage is re-materialized, and without
        # this DELETE the rows would DOUBLE (there is no unique index on the plan),
        # double-counting members and mis-listing the audit. DELETE-then-INSERT makes
        # the stage's plan a clean replace.
        conn.execute("DELETE FROM review_actions WHERE run_id=? AND stage=?", (run_id, stage))
        for act in staged:
            conn.execute(
                "INSERT INTO review_actions(run_id, stage, folder, kind, reason, default_action, "
                "asset_id, instance_id, path, survivor_instance_id, group_no, member_no, "
                "is_external, is_lead, lead_reason, matched_trashed_asset_id, distance, shortcut_name) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, stage, act["folder"], act["kind"], act["reason"], act["default_action"],
                 act["asset_id"], act["instance_id"], act["path"], act["survivor_instance_id"],
                 act["group_no"], act["member_no"], 1 if act["is_external"] else 0,
                 1 if act.get("is_lead") else 0, act.get("lead_reason"),
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
                        "suggested_lead", "suggested_reason", "media_type", "width", "height",
                        "size", "duration_s", "codec", "bitrate",
                        "is_external", "distance", "quality", "low_confidence"])
            for a in staged:
                w.writerow([a["shortcut_name"], a["path"], a["asset_id"], a["group_no"],
                            a["member_no"], 1 if a.get("is_lead") else 0,
                            a.get("lead_reason") or "",
                            a.get("media_type") or "",
                            a.get("width") if a.get("width") is not None else "",
                            a.get("height") if a.get("height") is not None else "",
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


def _keep_suggested_intended(ctx, actions: list[dict]) -> list[dict]:
    r"""Delete set for ``--confirm --keep-suggested`` (stage 2 only): keep ONLY each
    group's suggested lead, delete every other member — **ignoring the user's shortcut
    edits entirely** (the whole point of the flag: "trust packrat's pick").

    A group's suggested lead is the persisted action whose ``shortcut_name`` carries the
    ``_suggested`` marker (written at staging, §8 B step 9). **Safety:** if a group has
    **no** suggested lead (e.g. an all-external group, or a lead whose ``.lnk`` failed
    to stage so its row wasn't persisted), the whole group is **spared** — never delete
    every copy of an asset because packrat couldn't name a keeper. Logs each such group.
    """
    by_group: dict[int, list[dict]] = {}
    for a in actions:
        by_group.setdefault(a["group_no"], []).append(a)
    intended: list[dict] = []
    for group_no, members in by_group.items():
        leads = [a for a in members if _SUGGESTED_MARK in (a["shortcut_name"] or "")]
        if not leads:
            ctx.log(f"  keep-suggested: group {group_no:04d} has no suggested lead — "
                    f"sparing all {len(members)} member(s).")
            continue
        lead_ids = {a["id"] for a in leads}
        intended.extend(a for a in members if a["id"] not in lead_ids)
    return intended


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


def _report_staged(ctx, root, run_id, stage, staged, skipped, plan, *, advancing) -> None:
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
    _report_review_stats(ctx, stage, plan.get("actions") or [])
    if stage == STAGE_RECOMPRESS:
        ctx.log(f"  tip: `packrat dedup {root['name']} --confirm --keep-suggested` keeps only the "
                f"suggested lead per group (ignores your shortcut edits this stage).")
    ctx.log(f"review in Explorer, then: `packrat dedup {root['name']} --confirm` (or --cancel).")


def _report_review_stats(ctx, stage, actions: list[dict]) -> None:
    """Log the stage-1 / stage-2 review breakdown — the SAME text the TUI Review box shows.

    Both faces render :mod:`packrat.review_stats` line-builders over the same
    ``review_actions`` shape, so the CLI staging log and the box can't drift (§8 B).
    Silent on stage 3 (unranked minor edits) and on empty stages.
    """
    from .. import review_stats
    if not actions:
        return
    if stage == STAGE_EXACT:
        for ln in review_stats.stage1_lines(review_stats.stage1_split(actions)):
            ctx.log(ln)
    elif stage == STAGE_RECOMPRESS:
        bundle = review_stats.stage2_stats(actions, is_network=fsutil.is_network_path)
        # keep_suggested=False: the CLI prints its OWN `--confirm --keep-suggested` tip
        # (below, in _report_staged), so suppress the box's `[b]` tip here — `[b]` is a
        # TUI-only key and would duplicate the CLI tip. Width = the reference frame's text
        # width; the daemon has no client terminal size, so the log isn't reflowed to it.
        for ln in review_stats.stage2_lines(bundle, _CLI_STATS_WIDTH, keep_suggested=False):
            ctx.log(f"  {ln}")


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
                                   "low_confidence", "is_lead", "lead_reason", "media_type",
                                   "width", "height", "size", "duration_s", "codec", "shortcut_name")}
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
        # analyze OWNS the root (per-root exclusivity); confirm/cancel/dry-run act on
        # the already-owned pending run (or open nothing), so they acquire no root —
        # the global slot + the existing pending row already serialize them (§3).
        owned_root=lambda p: None if (p.get("confirm") or p.get("cancel") or p.get("dry_run"))
        else p.get("root_id"),
    )
)
