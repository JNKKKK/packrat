"""Shared sample data for the TUI golden-frame tests (§Testing).

Query-shaped sample dicts matching exactly what :mod:`packrat.queries` returns
(``status_snapshot`` / ``roots_snapshot`` / ``root_detail`` / job rows + per-op
``result_json``), so the frame tests render the pure builders against a stable,
mockup-scale dataset. (The richer, deliberately-oversized dataset the live offline
app renders is :mod:`packrat.tui.demo`.)

Timestamps are real ISO strings anchored to :data:`REFERENCE_NOW` so relative-time
rendering ("2h ago", "today 11:31") is **deterministic** in tests — the render
helpers take an explicit ``now`` (never call the wall clock in a golden test).
"""

from __future__ import annotations

import json

# Fixed reference "now" all fixture timestamps are anchored to. The mockup mixes
# "deduped today 11:31" (iPhone) with "deduped Jul 12" (Camera), so now ≈ Jul 15.
REFERENCE_NOW = "2026-07-15T13:30:00"


def _rj(d: dict) -> str:
    """Serialize a result_json dict the way the daemon stores it (a JSON string)."""
    return json.dumps(d)


# --- roots_snapshot() rows -------------------------------------------------
# Fields exactly per queries.roots_snapshot(): id, name, path, kind, enabled,
# last_full_scan_at, asset_count, photos, videos, instance_count, last_scan_at,
# last_dedup_at. Order here is registration (id ASC) — exactly what the query
# returns; the TUI sorts client-side (the [s] cycle), whose default
# "most-recently-registered" is an id-DESC reordering (→ Downloads, _Trash,
# Photos, Camera, iPhone, the mockup dashboard order).
ROOTS: list[dict] = [
    {
        "id": 1, "name": "iPhone", "path": r"D:\Backup\iPhone", "kind": "library",
        "enabled": 1, "last_full_scan_at": "2026-07-10T10:00:00",
        "asset_count": 98412, "photos": 92110, "videos": 6302, "instance_count": 98540,
        "size_bytes": 512_000_000_000,
        "last_scan_at": "2026-07-15T09:04:00", "last_dedup_at": "2026-07-15T11:31:00",
    },
    {
        "id": 2, "name": "Camera", "path": r"E:\Photos", "kind": "library",
        "enabled": 1, "last_full_scan_at": "2026-07-08T08:00:00",
        "asset_count": 26150, "photos": 25900, "videos": 250, "instance_count": 26150,
        "size_bytes": 148_000_000_000,
        "last_scan_at": "2026-07-14T09:31:00", "last_dedup_at": "2026-07-12T15:00:00",
    },
    {
        "id": 3, "name": "Photos", "path": r"E:\Photos2", "kind": "library",
        "enabled": 1, "last_full_scan_at": "2026-07-14T20:00:00",
        "asset_count": 8900, "photos": 8600, "videos": 300, "instance_count": 8900,
        "size_bytes": 41_300_000_000,
        "last_scan_at": "2026-07-15T09:00:00", "last_dedup_at": None,
    },
    {
        "id": 4, "name": "_Trash", "path": r"D:\Backup\_Trash", "kind": "trash",
        "enabled": 1, "last_full_scan_at": None,
        "asset_count": 0, "photos": 0, "videos": 0, "instance_count": 0,
        "size_bytes": 0,
        "last_scan_at": None, "last_dedup_at": None,
    },
    {
        "id": 5, "name": "Downloads", "path": r"D:\dump", "kind": "library",
        "enabled": 1, "last_full_scan_at": None,
        "asset_count": 241, "photos": 200, "videos": 41, "instance_count": 241,
        "size_bytes": 3_200_000_000,
        "last_scan_at": "2026-07-15T11:02:00", "last_dedup_at": None,
    },
]


# --- job rows (shape of queries._job_dict) --------------------------------
# id, type, root_id, status, total, done, enqueued_at, started_at, finished_at,
# error, result_json, params_json, root_name, label. The queries add `label`;
# fixtures carry it too so a widget can render without recomputing.
def _job(**kw) -> dict:
    base = {
        "id": 0, "type": "scan", "root_id": None, "status": "done",
        "total": None, "done": 0, "enqueued_at": None, "started_at": None,
        "finished_at": None, "error": None, "result_json": None,
        "params_json": "{}", "root_name": None, "label": "",
    }
    base.update(kw)
    return base


