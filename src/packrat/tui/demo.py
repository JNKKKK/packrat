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

# anchor demo timestamps to the same "now"; reuse the fixture job/result builders.
from .data import result_of
from .fixtures import REFERENCE_NOW, _job, _rj


# --- roots: ~33 → the Roots list (§2.1) spans several pages on any terminal ----
# A spread of dot states (◉ deduped / ◐ scanned-only / ○ never) + trash roots,
# several VERY LONG NAS paths (to verify middle-elide + full display on wide) and
# a few LONG root NAMES (to verify the name column), varied counts (for [s] sort).
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
    (12, "Wife_iPhone", r"D:\Backup\Wife_iPhone", "library", 54300, 4100,
     "2026-07-12T20:00:00", "2026-07-11T09:00:00", "2026-07-11T09:00:00"),
    (13, "Screenshots", r"E:\Screenshots", "library", 3200, 0,
     "2026-07-14T12:00:00", None, None),
    (14, "DSLR", r"\\tubie_nas\Res-v2\DSLR\Canon_R6", "library", 22800, 1500,
     "2026-07-08T15:00:00", "2026-07-08T18:00:00", "2026-07-08T15:00:00"),
    (15, "WhatsApp", r"D:\Backup\WhatsApp\Media", "library", 9700, 2300,
     "2026-07-13T10:00:00", None, None),
    (16, "Scanner", r"E:\import\FlatbedScanner", "library", 410, 0,
     None, None, None),                                  # ○ never scanned
    (17, "Camcorder", r"\\tubie_nas\Res-v2\Camcorder\MiniDV_caps", "library", 12, 880,
     "2026-07-05T09:00:00", "2026-07-05T11:00:00", "2026-07-05T09:00:00"),
    (18, "Instagram", r"D:\export\Instagram_2026", "library", 1840, 260,
     "2026-07-14T22:00:00", None, None),
    (19, "iPad", r"D:\Backup\iPad_Air", "library", 7600, 540,
     "2026-07-09T08:00:00", "2026-07-09T10:00:00", None),
    (20, "_Trash2", r"E:\_Trash", "trash", 0, 0, None, None, None),
    (21, "Kids_tablet", r"D:\Backup\Fire_HD", "library", 2100, 3300,
     "2026-07-07T19:00:00", None, None),
    # --- long NAS paths (verify middle-elide when narrow, full text when wide) ---
    (22, "NAS_Media_Archive", r"\\tubie_nas\Res-v2\Media\Photography\Archive\2010-2014\originals\raw",
     "library", 88400, 9200, "2026-07-04T08:00:00", "2026-07-04T20:00:00", "2026-07-04T08:00:00"),
    (23, "Synology_Backup", r"\\synology-ds920.local\home\Backups\Devices\iPhone15Pro\DCIM\Camera",
     "library", 33100, 5400, "2026-07-03T09:00:00", None, None),
    (24, "Cloud_Sync_Folder", r"C:\Users\nk\OneDrive - Personal\Pictures\Camera Roll\Auto Upload",
     "library", 12600, 700, "2026-07-14T06:00:00", "2026-07-13T23:00:00", None),
    (25, "ExternalSSD_Videos", r"F:\Video Projects\2026\Family Events\Wedding\Ceremony\4K_ProRes",
     "library", 40, 2100, "2026-07-02T10:00:00", None, "2026-07-02T10:00:00"),
    (26, "Photobooth_Exports", r"\\tubie_nas\Res-v2\Events\CorporateParty2026\PhotoboothExports\hires",
     "library", 5600, 120, None, None, None),           # ○ never scanned, long path
    # --- long ROOT NAMES (verify the name column) ---
    (27, "GrandparentsSharedAlbum", r"D:\Shared\Grandparents", "library", 4200, 380,
     "2026-07-01T12:00:00", None, None),
    (28, "iPhone_Portrait_Mode_Only", r"D:\Backup\iPhone\Portrait", "library", 3100, 0,
     "2026-07-12T15:00:00", "2026-07-12T17:00:00", None),
    (29, "Screenshots_and_Memes_2026", r"E:\Screenshots\2026", "library", 8800, 40,
     "2026-07-11T09:00:00", None, None),
    # --- more ordinary roots to bulk out the list ---
    (30, "Nikon_Z6", r"E:\Cameras\Nikon_Z6", "library", 18400, 2600,
     "2026-07-10T08:00:00", "2026-07-10T12:00:00", "2026-07-10T08:00:00"),
    (31, "Sony_A7", r"E:\Cameras\Sony_A7IV", "library", 27200, 3300,
     "2026-07-09T14:00:00", None, "2026-07-09T14:00:00"),
    (32, "Dashcam", r"D:\Dashcam\front", "library", 30, 14200,
     "2026-07-08T22:00:00", None, None),
    (33, "_Trash_NAS", r"\\tubie_nas\Res-v2\_recycle", "trash", 0, 0, None, None, None),
    # --- CJK name + path (verify wide-char display width doesn't break layout) ---
    (34, "手机相册", r"D:\备份\手机相册\2026年家庭照片", "library", 6800, 940,
     "2026-07-13T08:00:00", "2026-07-13T12:00:00", None),
]


