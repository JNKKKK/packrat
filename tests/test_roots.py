"""roots register: validation, overlap, unique-name, resolution (§8 A1, §11)."""

from __future__ import annotations

import pytest

from packrat import db
from packrat.roots import RootError, register, resolve_dest, resolve_root, root_holder


@pytest.fixture()
def database(packrat_home):
    db.init_db().close()
    conn = db.connect(check_same_thread=False)
    d = db.Database(conn)
    yield d
    d.close()


def test_register_basic(database, tmp_path):
    folder = tmp_path / "iPhone"
    folder.mkdir()
    row = register(database, str(folder))
    assert row["name"] == "iPhone"
    assert row["kind"] == "library"
    assert row["enabled"] == 1
    assert row["last_full_scan_at"] is None


def test_register_missing_path(database, tmp_path):
    with pytest.raises(RootError, match="does not exist"):
        register(database, str(tmp_path / "nope"))


def test_register_not_a_directory(database, tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(RootError, match="not a directory"):
        register(database, str(f))


def test_register_duplicate_path(database, tmp_path):
    folder = tmp_path / "iPhone"
    folder.mkdir()
    register(database, str(folder))
    with pytest.raises(RootError, match="already registered"):
        register(database, str(folder))


def test_register_overlap_nested(database, tmp_path):
    parent = tmp_path / "Backup"
    child = parent / "iPhone"
    child.mkdir(parents=True)
    register(database, str(parent))
    with pytest.raises(RootError, match="overlaps"):
        register(database, str(child))


def test_register_overlap_containing(database, tmp_path):
    parent = tmp_path / "Backup"
    child = parent / "iPhone"
    child.mkdir(parents=True)
    register(database, str(child))
    with pytest.raises(RootError, match="overlaps"):
        register(database, str(parent))


def test_register_leaf_name_collision(database, tmp_path):
    a = tmp_path / "one" / "iPhone"
    b = tmp_path / "two" / "iPhone"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    register(database, str(a))
    with pytest.raises(RootError, match="already in use"):
        register(database, str(b))
    # ...but --name resolves the collision.
    row = register(database, str(b), name="iPhone2")
    assert row["name"] == "iPhone2"


def test_register_name_case_insensitive(database, tmp_path):
    a = tmp_path / "one" / "iPhone"
    a.mkdir(parents=True)
    register(database, str(a))
    b = tmp_path / "two" / "somewhere"
    b.mkdir(parents=True)
    with pytest.raises(RootError, match="already in use"):
        register(database, str(b), name="IPHONE")


def test_register_invalid_kind(database, tmp_path):
    folder = tmp_path / "x"
    folder.mkdir()
    with pytest.raises(RootError, match="invalid kind"):
        register(database, str(folder), kind="bogus")


def test_register_stores_ignore_globs(database, tmp_path):
    folder = tmp_path / "x"
    folder.mkdir()
    row = register(database, str(folder), ignore_globs=["*.tmp", "cache/"])
    import json

    assert json.loads(row["ignore_globs"]) == ["*.tmp", "cache/"]


def test_resolve_by_path_then_name(database, tmp_path):
    folder = tmp_path / "iPhone"
    folder.mkdir()
    row = register(database, str(folder))
    by_path = resolve_root(database, str(folder))
    by_name = resolve_root(database, "iPhone")
    assert by_path["id"] == by_name["id"] == row["id"]
    # case-insensitive handle
    assert resolve_root(database, "IPHONE")["id"] == row["id"]


def test_resolve_unknown(database, tmp_path):
    with pytest.raises(RootError, match="no registered root"):
        resolve_root(database, "ghost")


def test_root_holder_none_by_default(database, tmp_path):
    folder = tmp_path / "x"
    folder.mkdir()
    row = register(database, str(folder))
    assert root_holder(database, row["id"]) is None


def test_root_holder_pending_review(database, tmp_path):
    folder = tmp_path / "x"
    folder.mkdir()
    row = register(database, str(folder))
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, created_at) "
        "VALUES (?, 'dedup', 'pending', '2026-01-01T00:00:00+00:00')",
        (row["id"],),
    )
    holder = root_holder(database, row["id"])
    assert holder is not None and holder["type"] == "review_run"
    assert "dedup pending" in holder["what"]


def test_root_holder_open_merge_and_ignore_merge(database, tmp_path):
    """An open merge_run holds the root — but ignore_merge=True (a resuming merge) skips it (§8 C)."""
    folder = tmp_path / "x"
    folder.mkdir()
    row = register(database, str(folder))
    database.execute(
        "INSERT INTO merge_runs(source_path, dest_path, dest_root_id, status, created_at) "
        "VALUES ('s', ?, ?, 'copying', 't')",
        (str(folder), row["id"]),
    )
    holder = root_holder(database, row["id"])
    assert holder is not None and holder["type"] == "merge_run"
    # A resuming merge must NOT be blocked by its own open run.
    assert root_holder(database, row["id"], ignore_merge=True) is None
    # But a pending review still blocks even with ignore_merge.
    database.execute(
        "INSERT INTO review_runs(root_id, run_type, status, created_at) "
        "VALUES (?, 'dedup', 'pending', 't')",
        (row["id"],),
    )
    assert root_holder(database, row["id"], ignore_merge=True) is not None


# ---------------------------------------------------------------------------
# resolve_dest (merge --into, §8 C Phase 0 step 2) — containment, path-first
# ---------------------------------------------------------------------------
def test_resolve_dest_by_name_and_subfolder(database, tmp_path):
    lib = tmp_path / "Lib"
    lib.mkdir()
    row = register(database, str(lib))
    # By name → the root's own path.
    r, dest = resolve_dest(database, "Lib")
    assert r["id"] == row["id"] and dest == row["path"]
    # By a (not-yet-existing) subfolder path → the containing root + that path.
    sub = lib / "incoming" / "2024"
    r2, dest2 = resolve_dest(database, str(sub))
    assert r2["id"] == row["id"]
    from packrat import fsutil
    assert fsutil.paths_equal(dest2, str(sub))


def test_resolve_dest_no_library_root(database, tmp_path):
    with pytest.raises(RootError, match="under no registered library root"):
        resolve_dest(database, str(tmp_path / "orphan"))


def test_resolve_dest_rejects_trash_root(database, tmp_path):
    trash = tmp_path / "Trash"
    trash.mkdir()
    register(database, str(trash), kind="trash")
    with pytest.raises(RootError, match="library root"):
        resolve_dest(database, "Trash")
    with pytest.raises(RootError, match="library root"):
        resolve_dest(database, str(trash / "sub"))