RUNNING_SCAN = _job(
    id=418, type="scan", root_id=1, status="running", total=13204, done=8912,
    started_at="2026-07-15T09:04:00", params_json=_rj({"root_id": 1}),
    root_name="iPhone", label="scan iPhone",
    # `_eta_s` is the TUI-side estimate (§cross-cutting) — the app derives it live
    # from the SSE rate; the fixture pins it to the mockup's "ETA 4m" (240s).
    _eta_s=240,
)

# Recent/terminal jobs with per-op result_json (§Result cards).
SCAN_DONE = _job(
    id=418, type="scan", root_id=1, status="done", total=13204, done=13204,
    started_at="2026-07-15T09:04:00", finished_at="2026-07-15T09:12:00",
    root_name="iPhone", label="scan iPhone",
    result_json=_rj({
        "op": "scan", "dry_run": False, "full": False, "embed": False,
        "roots_scanned": 1, "roots_skipped": 0, "new": 412, "exact_dup": 0,
        "backfilled": 0, "matches_trashed": 17, "undecodable": 3, "errors": 0,
        "read_errors": 0, "skipped_fastpath": 8912, "deleted_instances": 2,
        "forgotten_assets": 1, "candidates": 13204,
        "summary": "+412 new · 3 undecodable",
    }),
)

MERGE_DONE = _job(
    id=421, type="merge", root_id=2, status="done", total=246, done=246,
    started_at="2026-07-14T22:05:00", finished_at="2026-07-14T22:10:00",
    root_name="Camera",
    params_json=_rj({"source": r"E:\iphone_dump", "into": "Camera"}),
    label="merge iphone_dump → Camera",
    result_json=_rj({
        "op": "merge", "dry_run": False, "source": r"E:\iphone_dump",
        "dest_root": "Camera", "new": 240, "exact_known": 18, "trashed": 1,
        "dup_in_source": 6, "collisions": 2, "unindexed": 0, "errors": 0,
        "summary": "240 copied · 18 exact-known",
    }),
)

# A paused dedup: the analyze job COMPLETED (status='done') and left a pending
# review_run — the card keys off result_json.review_status='pending' to show the
# ⚠ awaiting-review card with its confirm/cancel actions (§5.4), NOT a live bar.
DEDUP_PENDING = _job(
    id=430, type="dedup", root_id=3, status="done", total=None, done=0,
    started_at="2026-07-15T11:31:00", finished_at="2026-07-15T11:31:00",
    params_json=_rj({"root_id": 3}), root_name="Photos",
    label="dedup Photos (analyze)",
    result_json=_rj({
        "op": "dedup", "action": "analyze", "review_status": "pending", "stage": 2,
        "to_delete_exact": 0, "groups": 18, "members": 47,
        "summary": "stage 2 · 18 groups / 47 members",
    }),
)

DEDUP_DONE = _job(
    id=430, type="dedup", root_id=3, status="done", total=None, done=0,
    finished_at="2026-07-15T11:48:00",
    params_json=_rj({"root_id": 3, "confirm": True}), root_name="Photos",
    label="dedup Photos (confirm)",
    result_json=_rj({
        "op": "dedup", "action": "confirm", "review_status": "completed", "stage": 3,
        "to_delete_exact": 12, "groups": 0, "members": 0,
        "summary": "52 deleted (12 exact · 40 near-dup) · 9 spared",
    }),
)

DEDUP_CLEAN = _job(
    id=451, type="dedup", root_id=1, status="done", finished_at="2026-07-13T10:00:00",
    params_json=_rj({"root_id": 1}), root_name="iPhone", label="dedup iPhone (analyze)",
    result_json=_rj({
        "op": "dedup", "action": "analyze",
        "summary": "already clean — no exact duplicates or near-dup groups",
    }),
)

TRASH_REFRESH_DONE = _job(
    id=402, type="trash-refresh", status="done", finished_at="2026-07-15T08:00:00",
    label="trash refresh",
    result_json=_rj({
        "op": "trash-refresh", "roots": 1, "new_trashed": 9, "flipped": 3,
        "already_trashed": 1, "emptied": 12, "undeletable": 0, "errors": 0,
        "summary": "9 new trashed · 3 flipped · 1 known · 12 emptied",
    }),
)

UNTRASH_DONE = _job(
    id=500, type="untrash", status="done", finished_at="2026-07-15T07:00:00",
    params_json=_rj({"path": r"R:\recovered\IMG_4471.jpg"}),
    label="untrash IMG_4471.jpg",
    result_json=_rj({
        "op": "untrash", "dry_run": False, "untrashed": 1, "forgotten": 0,
        "already_active": 0, "unknown": 0, "errors": 0,
        "summary": "1 reactivated · 0 forgotten · 0 active · 0 unknown",
    }),
)