# Roots (by id) where a probe found unscanned files → ◐ grey "new files probed" (§12
# 4-state dot). {id: new_count}; every other library root probed clean (count 0). Picked
# to span the cases: a scanned+deduped root (13 Screenshots) that got new drops, and a
# never-scanned root (8 Scans) whose first probe found files (◐, NOT ○ — outranks never).
_PROBE_NEW = {13: 128, 8: 640, 23: 54}


def _root(spec) -> dict:
    rid, name, path, kind, photos, videos, scan, dedup, full = spec
    # Synthesize a plausible on-disk size (~4 MB/photo, ~60 MB/video) so the demo's
    # size column shows varied realistic values; trash roots are empty.
    size_bytes = 0 if kind == "trash" else photos * 4_000_000 + videos * 60_000_000
    probe_new = 0 if kind == "trash" else _PROBE_NEW.get(rid, 0)
    # Dedup-dirty flag (§12 rung 3): the spec's scan/dedup timestamps used to encode the
    # green/yellow split via a recency compare; the ladder now uses this event flag, so
    # derive it from the SAME relationship to preserve the demo's dot variety — a root
    # scanned AFTER its last dedup is dirty (needs_dedup=1). A never-deduped root falls to
    # yellow via the ladder's "never deduped" branch regardless, so its flag stays 0.
    needs_dedup = 1 if (dedup and scan and scan > dedup) else 0
    return {
        "id": rid, "name": name, "path": path, "kind": kind, "enabled": 1,
        "last_full_scan_at": full,
        "last_probe_at": None if kind == "trash" else "2026-07-15T12:45:00",
        "probe_new_count": probe_new, "needs_dedup": needs_dedup,
        "asset_count": photos + videos, "photos": photos, "videos": videos,
        "instance_count": photos + videos, "size_bytes": size_bytes,
        "last_scan_at": scan, "last_dedup_at": dedup,
    }


ROOTS = [_root(s) for s in _ROOT_SPECS]


# --- running job + a deep queue backlog (§4 spans pages) ----------------------
RUNNING = _job(
    id=612, type="scan", root_id=6, status="running", total=45000, done=17800,
    started_at="2026-07-15T13:10:00", params_json=_rj({"root_id": 6, "full": True}),
    root_name="Archive", label="scan Archive (full)", _eta_s=1560,
)

