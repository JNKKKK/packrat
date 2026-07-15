r"""Root lifecycle + resolution (§8 A1, §11).

``roots register`` is **metadata-only and instantaneous** — it validates a folder
and inserts a ``roots`` row; it walks/fingerprints nothing (that is ``scan``). So
these are plain functions over the daemon's write connection, not a job: they take
no worker slot and run even while a scan is in flight (a new root is independent of
any running op).

Also home to:
- :func:`resolve_root` — the path-vs-``--name`` argument resolution shared by
  ``scan`` and (later) ``dedup``/``cleanup``/``merge --into`` (§11).
- :func:`root_holder` — "who owns this root right now" (pending review / open
  merge), used by both the queue's per-root reject and ``scan --all``'s skip-and-log
  (§8 A2 step 1a); centralized so both agree.
"""

from __future__ import annotations

import json
import os

from . import fsutil
from .db import Database
from .util import now_iso

VALID_KINDS = ("library", "trash")


class RootError(Exception):
    """A ``roots register`` validation failure or a failed root resolution (§8 A1/§11)."""


# ---------------------------------------------------------------------------
# register (§8 A1)
# ---------------------------------------------------------------------------
def register(
    db: Database,
    path: str,
    *,
    name: str | None = None,
    kind: str = "library",
    ignore_globs: list[str] | None = None,
) -> dict:
    """Validate ``path`` and insert a ``roots`` row (§8 A1). Return the new row.

    Raises :class:`RootError` on: missing/unreadable path, non-directory, overlap
    with an existing root (nested or containing), or a leaf-name/``--name`` clash.
    """
    if kind not in VALID_KINDS:
        raise RootError(f"invalid kind {kind!r}; must be one of {', '.join(VALID_KINDS)}")

    # 1. Canonicalize; require exists + directory + readable.
    canon = fsutil.canonicalize(path)
    ext = fsutil.extended(canon)
    if not os.path.exists(ext):
        raise RootError(f"path does not exist: {canon}")
    if not os.path.isdir(ext):
        raise RootError(f"not a directory: {canon}")
    try:
        with os.scandir(ext) as it:
            next(it, None)  # touch the listing to confirm readability
    except OSError as exc:
        raise RootError(f"not readable: {canon} ({exc})") from exc

    # 2. Overlap check — reject if this path is, contains, or is contained by a root.
    for row in db.query("SELECT id, name, path FROM roots"):
        existing = row["path"]
        if fsutil.is_within(canon, existing) or fsutil.is_within(existing, canon):
            if fsutil.paths_equal(canon, existing):
                raise RootError(f"already registered as root {row['name']!r}: {existing}")
            raise RootError(
                f"overlaps existing root {row['name']!r} ({existing}); "
                "a folder may not be nested inside or contain another root"
            )

    # 3. Unique-name check (case-insensitive) — leaf name or explicit --name.
    handle = name or fsutil.leaf_name(canon)
    if not handle:
        raise RootError(f"cannot derive a name from {canon}; pass --name")
    clash = db.query_one("SELECT name FROM roots WHERE name = ? COLLATE NOCASE", (handle,))
    if clash is not None:
        raise RootError(
            f"root name {handle!r} already in use; pick a differently-named folder "
            "or pass --name <label>"
        )

    # 4. Insert.
    globs_json = json.dumps(ignore_globs) if ignore_globs else None
    cur = db.execute(
        "INSERT INTO roots(path, name, kind, enabled, ignore_globs, last_full_scan_at) "
        "VALUES (?, ?, ?, 1, ?, NULL)",
        (canon, handle, kind, globs_json),
    )
    row = db.query_one("SELECT * FROM roots WHERE id = ?", (int(cur.lastrowid),))
    return dict(row)


def ignore_globs_of(row) -> list[str]:
    """Decode a ``roots`` row's ``ignore_globs`` JSON column to a list."""
    raw = row["ignore_globs"] if not isinstance(row, dict) else row.get("ignore_globs")
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return [str(g) for g in val] if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


# ---------------------------------------------------------------------------
# resolution (§11): path first, then --name handle
# ---------------------------------------------------------------------------
def resolve_root(db: Database, arg: str) -> dict:
    """Resolve a CLI root argument to a ``roots`` row (§11).

    1. Canonicalized as a path, exact-match a stored ``roots.path``.
    2. Else case-insensitively match a ``roots.name``.
    3. Else raise :class:`RootError`.
    """
    canon = fsutil.canonicalize(arg)
    for row in db.query("SELECT * FROM roots"):
        if fsutil.paths_equal(canon, row["path"]):
            return dict(row)
    row = db.query_one("SELECT * FROM roots WHERE name = ? COLLATE NOCASE", (arg,))
    if row is not None:
        return dict(row)
    raise RootError(f"no registered root at path or named {arg!r}; try `packrat roots` to list")


# ---------------------------------------------------------------------------
# per-root exclusivity holder (§3 guarantee 2 / §8 A2 step 1a)
# ---------------------------------------------------------------------------
def root_holder(db: Database, root_id: int) -> dict | None:
    """Describe the op currently *owning* ``root_id``, or ``None`` (§3).

    The owners are a ``pending`` ``review_runs`` row (dedup/cleanup) or an open
    ``merge_runs`` row (``planning``/``copying``) with this root as dest, per the §4
    partial-unique indexes. Returns a dict with a human ``what`` string so both the
    queue reject and ``scan --all`` skip-log speak the same language.
    """
    rr = db.query_one(
        "SELECT id, run_type, created_at FROM review_runs WHERE root_id=? AND status='pending'",
        (root_id,),
    )
    if rr is not None:
        return {
            "type": "review_run",
            "run_type": rr["run_type"],
            "since": rr["created_at"],
            "what": f"{rr['run_type']} pending since {rr['created_at']}",
        }
    mr = db.query_one(
        "SELECT id, status, created_at FROM merge_runs "
        "WHERE dest_root_id=? AND status IN ('planning','copying')",
        (root_id,),
    )
    if mr is not None:
        return {
            "type": "merge_run",
            "status": mr["status"],
            "since": mr["created_at"],
            "what": f"merge {mr['status']} since {mr['created_at']}",
        }
    return None
