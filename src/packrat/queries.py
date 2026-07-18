"""Read-only snapshot queries (§3, §11) — safe anytime, never blocked by a job.

These back ``status``/``roots`` and the TUI stat panels. They open a **read-only**
connection so they never contend with the single writer (WAL allows concurrent
readers). Kept deliberately thin in M0 — the collection is empty until M1 scan —
but the shapes match §11 so later milestones fill them in without changing the API.
"""

from __future__ import annotations

from pathlib import Path

from . import db as _db


def _ro():
    return _db.connect(read_only=True)


def status_snapshot() -> dict:
    """Global rollup (§11): asset counts, trashed, per-root, running/interrupted jobs."""
    conn = _ro()
    try:
        assets = conn.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"]
        photos = conn.execute(
            "SELECT COUNT(*) c FROM assets WHERE media_type='photo' AND status='active'"
        ).fetchone()["c"]
        videos = conn.execute(
            "SELECT COUNT(*) c FROM assets WHERE media_type='video' AND status='active'"
        ).fetchone()["c"]
        trashed = conn.execute(
            "SELECT COUNT(*) c FROM assets WHERE status='trashed'"
        ).fetchone()["c"]
        running = conn.execute(
            "SELECT j.id, j.type, j.root_id, j.total, j.done, j.started_at, j.params_json, "
            "  r.name AS root_name FROM jobs j LEFT JOIN roots r ON r.id=j.root_id "
            "WHERE j.status='running'"
        ).fetchone()
        interrupted = conn.execute(
            "SELECT id, type, started_at, params_json FROM jobs "
            "WHERE status='interrupted' ORDER BY id DESC LIMIT 20"
        ).fetchall()
        # The durable FIFO backlog (§3), oldest-first, each annotated with why it
        # waits: 'blocked' when its owned root is held (read from the catalog via the
        # shared root_holder), else runnable ('waiting for worker'). Computed here so
        # `status`/TUI show the same reasons the live queue enforces at dequeue.
        queued = _queued_with_reasons(conn)
        pending_reviews = conn.execute(
            "SELECT rr.id, rr.root_id, rr.run_type, rr.stage, rr.created_at, r.name root_name "
            "FROM review_runs rr JOIN roots r ON r.id = rr.root_id "
            "WHERE rr.status='pending'"
        ).fetchall()
        pending_list = []
        for r in pending_reviews:
            d = dict(r)
            d["counts"] = _review_counts(conn, r["id"], r["run_type"], r["stage"])
            pending_list.append(d)
        return {
            "assets": assets,
            "photos": photos,
            "videos": videos,
            "trashed": trashed,
            "running": _job_dict(running) if running else None,
            "queued": queued,
            "interrupted": [dict(r) for r in interrupted],
            "pending_reviews": pending_list,
            "roots": roots_snapshot(),
        }
    finally:
        conn.close()


def _annotate_queued_row(conn, row) -> dict:
    """Add ``label`` + ``blocked`` to a queued ``jobs`` row (§3/§12).

    A job is *blocked* when its **owned** root is held by a pending review / open
    merge — the same predicate the queue applies at dequeue. Ownership is narrower
    than ``root_id`` (e.g. a dedup ``--confirm`` owns nothing), so we recompute it
    from ``type`` + ``params`` via each job spec's ``owned_root`` and the shared
    ``root_holder``. ``blocked`` is the holder dict or None (None → waiting for worker).
    """
    import json as _json

    from .jobs import get_job_spec, job_label
    from .roots import root_holder

    try:
        params = _json.loads(row["params_json"] or "{}")
    except (ValueError, TypeError):
        params = {}
    blocked = None
    spec = get_job_spec(row["type"])
    if spec is not None and spec.owned_root is not None:
        owned = spec.owned_root(params)
        if owned is not None:
            blocked = root_holder(_DBShim(conn), owned, ignore_merge=spec.ignore_merge_holder)
    d = dict(row)
    d["label"] = job_label(row["type"], params, root_name=row["root_name"])
    d["blocked"] = blocked
    return d