# ~18 queued jobs → the dashboard preview truncates ("… N more") and the maximized
# Queue pages; a mix of runnable ("waiting for worker") and blocked rows.
_PHOTOS_HOLD = {"run_type": "dedup", "what": "dedup pending since 2026-07-15T08:00:00"}
_QUEUE_SPECS = [
    (613, "merge", 2, {"source": r"E:\iphone_dump", "into": "Camera"},
     "merge iphone_dump → Camera", None),
    (614, "scan", 3, {"root_id": 3}, "scan Photos", _PHOTOS_HOLD),
    (615, "dedup", 3, {"root_id": 3, "confirm": True}, "dedup Photos (confirm)", _PHOTOS_HOLD),
    (616, "scan", 7, {"root_id": 7}, "scan GoPro", None),
    (617, "scan", 10, {"root_id": 10, "full": True}, "scan OldPhone (full)", None),
    (618, "cleanup", 1, {"root_id": 1, "mode": "exact", "apply": True},
     "cleanup iPhone (exact · delete)", None),
    (619, "merge", 6, {"source": r"E:\import\SDCard_2026", "into": "Archive"},
     "merge SDCard_2026 → Archive", None),
    (620, "dedup", 2, {"root_id": 2}, "dedup Camera (analyze)", None),
    (621, "scan", 9, {"root_id": 9}, "scan Drone", None),
    (622, "scan", 22, {"root_id": 22, "full": True}, "scan NAS_Media_Archive (full)", None),
    (623, "scan", 23, {"root_id": 23}, "scan Synology_Backup", None),
    (624, "dedup", 30, {"root_id": 30}, "dedup Nikon_Z6 (analyze)", None),
    (625, "merge", 24, {"source": r"E:\import\phone_export", "into": "Cloud_Sync_Folder"},
     "merge phone_export → Cloud_Sync_Folder", None),
    (626, "scan", 31, {"root_id": 31}, "scan Sony_A7", None),
    (627, "cleanup", 29, {"root_id": 29, "mode": "undecodable", "apply": True},
     "cleanup Screenshots_and_Memes_2026 (undecodable · delete)", None),
    (628, "scan", 32, {"root_id": 32, "full": True}, "scan Dashcam (full)", None),
    (629, "dedup", 12, {"root_id": 12, "confirm": True}, "dedup Wife_iPhone (confirm)", None),
    (630, "scan", 26, {"root_id": 26}, "scan Photobooth_Exports", None),
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
    # --- more history so Recent spans several pages -------------------------
    _job(id=599, type="scan", root_id=22, status="done", total=97600, done=97600,
         finished_at="2026-07-04T08:00:00", params_json=_rj({"root_id": 22, "full": True}),
         root_name="NAS_Media_Archive", label="scan NAS_Media_Archive (full)",
         result_json=_rj({"op": "scan", "new": 97600, "summary": "+97,600 new"})),
    _job(id=598, type="dedup", root_id=22, status="done", finished_at="2026-07-04T20:00:00",
         params_json=_rj({"root_id": 22, "confirm": True}), root_name="NAS_Media_Archive",
         label="dedup NAS_Media_Archive (confirm)",
         result_json=_rj({"op": "dedup", "action": "confirm", "review_status": "completed",
                          "summary": "1,204 deleted · 88 spared"})),
    _job(id=597, type="cleanup", root_id=1, status="done", finished_at="2026-07-03T16:00:00",
         params_json=_rj({"root_id": 1, "mode": "undecodable", "apply": True}), root_name="iPhone",
         label="cleanup iPhone (undecodable · delete)",
         result_json=_rj({"op": "cleanup", "mode": "undecodable", "action": "delete",
                          "deleted": 7, "summary": "7 deleted (marked trashed)"})),
    _job(id=596, type="scan", root_id=30, status="done", total=21000, done=21000,
         finished_at="2026-07-10T08:00:00", params_json=_rj({"root_id": 30}), root_name="Nikon_Z6",
         label="scan Nikon_Z6", result_json=_rj({"op": "scan", "new": 340, "summary": "+340 new"})),
    _job(id=595, type="merge", root_id=24, status="done", finished_at="2026-07-13T23:00:00",
         params_json=_rj({"source": r"E:\import\onedrive_dump", "into": "Cloud_Sync_Folder"}),
         root_name="Cloud_Sync_Folder", label="merge onedrive_dump → Cloud_Sync_Folder",
         result_json=_rj({"op": "merge", "source": r"E:\import\onedrive_dump",
                          "dest_root": "Cloud_Sync_Folder", "new": 512, "exact_known": 88,
                          "summary": "512 copied · 88 exact-known"})),
    _job(id=594, type="trash-refresh", status="done", finished_at="2026-07-12T07:00:00",
         label="trash refresh",
         result_json=_rj({"op": "trash-refresh", "roots": 2, "new_trashed": 34, "flipped": 2,
                          "already_trashed": 5, "emptied": 41, "undeletable": 1, "errors": 0,
                          "summary": "34 new trashed · 2 flipped · 41 emptied"})),
    _job(id=593, type="scan", root_id=31, status="interrupted", finished_at="2026-07-09T14:30:00",
         error="daemon restarted", params_json=_rj({"root_id": 31}), root_name="Sony_A7",
         label="scan Sony_A7"),
    _job(id=592, type="dedup", root_id=14, status="done", finished_at="2026-07-08T18:00:00",
         params_json=_rj({"root_id": 14}), root_name="DSLR", label="dedup DSLR (analyze)",
         result_json=_rj({"op": "dedup", "action": "analyze",
                          "summary": "already clean — no exact duplicates or near-dup groups"})),
    _job(id=591, type="scan", root_id=32, status="done", total=14400, done=14400,
         finished_at="2026-07-08T22:00:00", params_json=_rj({"root_id": 32}), root_name="Dashcam",
         label="scan Dashcam",
         result_json=_rj({"op": "scan", "new": 14201, "undecodable": 14, "read_errors": 3,
                          "summary": "+14,201 new · 14 undecodable · 3 read errors"})),
    _job(id=590, type="untrash", status="done", finished_at="2026-07-07T11:00:00",
         params_json=_rj({"path": r"R:\recovered\2019\batch"}), label="untrash batch",
         result_json=_rj({"op": "untrash", "untrashed": 18, "forgotten": 4, "already_active": 2,
                          "unknown": 1, "errors": 0,
                          "summary": "18 reactivated · 4 forgotten · 2 active · 1 unknown"})),
    _job(id=589, type="merge", root_id=6, status="error", finished_at="2026-07-06T13:00:00",
         error="source path not readable: \\\\tubie_nas\\Res-v2\\incoming (timed out)",
         params_json=_rj({"source": r"\\tubie_nas\Res-v2\incoming", "into": "Archive"}),
         root_name="Archive", label="merge incoming → Archive"),
    _job(id=588, type="scan", root_id=27, status="done", total=4600, done=4600,
         finished_at="2026-07-01T12:00:00", params_json=_rj({"root_id": 27}),
         root_name="GrandparentsSharedAlbum", label="scan GrandparentsSharedAlbum",
         result_json=_rj({"op": "scan", "new": 4580, "summary": "+4,580 new"})),
]


