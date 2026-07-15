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
            "SELECT id, type, total, done, started_at FROM jobs WHERE status='running'"
        ).fetchone()
        interrupted = conn.execute(
            "SELECT id, type, started_at, params_json FROM jobs "
            "WHERE status='interrupted' ORDER BY id DESC LIMIT 20"
        ).fetchall()
        pending_reviews = conn.execute(
            "SELECT rr.id, rr.root_id, rr.run_type, rr.created_at, r.name root_name "
            "FROM review_runs rr JOIN roots r ON r.id = rr.root_id "
            "WHERE rr.status='pending'"
        ).fetchall()
        return {
            "assets": assets,
            "photos": photos,
            "videos": videos,
            "trashed": trashed,
            "running": dict(running) if running else None,
            "interrupted": [dict(r) for r in interrupted],
            "pending_reviews": [dict(r) for r in pending_reviews],
            "roots": roots_snapshot(),
        }
    finally:
        conn.close()


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
            "SELECT id, run_type, created_at FROM review_runs WHERE root_id=? AND status='pending'",
            (rid,),
        ).fetchone()
        return {
            "id": rid, "name": match["name"], "path": match["path"], "kind": match["kind"],
            "enabled": match["enabled"], "last_full_scan_at": match["last_full_scan_at"],
            "last_scan_at": last_scan_at,
            "photos": photos, "videos": videos, "instances": instances,
            "pending_review": dict(pending) if pending else None,
        }
    finally:
        conn.close()


def recent_jobs(limit: int = 20) -> list[dict]:
    """Recent job runs for the TUI 'recent jobs' list (§12)."""
    conn = _ro()
    try:
        rows = conn.execute(
            "SELECT id, type, status, total, done, started_at, finished_at, error "
            "FROM jobs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def job_detail(job_id: int) -> dict | None:
    conn = _ro()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