def _queued_with_reasons(conn, root_id: int | None = None) -> list[dict]:
    """Backlog rows (dequeue order) + a per-job blocked reason (§3/§12).

    Ordered ``priority DESC, enqueued_at, id`` — the SAME order the queue dequeues in
    (§3), so the displayed backlog matches what will actually run next (a prioritized
    job appears at the front). With ``root_id`` set, only that root's queued jobs
    (``jobs.root_id`` = it) — the per-root detail view (§12); without it, the whole
    backlog (global Queue panel).
    """
    sql = (
        "SELECT j.id, j.type, j.root_id, j.status, j.enqueued_at, j.params_json, "
        "  r.name AS root_name FROM jobs j LEFT JOIN roots r ON r.id=j.root_id "
        "WHERE j.status='queued'"
    )
    args: tuple = ()
    if root_id is not None:
        sql += " AND j.root_id=?"
        args = (root_id,)
    sql += " ORDER BY j.priority DESC, j.enqueued_at, j.id"
    return [_annotate_queued_row(conn, row) for row in conn.execute(sql, args).fetchall()]


class _DBShim:
    """Adapt a raw read-only ``sqlite3`` connection to the ``.query_one`` interface
    ``roots.root_holder`` expects (it normally takes the daemon's ``Database``)."""

    def __init__(self, conn):
        self._conn = conn

    def query_one(self, sql: str, params: tuple = ()):
        return self._conn.execute(sql, params).fetchone()


