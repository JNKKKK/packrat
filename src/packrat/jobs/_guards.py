"""Shared pre-flight guards for the review/merge job handlers (§3, §6.2, §8 B/C).

dedup, cleanup, and merge all (a) resolve their target root and reject a non-library
one, and (b) refuse to run while *another* op holds that root. The dequeue gate (§3)
already enforces (b) for the job that *owns* the root, so these are defense-in-depth
for the sub-verbs that own nothing (cleanup preview/apply) and belt-and-suspenders for
merge — but the logic is identical enough to live in one place. Both raise
``ValueError`` (the job-handler convention → the queue records it as ``status='error'``
with the message).
"""

from __future__ import annotations

from ..roots import root_holder


def resolve_library_root(ctx, verb: str) -> dict:
    """Return this job's target ``roots`` row, requiring it be a **library** root.

    ``verb`` names the operation for the error message (``"dedup"`` / ``"cleanup"``).
    Raises ``ValueError`` if the id is unknown or the root is not a library root
    (a trash root's files are consumed by ``trash refresh``, not deduped/cleaned).
    """
    root_id = ctx.params.get("root_id")
    row = ctx.db.query_one("SELECT * FROM roots WHERE id=?", (root_id,))
    if row is None:
        raise ValueError(f"no such root id: {root_id}")
    if row["kind"] != "library":
        raise ValueError(
            f"{row['name']!r} is a {row['kind']} root; {verb} targets a library root "
            "(a trash root's files are consumed by `trash refresh`)"
        )
    return dict(row)


def reject_if_held(ctx, root: dict, *, ignore_merge: bool = False) -> None:
    """Raise ``ValueError`` if *another* active op holds ``root`` (§3 per-root lock).

    A pending review (dedup/cleanup) or an open merge stages/plans against the root,
    so a fresh op must not mutate files those plans reference. ``ignore_merge=True``
    (merge resume) skips the open-``merge_runs`` check so a resuming merge isn't held
    by its own row (§8 C). The dequeue gate covers the owning job; this re-checks for
    the sub-verbs that own no root.
    """
    holder = root_holder(ctx.db, int(root["id"]), ignore_merge=ignore_merge)
    if holder is not None:
        raise ValueError(
            f"root {root['name']!r} busy: {holder['what']} — confirm/cancel it "
            "(or let the merge finish) before operating on this folder"
        )