# A paused `cleanup --trash-perceptual`: the analyze job COMPLETED and left a pending
# review_run — like DEDUP_PENDING, but op='cleanup', so the card must route its
# confirm/cancel to `cleanup … --confirm` (not dedup). Its analyze emits
# review_status='pending' (jobs/cleanup.py), which the card keys off (§6.2).
CLEANUP_PENDING = _job(
    id=462, type="cleanup", root_id=3, status="done", finished_at="2026-07-15T11:40:00",
    params_json=_rj({"root_id": 3, "mode": "perceptual"}), root_name="Photos",
    label="cleanup Photos (perceptual · analyze)",
    result_json=_rj({
        "op": "cleanup", "mode": "perceptual", "action": "analyze",
        "review_status": "pending", "stage": 1, "to_delete_exact": 4,
        "groups": 11, "members": 11,
        "summary": "4 exact + 11 perceptual staged for review",
    }),
)

CLEANUP_ERROR = _job(
    id=461, type="cleanup", root_id=3, status="error", finished_at="2026-07-15T11:00:00",
    error="nothing to confirm; run `dedup Photos` first.",
    params_json=_rj({"root_id": 3, "mode": "perceptual", "confirm": True}),
    root_name="Photos", label="cleanup Photos (perceptual · confirm)",
)

SCAN_INTERRUPTED = _job(
    id=455, type="scan", root_id=1, status="interrupted",
    finished_at="2026-07-13T10:00:00", error="daemon restarted",
    params_json=_rj({"root_id": 1}), root_name="iPhone", label="scan iPhone",
)

# The undecodable/read-error files behind SCAN_DONE's counts (shape of
# queries.job_problem_files) — the scan result card lists these with paths+reasons.
SCAN_PROBLEM_FILES: list[dict] = [
    {"path": r"D:\Backup\iPhone\2019\IMG_0032.HEIC", "media_type": "photo",
     "problem": "undecodable", "detail": "PIL: cannot identify image file"},
    {"path": r"D:\Backup\iPhone\clips\old.3gp", "media_type": "video",
     "problem": "undecodable", "detail": None},
    {"path": r"D:\Backup\iPhone\2018\IMG_9910.HEIC", "media_type": "photo",
     "problem": "undecodable", "detail": "PIL: cannot identify image file"},
]


# --- status_snapshot() -----------------------------------------------------
def status_snapshot(*, running: bool = False) -> dict:
    """A ``status_snapshot()``-shaped dict (idle by default; ``running`` adds a job)."""
    return {
        "assets": 124803, "photos": 111240, "videos": 13563, "trashed": 3904,
        "size_bytes": 704_500_000_000,          # Σ of the ROOTS' size_bytes
        "lifetime_deduped": 8241,
        "running": dict(RUNNING_SCAN) if running else None,
        "queued": [dict(QUEUED_MERGE), dict(QUEUED_SCAN), dict(QUEUED_DEDUP)] if running else [],
        "interrupted": [],
        "pending_reviews": [],
        "roots": [dict(r) for r in ROOTS],
    }


# --- queued backlog rows (with blocked reason) -----------------------------
QUEUED_MERGE = _job(
    id=419, type="merge", root_id=2, status="queued", enqueued_at="2026-07-15T09:05:00",
    params_json=_rj({"source": r"D:\dump", "into": "Camera"}), root_name="Camera",
    label="merge dump → Camera", blocked=None,
)
QUEUED_SCAN = _job(
    id=420, type="scan", root_id=3, status="queued", enqueued_at="2026-07-15T09:06:00",
    params_json=_rj({"root_id": 3}), root_name="Photos", label="scan Photos",
    blocked={"run_type": "dedup", "since": "2026-07-15T08:00:00",
             "what": "dedup pending since 2026-07-15T08:00:00"},
)
QUEUED_DEDUP = _job(
    id=421, type="dedup", root_id=3, status="queued", enqueued_at="2026-07-15T09:07:00",
    params_json=_rj({"root_id": 3, "confirm": True}), root_name="Photos",
    label="dedup Photos (confirm)",
    blocked={"run_type": "dedup", "since": "2026-07-15T08:00:00",
             "what": "dedup pending since 2026-07-15T08:00:00"},
)


def queued_jobs() -> list[dict]:
    return [dict(QUEUED_MERGE), dict(QUEUED_SCAN), dict(QUEUED_DEDUP)]


