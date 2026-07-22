r"""The ``trash refresh`` job (§6.1) — absorb + empty the registered trash roots.

A thin job wrapper over :func:`packrat.trash.refresh_trash` (the shared procedure
also invoked at the start of ``cleanup`` and ``merge``). Exposed standalone for
when the user has just dropped junk into a trash folder and wants it absorbed now.

**Idempotent by construction** (record-then-delete, §6.1), so it needs no special
reconciliation: a crash/kill leaves already-recorded files' fingerprints committed
and simply re-processes any survivors on re-run. It owns **no** root — trash roots
are never owned by a review/merge (those are library-only) — so it is bound only by
the global single-worker slot (§3 guarantee 1). There is **no ``--dry-run``**
(§6.1 / §11): refresh is never a no-op.
"""

from __future__ import annotations

from .. import trash
from .context import JobContext
from .registry import JobSpec, register_job


def _run_trash_refresh(ctx: JobContext) -> None:
    # `root_id` scopes the refresh to one trash root (the `trash refresh <root>` verb
    # / TUI mascot modal); absent → every trash root (§6.1, the cleanup/merge default).
    summary = trash.refresh_trash(ctx, root_id=ctx.params.get("root_id"))
    ctx.set_result({
        "op": "trash-refresh", **summary,
        "summary": f"{summary.get('new_trashed', 0)} new trashed · "
                   f"{summary.get('flipped', 0)} flipped · {summary.get('emptied', 0)} emptied",
    })


register_job(
    JobSpec(
        type="trash-refresh",
        handler=_run_trash_refresh,
        owned_root=None,  # targets all trash roots; owns no library root (§3)
    )
)
