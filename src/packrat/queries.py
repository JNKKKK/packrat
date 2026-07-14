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
    """Per-root list (§11): id, name, path, kind, enabled, asset count, scan recency."""
    conn = _ro()
    try:
        rows = conn.execute(
            "SELECT r.id, r.name, r.path, r.kind, r.enabled, r.last_full_scan_at, "
            "  (SELECT COUNT(DISTINCT fi.asset_id) FROM file_instances fi "
            "   WHERE fi.root_id = r.id) AS asset_count "
            "FROM roots r ORDER BY r.id"
        ).fetchall()
        return [dict(r) for r in rows]
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
