r"""Startup reconciliation (§3) — crash / kill / power-loss recovery.

On **every** daemon start, before serving any request, the daemon reconciles
stale state left by a dead worker. The worker slot is in-memory, so any
``running`` job row found at boot is stale by definition (a live daemon has at
most one, in *this* process, which just started).

Actions:
- **Orphaned ``running`` jobs → ``interrupted``** with ``error='daemon restarted'``.
  The daemon does **not** auto-resume/re-enqueue — the durable per-op plan is
  intact, so the user re-runs the command (§3). This avoids a crash-loop and
  never resumes a destructive apply unattended.
- **Analyze rollback**: a dedup/cleanup *analyze* interrupted mid-staging left a
  ``pending`` review_run with half-built staging. Reconciliation rolls it back —
  delete the partial ``_packrat_review\`` staging and mark the run ``cancelled``
  — so a fresh re-run isn't blocked and a stray ``--confirm`` can't apply a
  partial plan. A **completed** analyze (paused, fully staged, no ``running``
  job) is left untouched. In M0 no analyze job exists yet, so only the
  interrupted-job linkage is handled; the staging-folder cleanup hook lands with
  dedup (M3).

Reconciliation performs no file I/O in M0 beyond nothing; it only flips stale
status flags to unblock re-running.
"""

from __future__ import annotations

import json
import logging

from ..db import Database
from ..util import now_iso

log = logging.getLogger("packrat.jobs.reconcile")


def reconcile_on_startup(db: Database) -> dict:
    """Flip orphaned ``running`` jobs to ``interrupted``; roll back partial analyzes.

    Idempotent: on a clean start there are no ``running`` rows and this is a
    no-op.
    """
    summary = {"interrupted_jobs": [], "rolled_back_runs": []}

    # Phase 1 — flip stale running rows to interrupted (in one txn). Capture each
    # job's type/params so the analyze-rollback (Phase 2) can act on the dedup ones.
    interrupted: list[dict] = []
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT id, type, params_json FROM jobs WHERE status='running'"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE jobs SET status='interrupted', finished_at=?, "
                "error='daemon restarted' WHERE id=?",
                (now_iso(), row["id"]),
            )
            interrupted.append({"id": row["id"], "type": row["type"], "params_json": row["params_json"]})
            summary["interrupted_jobs"].append({"id": row["id"], "type": row["type"]})

    # Phase 2 — analyze rollback (§3). A dedup/cleanup *analyze* that died mid-staging
    # left a `pending` review_run with half-built `_packrat_review\` staging and NOTHING
    # yet deleted. Roll it back: delete the partial staging + mark the run `cancelled`,
    # so a fresh re-run isn't blocked and a stray `--confirm` can't apply a partial plan.
    # NOT rolled back (left `pending` for a `--confirm` re-run, which resumes
    # idempotently, §3):
    #   - an interrupted `--confirm`/`--cancel` job (params say so), and
    #   - a run already past stage 1 or with stage_phase='applied' — a later stage means
    #     an earlier stage's deletions were already CONFIRMED; rolling back would discard
    #     a legitimately in-progress multi-stage run and its committed deletions.
    for job in interrupted:
        if job["type"] not in ("dedup", "cleanup"):
            continue
        params = _params(job["params_json"])
        if params.get("confirm") or params.get("cancel"):
            continue  # not an analyze — leave the pending run for a re-run
        rolled = _rollback_analyze(db, params.get("root_id"))
        summary["rolled_back_runs"].extend(rolled)

    if summary["interrupted_jobs"]:
        log.info(
            "reconciled %d orphaned running job(s) -> interrupted; rolled back %d partial analyze(s)",
            len(summary["interrupted_jobs"]), len(summary["rolled_back_runs"]),
        )
    return summary


def _params(params_json) -> dict:
    try:
        return json.loads(params_json) if params_json else {}
    except (ValueError, TypeError):
        return {}


def _rollback_analyze(db: Database, root_id) -> list[dict]:
    """Delete half-built staging + cancel the pending review_run for ``root_id`` (§3).

    Imported lazily so reconcile stays cheap/dependency-light on the common (no
    rollback) path. Returns a list of ``{run_id, root_id}`` rolled back.
    """
    if root_id is None:
        return []
    from .. import review

    run = db.query_one(
        "SELECT rr.id, rr.root_id, rr.stage, rr.stage_phase, r.path "
        "FROM review_runs rr JOIN roots r ON r.id=rr.root_id "
        "WHERE rr.root_id=? AND rr.status='pending'",
        (root_id,),
    )
    if run is None:
        return []
    # Only a first-stage, never-confirmed analyze is safe to roll back: past stage 1
    # (or stage_phase='applied') means an earlier stage's deletions were confirmed, so
    # this is a live multi-stage run to resume via --confirm, not a partial analyze.
    stage = run["stage"] if run["stage"] is not None else 1
    if stage != 1 or (run["stage_phase"] or "staged") == "applied":
        log.info("interrupted dedup run %d is mid-sequence (stage %s/%s) — left pending for --confirm",
                 run["id"], stage, run["stage_phase"])
        return []
    # Delete the partial staging folders (leave the _packrat_review parent).
    for name in (*review.DEDUP_STAGE_FOLDERS, review.PERCEPTUAL_TRASH):
        review.remove_tree(review.staging_folder(run["path"], name))
    with db.transaction() as conn:
        conn.execute(
            "UPDATE review_runs SET status='cancelled', confirmed_at=? WHERE id=?",
            (now_iso(), run["id"]),
        )
    log.info("rolled back interrupted analyze: review_run %d on root %d", run["id"], root_id)
    return [{"run_id": int(run["id"]), "root_id": int(root_id)}]
