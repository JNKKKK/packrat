"""Rich sample data for the OFFLINE demo TUI (``packrat --offline``).

Distinct from :mod:`packrat.tui.fixtures` on purpose: ``fixtures`` mirrors the doc
mockups **exactly** (5 roots, specific numbers) so the golden-frame tests stay
byte-stable and can't drift. ``demo`` instead maximizes coverage — enough roots,
queued jobs, and per-root history to fill **more than one page** everywhere a list
paginates, plus every terminal job shape (done / pending-review / already-clean /
error / interrupted / trash-refresh / untrash) so a person can open the demo and
exercise every screen and action.

Same query-shaped dicts as ``queries.py`` (so the same pure builders render it),
anchored to :data:`packrat.tui.fixtures.REFERENCE_NOW` for deterministic times.
"""

from __future__ import annotations

import json

from .fixtures import REFERENCE_NOW  # anchor demo timestamps to the same "now"


def _rj(d: dict) -> str:
    return json.dumps(d)


def _job(**kw) -> dict:
    base = {
        "id": 0, "type": "scan", "root_id": None, "status": "done",
        "total": None, "done": 0, "enqueued_at": None, "started_at": None,
        "finished_at": None, "error": None, "result_json": None,
        "params_json": "{}", "root_name": None, "label": "",
    }
    base.update(kw)
    return base


# --- roots: 11 of them → the Roots list (§2.1, 5 rows/page) spans 3 pages -----
# A spread of dot states (◉ deduped / ◐ scanned-only / ○ never) + a trash root +
# a long NAS path (to show middle-elide) + varied counts (to show the [s] sort).
_ROOT_SPECS = [
    # id, name, path, kind, photos, videos, last_scan_at, last_dedup_at, last_full
    (1, "iPhone", r"D:\Backup\iPhone", "library", 92110, 6302,
     "2026-07-15T09:04:00", "2026-07-15T11:31:00", "2026-07-10T10:00:00"),
    (2, "Camera", r"E:\Photos", "library", 25900, 250,
     "2026-07-14T09:31:00", "2026-07-12T15:00:00", "2026-07-08T08:00:00"),
    (3, "Photos", r"E:\Photos2", "library", 8600, 300,
     "2026-07-15T09:00:00", None, "2026-07-14T20:00:00"),
    (4, "_Trash", r"D:\Backup\_Trash", "trash", 0, 0, None, None, None),
    (5, "Downloads", r"D:\dump", "library", 200, 41,
     "2026-07-15T11:02:00", None, None),
    (6, "Archive", r"\\tubie_nas\Res-v2\PhotoArchive\2015-2019", "library", 41200, 3800,
     "2026-07-13T22:00:00", "2026-07-09T12:00:00", "2026-07-09T12:00:00"),
    (7, "GoPro", r"E:\Action\GoPro", "library", 1200, 9400,
     "2026-07-11T18:00:00", None, None),
    (8, "Scans", r"D:\Documents\FilmScans", "library", 6400, 0,
     None, None, None),                                  # ○ never scanned
    (9, "Drone", r"\\tubie_nas\Res-v2\Aerial\Mavic3\raw-and-proxies", "library", 800, 5200,
     "2026-07-10T14:00:00", "2026-07-10T16:00:00", None),
    (10, "OldPhone", r"D:\Backup\Galaxy_S9", "library", 15300, 900,
     "2026-07-06T08:00:00", "2026-07-06T10:00:00", "2026-07-06T08:00:00"),
    (11, "SDCard", r"E:\import\SDCard_2026", "library", 0, 0,
     None, None, None),                                  # ○ freshly registered
]


def _root(spec) -> dict:
    rid, name, path, kind, photos, videos, scan, dedup, full = spec
    return {
        "id": rid, "name": name, "path": path, "kind": kind, "enabled": 1,
        "last_full_scan_at": full,
        "asset_count": photos + videos, "photos": photos, "videos": videos,
        "instance_count": photos + videos, "last_scan_at": scan, "last_dedup_at": dedup,
    }


ROOTS = [_root(s) for s in _ROOT_SPECS]


# --- running job + a deep queue backlog (§4 spans pages) ----------------------
RUNNING = _job(
    id=612, type="scan", root_id=6, status="running", total=45000, done=17800,
    started_at="2026-07-15T13:10:00", params_json=_rj({"root_id": 6, "full": True}),
    root_name="Archive", label="scan Archive (full)", _eta_s=1560,
)