def status_snapshot(*, running: bool = True) -> dict:
    """A ``status_snapshot()``-shaped dict for the demo (a job runs by default)."""
    return {
        "assets": sum(r["asset_count"] for r in ROOTS),
        "photos": sum(r["photos"] for r in ROOTS),
        "videos": sum(r["videos"] for r in ROOTS),
        "trashed": 3904,
        "size_bytes": sum(r["size_bytes"] for r in ROOTS),
        "lifetime_deduped": 14027,
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


def job_problem_files(job_id: int) -> list[dict]:
    """Synthesize a scan job's problem-file list from its recorded counts.

    Mirrors ``queries.job_problem_files``: one entry per undecodable / read-error the
    job's ``result_json`` claims, so the count summary and the list agree (and a job
    with many gives the card enough rows to exercise ↑/↓ scrolling)."""
    job = next((j for j in RECENT if j["id"] == job_id), None)
    if job is None:
        return []
    r = result_of(job)
    if r.get("op") != "scan":
        return []
    root = job.get("root_name") or "root"
    _EXT = {"photo": ("HEIC", "jpg", "png"), "video": ("3gp", "mov", "avi")}
    _WHY = {
        "photo": "PIL: cannot identify image file",
        "video": "PyAV: no decodable video stream",
    }
    out: list[dict] = []
    for i in range(r.get("undecodable", 0)):
        mt = "photo" if i % 3 else "video"
        out.append({
            "path": rf"D:\Backup\{root}\2019\batch{i // 5:02d}\IMG_{4000 + i}.{_EXT[mt][i % 3]}",
            "media_type": mt, "problem": "undecodable",
            "detail": _WHY[mt] if i % 2 == 0 else None,
        })
    for i in range(r.get("read_errors", 0)):
        out.append({
            "path": rf"\\tubie_nas\Res-v2\{root}\corrupt\clip_{i}.mov",
            "media_type": "video", "problem": "read-error",
            "detail": "OSError: [Errno 5] I/O error (SMB timeout)",
        })
    return out


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
    if name == "Photos":                       # the actionable pending-review case (dedup)
        pending = {"id": 77, "run_type": "dedup", "stage": 2,
                   "created_at": "2026-07-15T08:00:00",
                   "counts": {"to_delete_exact": 240, "groups": 18, "members": 47}}
    elif name == "Camera":                      # a pending cleanup --trash-perceptual review
        pending = {"id": 88, "run_type": "cleanup-perceptual", "stage": 1,
                   "created_at": "2026-07-15T11:40:00",
                   "counts": {"exact": 4, "perceptual": 11, "network": 0}}
    # This root's slice of the backlog (with blocked reasons) → the Jobs panel's
    # Queued section; Photos has two waiting behind its pending review.
    queued = [dict(j) for j in QUEUED if j.get("root_id") == r["id"]]
    return {
        "id": r["id"], "name": r["name"], "path": r["path"], "kind": r["kind"],
        "enabled": 1, "last_full_scan_at": r["last_full_scan_at"],
        "last_probe_at": r.get("last_probe_at"), "probe_new_count": r.get("probe_new_count", 0),
        "needs_dedup": r.get("needs_dedup", 0),
        "last_scan_at": r["last_scan_at"],
        "photos": r["photos"], "videos": r["videos"], "instances": r["instance_count"],
        "size_bytes": r["size_bytes"],
        "pending_review": pending,
        "last_dedup_at": r["last_dedup_at"], "last_cleanup_at": None,
        "running_job": dict(RUNNING) if name == "Archive" else None,
        "queued_jobs": queued,
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
