"""Daemon HTTP API for M1: /roots register + /scan (in-process TestClient)."""

from __future__ import annotations

import time
import warnings

import pytest

warnings.simplefilter("ignore")

from starlette.testclient import TestClient  # noqa: E402

from packrat.daemon.server import build_app  # noqa: E402

pytest.importorskip("blake3")
pytest.importorskip("PIL")
pytest.importorskip("pdqhash")

TOKEN = "test-token"


@pytest.fixture()
def client(packrat_home):
    app = build_app(TOKEN)
    with TestClient(app) as c:
        yield c


def _h():
    return {"Authorization": f"Bearer {TOKEN}"}


def _wait(client, jid):
    for _ in range(1500):
        d = client.get(f"/jobs/{jid}", headers=_h()).json()
        if d["status"] != "running":
            return d
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_register_root(client, tmp_path):
    folder = tmp_path / "iPhone"
    folder.mkdir()
    r = client.post("/roots", json={"path": str(folder)}, headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["root"]["name"] == "iPhone"
    assert body["job_id"] is None
    # appears in the snapshot
    roots = client.get("/roots", headers=_h()).json()["roots"]
    assert len(roots) == 1 and roots[0]["kind"] == "library"


def test_register_validation_error_is_400(client, tmp_path):
    r = client.post("/roots", json={"path": str(tmp_path / "missing")}, headers=_h())
    assert r.status_code == 400
    assert "does not exist" in r.json()["detail"]


def test_register_with_scan(client, tiny_photos):
    r = client.post("/roots", json={"path": str(tiny_photos), "scan": True}, headers=_h())
    assert r.status_code == 200
    jid = r.json()["job_id"]
    assert jid is not None
    d = _wait(client, jid)
    assert d["status"] == "done"
    snap = client.get("/status", headers=_h()).json()
    assert snap["assets"] == 2  # a.png, b.png (a_copy is a byte-dup)


def test_scan_by_name(client, tiny_photos):
    client.post("/roots", json={"path": str(tiny_photos), "name": "Pics"}, headers=_h())
    r = client.post("/scan", json={"root": "Pics"}, headers=_h())
    assert r.status_code == 200
    d = _wait(client, r.json()["job_id"])
    assert d["status"] == "done"


def test_scan_unknown_root_404(client):
    r = client.post("/scan", json={"root": "ghost"}, headers=_h())
    assert r.status_code == 404


def test_status_root_detail(client, tiny_photos):
    client.post("/roots", json={"path": str(tiny_photos), "name": "Pics", "scan": True}, headers=_h())
    # let the auto-scan finish
    jid = None
    for _ in range(1500):
        js = client.get("/jobs", headers=_h()).json()["jobs"]
        if js and js[0]["status"] != "running":
            break
        time.sleep(0.02)
    d = client.get("/status?root=Pics", headers=_h()).json()["root_detail"]
    assert d["name"] == "Pics"
    assert d["photos"] == 2