def recent_jobs() -> list[dict]:
    return [dict(j) for j in (DEDUP_DONE, SCAN_DONE, MERGE_DONE, SCAN_INTERRUPTED)]


# --- root_detail() ---------------------------------------------------------
def root_detail_pending() -> dict:
    """iPhone with a pending dedup review (§3.1, the actionable case)."""
    return {
        "id": 1, "name": "iPhone", "path": r"D:\Backup\iPhone", "kind": "library",
        "enabled": 1, "last_full_scan_at": "2026-07-10T10:00:00",
        "last_scan_at": "2026-07-15T09:04:00",
        "photos": 92110, "videos": 6302, "instances": 98540,
        "size_bytes": 512_000_000_000,
        "pending_review": {
            "id": 77, "run_type": "dedup", "stage": 2, "created_at": "2026-07-15T11:31:00",
            "counts": {"to_delete_exact": 240, "groups": 18, "members": 47},
        },
        "last_dedup_at": "2026-07-15T11:31:00", "last_cleanup_at": None,
        "running_job": None,
        "queued_jobs": [],
        "last_scan": {
            "job_id": 418, "root_id": 1, "root_name": "iPhone", "full": 0, "embed": 0,
            "profiled": 0, "candidates": 13204, "new": 412, "exact_dup": 0,
            "backfilled": 0, "matches_trashed": 17, "skipped_fastpath": 8912,
            "undecodable": 3, "errors": 0, "deleted_instances": 2, "forgotten_assets": 1,
            "root_offline": 0, "profile_json": None, "created_at": "2026-07-15T09:04:00",
        },
        "undecodable_current": 3,
        "problem_files": [
            {"path": r"D:\Backup\iPhone\2019\IMG_0032.HEIC", "media_type": "photo",
             "problem": "undecodable", "detail": "PIL: cannot identify image file"},
            {"path": r"D:\Backup\iPhone\clips\old.3gp", "media_type": "video",
             "problem": "undecodable", "detail": None},
            {"path": r"D:\Backup\iPhone\2018\IMG_9910.HEIC", "media_type": "photo",
             "problem": "undecodable", "detail": None},
        ],
    }


def root_detail_clean() -> dict:
    """Camera, no pending review (§3.2, the clean case)."""
    return {
        "id": 2, "name": "Camera", "path": r"E:\Photos", "kind": "library",
        "enabled": 1, "last_full_scan_at": "2026-07-08T08:00:00",
        "last_scan_at": "2026-07-14T09:31:00",
        "photos": 25900, "videos": 250, "instances": 26150,
        "size_bytes": 148_000_000_000,
        "pending_review": None,
        "last_dedup_at": "2026-07-12T15:00:00", "last_cleanup_at": None,
        "running_job": None,
        "queued_jobs": [],
        "last_scan": {
            "job_id": 415, "root_id": 2, "root_name": "Camera", "full": 0, "embed": 0,
            "profiled": 0, "candidates": 26150, "new": 26, "exact_dup": 0,
            "backfilled": 0, "matches_trashed": 0, "skipped_fastpath": 26124,
            "undecodable": 0, "errors": 0, "deleted_instances": 0, "forgotten_assets": 0,
            "root_offline": 0, "profile_json": None, "created_at": "2026-07-15T09:31:00",
        },
        "undecodable_current": 0,
        "problem_files": [],
    }


def root_detail_cleanup_pending() -> dict:
    """Photos with a pending ``cleanup --trash-perceptual`` review (§6.2).

    Its ``counts`` carry the CLEANUP shape (``{exact, perceptual, network}``), NOT the
    dedup shape — so the Review box must branch on ``run_type`` to render real numbers
    (regression: it read dedup keys → a false "0 to delete · 0 groups / 0 members")."""
    return {
        "id": 3, "name": "Photos", "path": r"E:\Photos2", "kind": "library",
        "enabled": 1, "last_full_scan_at": "2026-07-14T20:00:00",
        "last_scan_at": "2026-07-15T09:00:00",
        "photos": 8600, "videos": 300, "instances": 8900,
        "size_bytes": 41_300_000_000,
        "pending_review": {
            "id": 88, "run_type": "cleanup-perceptual", "stage": 1,
            "created_at": "2026-07-15T11:40:00",
            "counts": {"exact": 4, "perceptual": 11, "network": 4},
        },
        "last_dedup_at": None, "last_cleanup_at": None,
        "running_job": None,
        "queued_jobs": [],
        "last_scan": None,
        "undecodable_current": 0,
        "problem_files": [],
    }