def roots_snapshot() -> list[dict]:
    """Per-root list (§11): id, name, path, kind, enabled, asset count, scan recency.

    ``instance_count`` counts physical files; ``asset_count`` distinct content in
    the root. ``last_full_scan_at`` is stamped only by ``scan --full`` (§8 A2 step
    11); a plain incremental scan does not move it. ``last_scan_at`` is the general
    scan recency — ``MAX(file_instances.last_seen_at)``, bumped by *every* scan
    (incremental or full) on every present file (§8 A2 step 4/9), so it answers
    "when was this root last scanned" without a schema column.
    """
    conn = _ro()
    try:
        rows = conn.execute(
            "SELECT r.id, r.name, r.path, r.kind, r.enabled, r.last_full_scan_at, "
            "  (SELECT COUNT(DISTINCT fi.asset_id) FROM file_instances fi "
            "   WHERE fi.root_id = r.id) AS asset_count, "
            "  (SELECT COUNT(*) FROM file_instances fi WHERE fi.root_id = r.id) "
            "   AS instance_count, "
            "  (SELECT MAX(fi.last_seen_at) FROM file_instances fi "
            "   WHERE fi.root_id = r.id) AS last_scan_at "
            "FROM roots r ORDER BY r.id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def root_detail(root_arg: str) -> dict | None:
    """One root's detail for ``packrat status <root>`` (§11).

    Resolves ``root_arg`` as path-then-name (§11) via a read-only connection, then
    reports its counts + scan recency + any pending review run.
    """
    from . import fsutil

    conn = _ro()
    try:
        rows = conn.execute("SELECT * FROM roots").fetchall()
        canon = fsutil.canonicalize(root_arg)
        match = None
        for r in rows:
            if fsutil.paths_equal(canon, r["path"]):
                match = r
                break
        if match is None:
            for r in rows:
                if r["name"].lower() == root_arg.lower():
                    match = r
                    break
        if match is None:
            return None
        rid = match["id"]
        photos = conn.execute(
            "SELECT COUNT(DISTINCT fi.asset_id) c FROM file_instances fi "
            "JOIN assets a ON a.id=fi.asset_id WHERE fi.root_id=? AND a.media_type='photo'",
            (rid,),
        ).fetchone()["c"]
        videos = conn.execute(
            "SELECT COUNT(DISTINCT fi.asset_id) c FROM file_instances fi "
            "JOIN assets a ON a.id=fi.asset_id WHERE fi.root_id=? AND a.media_type='video'",
            (rid,),
        ).fetchone()["c"]
        row = conn.execute(
            "SELECT COUNT(*) c, MAX(last_seen_at) last_scan_at "
            "FROM file_instances WHERE root_id=?",
            (rid,),
        ).fetchone()
        instances, last_scan_at = row["c"], row["last_scan_at"]
        pending = conn.execute(
            "SELECT id, run_type, stage, created_at FROM review_runs "
            "WHERE root_id=? AND status='pending'",
            (rid,),
        ).fetchone()
        pending_dict = None
        if pending is not None:
            pending_dict = dict(pending)
            pending_dict["counts"] = _review_counts(
                conn, pending["id"], pending["run_type"], pending["stage"]
            )
        # The root's live queue view (§12 root detail): the job running ON this root
        # (if any), plus this root's queued backlog with blocked reasons. Both key off
        # jobs.root_id, so a `scan --all` (root_id NULL) isn't attributed to any root.
        running_row = conn.execute(
            "SELECT j.id, j.type, j.root_id, j.status, j.total, j.done, j.started_at, "
            "  j.params_json, r.name AS root_name FROM jobs j "
            "LEFT JOIN roots r ON r.id=j.root_id WHERE j.status='running' AND j.root_id=?",
            (rid,),
        ).fetchone()
        running_dict = _job_dict(running_row) if running_row is not None else None
        queued_here = _queued_with_reasons(conn, root_id=rid)
        # Most-recent persisted scan result for this root + its problem files, so
        # `status <root>` can re-render the last scan's banner + undecodable/error
        # paths (the §scan-results read path). Newest by job_id.
        last_scan = conn.execute(
            "SELECT * FROM scan_results WHERE root_id=? ORDER BY job_id DESC LIMIT 1",
            (rid,),
        ).fetchone()
        # Undecodable problem files are re-derived LIVE from the catalog (current state),
        # NOT read from the frozen last-scan snapshot — a `cleanup --undecodable` or a
        # decoder-upgrade rescan changes the set without necessarily writing a fresh
        # snapshot, so the frozen rows go stale (they'd keep listing files just deleted).
        # This mirrors what scan itself persists (§8 A2 Phase 5 re-derives the same way),
        # only computed at read time so `status` always reflects the root as it is now.
        undec_rows = conn.execute(
            "SELECT DISTINCT fi.path, a.media_type, a.decode_error detail "
            "FROM assets a JOIN file_instances fi ON fi.asset_id=a.id "
            "WHERE fi.root_id=? AND a.undecodable=1 ORDER BY fi.path",
            (rid,),
        ).fetchall()
        problem_files = [
            {"path": r["path"], "media_type": r["media_type"],
             "problem": "undecodable", "detail": r["detail"]}
            for r in undec_rows
        ]
        # Read-errors have no asset to re-derive, so they stay per-pass (from the last scan).
        if last_scan is not None:
            problem_files += [
                dict(r)
                for r in conn.execute(
                    "SELECT path, media_type, problem, detail FROM scan_problem_files "
                    "WHERE job_id=? AND root_id=? AND problem='read-error' ORDER BY path",
                    (last_scan["job_id"], rid),
                ).fetchall()
            ]
        return {
            "id": rid, "name": match["name"], "path": match["path"], "kind": match["kind"],
            "enabled": match["enabled"], "last_full_scan_at": match["last_full_scan_at"],
            "last_scan_at": last_scan_at,
            "photos": photos, "videos": videos, "instances": instances,
            "pending_review": pending_dict,
            "running_job": running_dict,
            "queued_jobs": queued_here,
            "last_scan": dict(last_scan) if last_scan is not None else None,
            # Live current undecodable count (see problem_files above) — the banner shows
            # this, not the stale last-scan number, so count + list agree post-cleanup.
            "undecodable_current": len(undec_rows),
            "problem_files": problem_files,
        }
    finally:
        conn.close()


def _resolve_root_ro(conn, root_arg: str):
    """Resolve ``root_arg`` (path-then-name, §11) against an open RO connection."""
    from . import fsutil

    rows = conn.execute("SELECT * FROM roots").fetchall()
    canon = fsutil.canonicalize(root_arg)
    for r in rows:
        if fsutil.paths_equal(canon, r["path"]):
            return r
    for r in rows:
        if r["name"].lower() == root_arg.lower():
            return r
    return None


def cleanup_exact_preview(root_arg: str, mode: str = "exact") -> dict | None:
    """Count a library root's files a one-shot ``cleanup`` mode would delete (§6.2, §9.1).

    Backs the CLI's typed confirmation — the count the user approves before the apply
    job deletes. Read-only + stable (the preview job commits any refresh before this
    runs). ``mode``:
    - ``exact`` → files whose asset is ``trashed`` (byte-identical trash re-appearances);
    - ``undecodable`` → the folder's ``undecodable=1`` **active** files (§9.1).
    ``network`` is how many sit on a non-recyclable network share (permanent, §10).
    Returns ``None`` if the root doesn't resolve; raises nothing on a trash root (the
    handler rejects that separately). ``perceptual`` mode has no count-confirm (it stages
    for review), so it is not a valid ``mode`` here.
    """
    conn = _ro()
    try:
        match = _resolve_root_ro(conn, root_arg)
        if match is None:
            return None
        if mode == "undecodable":
            where = "a.undecodable=1 AND a.status='active'"
        else:  # exact
            where = "a.status='trashed'"
        rows = conn.execute(
            f"SELECT fi.path FROM file_instances fi JOIN assets a ON a.id=fi.asset_id "
            f"WHERE fi.root_id=? AND {where}",
            (match["id"],),
        ).fetchall()
        from . import fsutil

        network = sum(1 for r in rows if fsutil.is_network_path(r["path"]))
        return {"root_id": match["id"], "name": match["name"], "kind": match["kind"],
                "count": len(rows), "network": network}
    finally:
        conn.close()


def _review_counts(conn, run_id: int, run_type: str, stage: int | None) -> dict:
    """Actionable count breakdown for a pending review run's *current* stage (§11).

    Scoped to ``stage`` (the run's cursor) so the numbers reflect what is **still
    pending**, not the whole run's history — a dedup run mid-sequence keeps its
    already-confirmed earlier-stage ``review_actions`` rows (dedup never deletes
    them), so counting all rows would report already-deleted exact dups as still
    "to delete". Cleanup is single-stage (``stage=1``) so the filter is a no-op there.

    - **dedup:** ``{to_delete_exact, groups, members}`` — for the current stage:
      exact deletions (stage 1) or near-dup group/member totals (stages 2/3).
    - **cleanup-perceptual:** ``{exact, perceptual}`` — exact-trash matches (delete on
      confirm) + staged perceptual candidates.
    """
    if stage is None:
        rows = conn.execute(
            "SELECT kind, group_no FROM review_actions WHERE run_id=?", (run_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT kind, group_no FROM review_actions WHERE run_id=? AND stage=?",
            (run_id, stage),
        ).fetchall()
    if run_type == "dedup":
        exact = sum(1 for r in rows if r["kind"] == "exact")
        groups = {r["group_no"] for r in rows if r["kind"] == "perceptual" and r["group_no"] is not None}
        members = sum(1 for r in rows if r["kind"] == "perceptual")
        return {"to_delete_exact": exact, "groups": len(groups), "members": members}
    exact = sum(1 for r in rows if r["kind"] == "exact")
    perceptual = sum(1 for r in rows if r["kind"] == "perceptual")
    return {"exact": exact, "perceptual": perceptual}


def _job_dict(row) -> dict:
    """Shape a ``jobs`` row for a client: add a derived display ``label`` (§12).

    The label is computed from ``type`` + ``params_json`` (the params→label rule,
    :mod:`packrat.jobs.labels`), with the root name resolved from ``root_id`` when set.
    ``result_json``/``params_json`` are passed through as raw JSON strings (the client
    decodes what it needs).
    """
    import json as _json

    from .jobs import job_label

    d = dict(row)
    params = {}
    try:
        params = _json.loads(d.get("params_json") or "{}")
    except (ValueError, TypeError):
        params = {}
    d["label"] = job_label(d["type"], params, root_name=d.get("root_name"))
    return d


def recent_jobs(limit: int = 20) -> list[dict]:
    """Recent job runs for the TUI 'recent jobs' list (§12), newest-first.

    Includes ``queued`` rows (the backlog) and terminal history. Each row carries a
    derived ``label`` and its root name (via ``jobs.root_id``).
    """
    conn = _ro()
    try:
        rows = conn.execute(
            "SELECT j.id, j.type, j.root_id, j.status, j.total, j.done, j.enqueued_at, "
            "  j.started_at, j.finished_at, j.error, j.result_json, j.params_json, "
            "  r.name AS root_name "
            "FROM jobs j LEFT JOIN roots r ON r.id = j.root_id "
            "ORDER BY j.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_job_dict(r) for r in rows]
    finally:
        conn.close()


def queued_jobs() -> list[dict]:
    """The durable backlog in dequeue order (§3/§12) — the TUI Queue panel.

    Ordered ``priority DESC, enqueued_at, id`` (matching the queue's dequeue, §3), so a
    prioritized job shows at the front. Each queued row is annotated with its display
    ``label``; the *blocked* reason (owned root held) is computed by the daemon's live
    queue (:meth:`JobQueue.blocked_reason`), not here, since it depends on in-memory
    holder state.
    """
    conn = _ro()
    try:
        rows = conn.execute(
            "SELECT j.id, j.type, j.root_id, j.status, j.enqueued_at, j.params_json, "
            "  r.name AS root_name "
            "FROM jobs j LEFT JOIN roots r ON r.id = j.root_id "
            "WHERE j.status='queued' ORDER BY j.priority DESC, j.enqueued_at, j.id"
        ).fetchall()
        return [_job_dict(r) for r in rows]
    finally:
        conn.close()


def root_jobs(root_id: int, limit: int = 50) -> list[dict]:
    """One root's jobs — current (queued/running) + history, newest-first (§12).

    Keys off ``jobs.root_id`` (the root a job concerns). A ``scan --all`` has no
    ``root_id`` so it doesn't appear here per-root — its per-root outcome lives in
    ``scan_results`` (§4), surfaced separately by ``status <root>``.
    """
    conn = _ro()
    try:
        rows = conn.execute(
            "SELECT j.id, j.type, j.root_id, j.status, j.total, j.done, j.enqueued_at, "
            "  j.started_at, j.finished_at, j.error, j.result_json, j.params_json, "
            "  r.name AS root_name "
            "FROM jobs j LEFT JOIN roots r ON r.id = j.root_id "
            "WHERE j.root_id=? ORDER BY j.id DESC LIMIT ?",
            (root_id, limit),
        ).fetchall()
        return [_job_dict(r) for r in rows]
    finally:
        conn.close()


def job_detail(job_id: int) -> dict | None:
    conn = _ro()
    try:
        row = conn.execute(
            "SELECT j.*, r.name AS root_name FROM jobs j "
            "LEFT JOIN roots r ON r.id = j.root_id WHERE j.id=?",
            (job_id,),
        ).fetchone()
        return _job_dict(row) if row else None
    finally:
        conn.close()
