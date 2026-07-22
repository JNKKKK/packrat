"""Job label derivation from type + params (§12)."""

from __future__ import annotations

from packrat.jobs import job_label


def test_scan_labels():
    assert job_label("scan", {"root_id": 1}, root_name="iPhone") == "scan iPhone"
    assert job_label("scan", {"root_id": 1, "full": True}, root_name="iPhone") == "scan iPhone (full)"
    assert job_label("scan", {"full": True, "embed": True}, root_name="iPhone") == "scan iPhone (full · embed)"
    assert job_label("scan", {"dry_run": True}, root_name="iPhone") == "scan iPhone (dry-run)"
    # --all owns no root: qualifier carries "all roots".
    assert job_label("scan", {"all": True}) == "scan (all roots)"
    assert job_label("scan", {"all": True, "full": True}) == "scan (all roots · full)"


def test_dedup_labels():
    assert job_label("dedup", {"root_id": 1}, root_name="P") == "dedup P (analyze)"
    assert job_label("dedup", {"confirm": True}, root_name="P") == "dedup P (confirm)"
    assert job_label("dedup", {"confirm": True, "keep_suggested": True}, root_name="P") \
        == "dedup P (confirm · keep-suggested)"
    assert job_label("dedup", {"cancel": True}, root_name="P") == "dedup P (cancel)"
    assert job_label("dedup", {"dry_run": True}, root_name="P") == "dedup P (dry-run)"


def test_cleanup_labels():
    assert job_label("cleanup", {"mode": "exact"}, root_name="R") == "cleanup R (exact · preview)"
    assert job_label("cleanup", {"mode": "exact", "apply": True}, root_name="R") == "cleanup R (exact · delete)"
    assert job_label("cleanup", {"mode": "exact", "dry_run": True}, root_name="R") == "cleanup R (exact · dry-run)"
    assert job_label("cleanup", {"mode": "undecodable", "apply": True}, root_name="R") \
        == "cleanup R (undecodable · delete)"
    assert job_label("cleanup", {"mode": "perceptual"}, root_name="R") == "cleanup R (perceptual · analyze)"
    assert job_label("cleanup", {"mode": "perceptual", "confirm": True}, root_name="R") \
        == "cleanup R (perceptual · confirm)"
    assert job_label("cleanup", {"cancel": True}, root_name="R") == "cleanup R (perceptual · cancel)"


def test_rootless_labels():
    assert job_label("trash-refresh", {}) == "trash refresh"
    assert job_label("untrash", {"path": r"R:\recovered\IMG.jpg"}) == "untrash IMG.jpg"
    assert job_label("untrash", {"path": r"R:\recovered\2019"}, ) == "untrash 2019"
    assert job_label("untrash", {"path": "/x/y/IMG.jpg", "dry_run": True}) == "untrash IMG.jpg (dry-run)"


def test_trash_refresh_single_root_label():
    # `trash refresh <root>` names the scoped root; the per-root panel drops it.
    assert job_label("trash-refresh", {"root_id": 4}, root_name="_Trash") == "trash refresh _Trash"
    assert job_label("trash-refresh", {"root_id": 4}, root_name="_Trash",
                     include_root=False) == "trash refresh"


def test_merge_labels():
    # merge shows "<src-leaf> → <dest-root>" (§12).
    assert job_label("merge", {"source": r"E:\iphone_dump", "root_id": 1}, root_name="iPhone") \
        == "merge iphone_dump → iPhone"
    assert job_label("merge", {"source": "/tmp/dump", "dry_run": True}, root_name="iPhone") \
        == "merge dump → iPhone (dry-run)"
    # per-root panel drops the dest root name (header already names it).
    assert job_label("merge", {"source": r"E:\dump", "root_id": 1}, root_name="iPhone",
                     include_root=False) == "merge dump →"


def test_per_root_panel_drops_root():
    # In the per-root jobs panel the header already names the root → include_root=False.
    assert job_label("cleanup", {"mode": "exact", "apply": True}, root_name="R", include_root=False) \
        == "cleanup (exact · delete)"
    assert job_label("dedup", {"confirm": True}, root_name="R", include_root=False) == "dedup (confirm)"
