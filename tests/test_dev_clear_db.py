"""Dev-only `clear-db`: build gate, catalog wipe, schema preservation, job refusal."""

from __future__ import annotations

import time
import warnings

import pytest

from packrat import build, db
from packrat.db.schema import SCHEMA_VERSION

warnings.simplefilter("ignore")

from starlette.testclient import TestClient  # noqa: E402

from packrat.daemon.server import build_app  # noqa: E402

TOKEN = "test-token"


# ---------------------------------------------------------------------------
# build-mode detection
# ---------------------------------------------------------------------------
def test_env_override_enables(monkeypatch):
    monkeypatch.setenv("PACKRAT_DEV", "1")
    assert build.is_dev_build() is True


def test_env_override_disables(monkeypatch):
    # Even in a source checkout, PACKRAT_DEV=0 force-disables dev mode.
    monkeypatch.setenv("PACKRAT_DEV", "0")
    assert build.is_dev_build() is False


def test_source_checkout_detected(monkeypatch):
    monkeypatch.delenv("PACKRAT_DEV", raising=False)
    # The test suite runs from the source tree, so the heuristic is True here.
    assert build.is_dev_build() is True


# ---------------------------------------------------------------------------
# clear_catalog (DB layer)
# ---------------------------------------------------------------------------
@pytest.fixture()
def database(packrat_home):
    db.init_db().close()
    conn = db.connect(check_same_thread=False)
    d = db.Database(conn)
    yield d
    d.close()


def test_clear_catalog_empties_but_preserves_schema(database, tmp_path):
    from packrat.roots import register

    folder = tmp_path / "iPhone"
    folder.mkdir()
    register(database, str(folder))
    database.execute(
        "INSERT INTO assets(content_hash, media_type, status, added_at) "
        "VALUES ('abc', 'photo', 'active', '2026-01-01')"
    )
    assert database.query_one("SELECT COUNT(*) c FROM roots")["c"] == 1
    assert database.query_one("SELECT COUNT(*) c FROM assets")["c"] == 1

    counts = database.clear_catalog()
    assert counts.get("roots") == 1 and counts.get("assets") == 1
    assert database.query_one("SELECT COUNT(*) c FROM roots")["c"] == 0
    assert database.query_one("SELECT COUNT(*) c FROM assets")["c"] == 0
    # schema_version (in meta) survives — the DB stays usable with no re-init.
    sv = database.query_one("SELECT value FROM meta WHERE key='schema_version'")
    assert sv is not None and int(sv["value"]) == SCHEMA_VERSION
    # DB still works after the wipe; ids restart at 1 (sqlite_sequence reset).
    row = register(database, str(folder))
    assert row["id"] == 1


def test_clear_catalog_counts_cascaded_children(database, tmp_path):
    """Cascaded rows (phash under assets) are counted before the delete, not zeroed."""
    from packrat.roots import register

    folder = tmp_path / "r"
    folder.mkdir()
    r = register(database, str(folder))
    database.execute(
        "INSERT INTO assets(id, content_hash, media_type, status) VALUES (1,'h','photo','active')"
    )
    database.execute(
        "INSERT INTO file_instances(asset_id, root_id, path) VALUES (1, ?, 'p')", (r["id"],)
    )
    database.execute("INSERT INTO phash(asset_id, algo, bits) VALUES (1,'pdq',X'00')")
    counts = database.clear_catalog()
    # phash/file_instances would cascade-delete when assets is cleared; their
    # pre-delete counts must still be reported (regression for the FK-count bug).
    assert counts.get("phash") == 1
    assert counts.get("file_instances") == 1


# ---------------------------------------------------------------------------
# /dev/clear-db endpoint (dev-gated)
# ---------------------------------------------------------------------------
@pytest.fixture()
def dev_client(packrat_home, monkeypatch):
    monkeypatch.setenv("PACKRAT_DEV", "1")  # force the route to register
    app = build_app(TOKEN)
    with TestClient(app) as c:
        yield c


def _h():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_endpoint_clears(dev_client, tmp_path):
    folder = tmp_path / "iPhone"
    folder.mkdir()
    dev_client.post("/roots", json={"path": str(folder)}, headers=_h())
    assert len(dev_client.get("/roots", headers=_h()).json()["roots"]) == 1
    r = dev_client.post("/dev/clear-db", headers=_h())
    assert r.status_code == 200
    assert r.json()["total_rows"] >= 1
    assert dev_client.get("/roots", headers=_h()).json()["roots"] == []


def test_endpoint_refuses_while_job_running(dev_client):
    dev_client.post("/jobs", json={"type": "sleeper", "params": {"steps": 50, "delay_s": 0.05}}, headers=_h())
    r = dev_client.post("/dev/clear-db", headers=_h())
    assert r.status_code == 409
    assert "job is running" in r.json()["detail"]


def test_endpoint_absent_in_release_build(packrat_home, monkeypatch):
    monkeypatch.setenv("PACKRAT_DEV", "0")  # force release mode
    app = build_app(TOKEN)
    with TestClient(app) as c:
        r = c.post("/dev/clear-db", headers=_h())
        assert r.status_code == 404  # route never registered