# 9 queued jobs → the dashboard preview truncates ("… N more") and the maximized
# Queue pages; a mix of runnable ("waiting for worker") and blocked rows.
_QUEUE_SPECS = [
    (613, "merge", 2, {"source": r"E:\iphone_dump", "into": "Camera"},
     "merge iphone_dump → Camera", None),
    (614, "scan", 3, {"root_id": 3},
     "scan Photos", {"run_type": "dedup", "what": "dedup pending since 2026-07-15T08:00:00"}),
    (615, "dedup", 3, {"root_id": 3, "confirm": True},
     "dedup Photos (confirm)", {"run_type": "dedup", "what": "dedup pending since 2026-07-15T08:00:00"}),
    (616, "scan", 7, {"root_id": 7}, "scan GoPro", None),
    (617, "scan", 10, {"root_id": 10, "full": True}, "scan OldPhone (full)", None),
    (618, "cleanup", 1, {"root_id": 1, "mode": "exact", "apply": True},
     "cleanup iPhone (exact · delete)", None),
    (619, "merge", 6, {"source": r"E:\import\SDCard_2026", "into": "Archive"},
     "merge SDCard_2026 → Archive", None),
    (620, "dedup", 2, {"root_id": 2}, "dedup Camera (analyze)", None),
    (621, "scan", 9, {"root_id": 9}, "scan Drone", None),
]


def _queued(spec) -> dict:
    jid, typ, rid, params, label, blocked = spec
    return _job(id=jid, type=typ, root_id=rid, status="queued",
                enqueued_at="2026-07-15T13:05:00", params_json=_rj(params),
                root_name=label.split()[1] if len(label.split()) > 1 else None,
                label=label, blocked=blocked)


QUEUED = [_queued(s) for s in _QUEUE_SPECS]


# --- recent/terminal jobs — one of every shape (§5 card coverage) -------------
RECENT = [
    _job(id=611, type="dedup", root_id=3, status="done", finished_at="2026-07-15T11:48:00",
         params_json=_rj({"root_id": 3, "confirm": True}), root_name="Photos",
         label="dedup Photos (confirm)",
         result_json=_rj({"op": "dedup", "action": "confirm", "review_status": "completed",
                          "stage": 3, "to_delete_exact": 12, "groups": 0, "members": 0,
                          "summary": "52 deleted (12 exact · 40 near-dup) · 9 spared"})),
    _job(id=610, type="cleanup", root_id=1, status="done", finished_at="2026-07-15T10:20:00",
         params_json=_rj({"root_id": 1, "mode": "exact", "apply": True}), root_name="iPhone",
         label="cleanup iPhone (exact · delete)",
         result_json=_rj({"op": "cleanup", "mode": "exact", "action": "delete", "deleted": 3,
                          "already_gone": 0, "summary": "3 deleted"})),
    _job(id=609, type="scan", root_id=2, status="done", total=26150, done=26150,
         finished_at="2026-07-15T09:31:00", params_json=_rj({"root_id": 2}), root_name="Camera",
         label="scan Camera",
         result_json=_rj({"op": "scan", "new": 26, "exact_dup": 0, "backfilled": 0,
                          "matches_trashed": 0, "undecodable": 0, "read_errors": 0,
                          "skipped_fastpath": 26124, "deleted_instances": 0, "forgotten_assets": 0,
                          "summary": "+26 new"})),
    _job(id=608, type="merge", root_id=1, status="done", finished_at="2026-07-14T22:10:00",
         params_json=_rj({"source": r"E:\iphone_dump", "into": "iPhone"}), root_name="iPhone",
         label="merge iphone_dump → iPhone",
         result_json=_rj({"op": "merge", "source": r"E:\iphone_dump", "dest_root": "iPhone",
                          "new": 240, "exact_known": 18, "trashed": 1, "dup_in_source": 6,
                          "collisions": 2, "unindexed": 0, "errors": 0,
                          "summary": "240 copied · 18 exact-known"})),
    _job(id=607, type="trash-refresh", status="done", finished_at="2026-07-14T20:00:00",
         label="trash refresh",
         result_json=_rj({"op": "trash-refresh", "roots": 1, "new_trashed": 9, "flipped": 3,
                          "already_trashed": 1, "emptied": 12, "undeletable": 0, "errors": 0,
                          "summary": "9 new trashed · 3 flipped · 1 known · 12 emptied"})),
    _job(id=606, type="untrash", status="done", finished_at="2026-07-14T18:00:00",
         params_json=_rj({"path": r"R:\recovered\IMG_4471.jpg"}), label="untrash IMG_4471.jpg",
         result_json=_rj({"op": "untrash", "untrashed": 1, "forgotten": 0, "already_active": 0,
                          "unknown": 0, "errors": 0,
                          "summary": "1 reactivated · 0 forgotten · 0 active · 0 unknown"})),
    _job(id=605, type="dedup", root_id=6, status="done", finished_at="2026-07-09T12:00:00",
         params_json=_rj({"root_id": 6}), root_name="Archive", label="dedup Archive (analyze)",
         result_json=_rj({"op": "dedup", "action": "analyze",
                          "summary": "already clean — no exact duplicates or near-dup groups"})),
    _job(id=604, type="cleanup", root_id=3, status="error", finished_at="2026-07-13T10:00:00",
         error="nothing to confirm; run `dedup Photos` first.",
         params_json=_rj({"root_id": 3, "mode": "perceptual", "confirm": True}), root_name="Photos",
         label="cleanup Photos (perceptual · confirm)"),
    _job(id=603, type="scan", root_id=1, status="interrupted", finished_at="2026-07-13T09:00:00",
         error="daemon restarted", params_json=_rj({"root_id": 1}), root_name="iPhone",
         label="scan iPhone"),
    _job(id=602, type="scan", root_id=10, status="done", total=16200, done=16200,
         finished_at="2026-07-06T08:00:00", params_json=_rj({"root_id": 10}), root_name="OldPhone",
         label="scan OldPhone",
         result_json=_rj({"op": "scan", "new": 16200, "summary": "+16,200 new"})),
    _job(id=601, type="dedup", root_id=10, status="done", finished_at="2026-07-06T10:00:00",
         params_json=_rj({"root_id": 10, "confirm": True}), root_name="OldPhone",
         label="dedup OldPhone (confirm)",
         result_json=_rj({"op": "dedup", "action": "confirm", "review_status": "completed",
                          "summary": "18 deleted · 4 spared"})),
    _job(id=600, type="merge", root_id=2, status="done", finished_at="2026-07-05T14:00:00",
         params_json=_rj({"source": r"D:\old_card", "into": "Camera"}), root_name="Camera",
         label="merge old_card → Camera",
         result_json=_rj({"op": "merge", "source": r"D:\old_card", "dest_root": "Camera",
                          "new": 1204, "exact_known": 12, "summary": "1,204 copied · 12 exact-known"})),
]


