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

import logging

from ..db import Database
from ..util import now_iso

log = logging.getLogger("packrat.jobs.reconcile")


def reconcile_on_startup(db: Database) -> dict:
    """Flip orphaned ``running`` jobs to ``interrupted``; return a summary.

    Idempotent: on a clean start there are no ``running`` rows and this is a
    no-op.
    """
    summary = {"interrupted_jobs": [], "rolled_back_runs": []}

    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT id, type FROM jobs WHERE status='running'"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE jobs SET status='interrupted', finished_at=?, "
                "error='daemon restarted' WHERE id=?",
                (now_iso(), row["id"]),
            )
            summary["interrupted_jobs"].append({"id": row["id"], "type": row["type"]})

            # Analyze rollback linkage (§3): an interrupted dedup/cleanup analyze
            # (its job row is the one we just flipped) with a still-pending
            # review_run and half-built staging must be rolled back. Detecting
            # "was mid-analyze vs mid-confirm" and cleaning staging folders is
            # implemented with dedup (M3); here we only note the interrupted job.
            # (merge/scan/trash-refresh resume by re-run — no rollback needed.)

    if summary["interrupted_jobs"]:
        log.info(
            "reconciled %d orphaned running job(s) -> interrupted",
            len(summary["interrupted_jobs"]),
        )
    return summary