def status_snapshot(*, running: bool = True) -> dict:
    """A ``status_snapshot()``-shaped dict for the demo (a job runs by default)."""
    return {
        "assets": sum(r["asset_count"] for r in ROOTS),
        "photos": sum(r["photos"] for r in ROOTS),
        "videos": sum(r["videos"] for r in ROOTS),
        "trashed": 3904,
        "running": dict(RUNNING) if running else None,
        "queued": [dict(j) for j in QUEUED],
        "interrupted": [],
        "pending_reviews": [
            {"id": 77, "root_id": 3, "run_type": "dedup", "stage": 2,
             "created_at": "2026-07-15T08:00:00",
             "counts": {"to_delete_exact": 0, "groups": 18, "members": 47}, "root_name": "Photos"},
        ],
        "roots": [dict(r) for r in ROOTS],
    }


def recent_jobs() -> list[dict]:
    return [dict(j) for j in RECENT]


def queued_jobs() -> list[dict]:
    return [dict(j) for j in QUEUED]


# --- per-root detail + a long jobs history (§3 jobs list spans pages) ---------
def _root_by_name(name: str) -> dict | None:
    for r in ROOTS:
        if r["name"] == name:
            return r
    return None


def root_detail(name: str) -> dict | None:
    """A ``root_detail()``-shaped dict for demo root ``name`` (or None)."""
    r = _root_by_name(name)
    if r is None:
        return None
    pending = None
    if name == "Photos":                       # the actionable pending-review case
        pending = {"id": 77, "run_type": "dedup", "stage": 2,
                   "created_at": "2026-07-15T08:00:00",
                   "counts": {"to_delete_exact": 240, "groups": 18, "members": 47}}
    return {
        "id": r["id"], "name": r["name"], "path": r["path"], "kind": r["kind"],
        "enabled": 1, "last_full_scan_at": r["last_full_scan_at"],
        "last_scan_at": r["last_scan_at"],
        "photos": r["photos"], "videos": r["videos"], "instances": r["instance_count"],
        "pending_review": pending,
        "last_dedup_at": r["last_dedup_at"], "last_cleanup_at": None,
        "running_job": dict(RUNNING) if name == "Archive" else None,
        "queued_jobs": [],
        "last_scan": None,
        "undecodable_current": 0,
        "problem_files": [],
    }


def root_jobs(name: str) -> list[dict]:
    """A per-root job history for demo root ``name`` — long enough to paginate."""
    r = _root_by_name(name)
    if r is None:
        return []
    rid = r["id"]
    # Real per-root rows for the two data-rich roots; a synthesized history otherwise
    # so every root's detail Jobs list has enough rows to page (§3, 4 rows/page).
    mine = [dict(j) for j in RECENT if j.get("root_id") == rid]
    if name == "Photos":
        mine = [dict(_pending_analyze())] + mine
    # Pad with older synthetic scans so the list spans >1 page.
    for k in range(6):
        mine.append(_job(
            id=500 - k * 3 - rid, type="scan", root_id=rid, status="done",
            finished_at=f"2026-06-{28 - k:02d}T08:00:00", root_name=name, label=f"scan {name}",
            result_json=_rj({"op": "scan", "new": 100 + k, "summary": f"+{100 + k} new"})))
    return mine


def _pending_analyze() -> dict:
    return _job(id=430, type="dedup", root_id=3, status="done",
                started_at="2026-07-15T11:31:00", finished_at="2026-07-15T11:31:00",
                params_json=_rj({"root_id": 3}), root_name="Photos",
                label="dedup Photos (analyze)",
                result_json=_rj({"op": "dedup", "action": "analyze", "review_status": "pending",
                                 "stage": 2, "to_delete_exact": 0, "groups": 18, "members": 47,
                                 "summary": "stage 2 · 18 groups / 47 members"}))
