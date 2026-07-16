"""Daemon HTTP API for M4: /cleanup, /cleanup/preview, /trash/refresh, /untrash.

In-process TestClient (never binds the port), mirroring test_api_roots.py. Verifies
the HTTP surface — routing, validation (trash root → 400), param passthrough — not
the full delete behavior (that's tests/test_cleanup.py against the handler).
"""

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


def test_cleanup_rejects_trash_root_400(client, tmp_path):
    trash = tmp_path / "Trash"
    trash.mkdir()
    client.post("/roots", json={"path": str(trash), "kind": "trash"}, headers=_h())
    r = client.post("/cleanup", json={"root": "Trash"}, headers=_h())
    assert r.status_code == 400
    assert "library root" in r.json()["detail"]


def test_cleanup_unknown_root_404(client):
    r = client.post("/cleanup", json={"root": "ghost"}, headers=_h())
    assert r.status_code == 404


def test_cleanup_preview_endpoint(client, tiny_photos):
    client.post("/roots", json={"path": str(tiny_photos), "name": "Pics", "scan": True}, headers=_h())
    # let the auto-scan finish
    for _ in range(1500):
        js = client.get("/jobs", headers=_h()).json()["jobs"]
        if js and all(j["status"] != "running" for j in js):
            break
        time.sleep(0.02)
    prev = client.get("/cleanup/preview?root=Pics", headers=_h()).json()
    assert prev["name"] == "Pics" and prev["count"] == 0  # nothing trashed yet
    # undecodable-mode preview is a distinct count (0 here — the tiny PNGs decode fine).
    prev_u = client.get("/cleanup/preview?root=Pics&mode=undecodable", headers=_h()).json()
    assert prev_u["count"] == 0


def test_cleanup_unknown_mode_400(client, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    client.post("/roots", json={"path": str(lib), "name": "Lib"}, headers=_h())
    r = client.post("/cleanup", json={"root": "Lib", "mode": "bogus"}, headers=_h())
    assert r.status_code == 400
    assert "unknown cleanup mode" in r.json()["detail"]


def test_trash_refresh_endpoint(client, tmp_path):
    trash = tmp_path / "Trash"
    trash.mkdir()
    client.post("/roots", json={"path": str(trash), "kind": "trash"}, headers=_h())
    r = client.post("/trash/refresh", json={}, headers=_h())
    assert r.status_code == 200
    d = _wait(client, r.json()["job_id"])
    assert d["status"] == "done" and d["type"] == "trash-refresh"


def test_untrash_endpoint(client, tmp_path):
    f = tmp_path / "recovered.png"
    import numpy as np
    from PIL import Image

    Image.fromarray(np.zeros((16, 16, 3), dtype="uint8")).save(f)
    r = client.post("/untrash", json={"path": str(f)}, headers=_h())
    assert r.status_code == 200
    d = _wait(client, r.json()["job_id"])
    assert d["status"] == "done" and d["type"] == "untrash"


def test_untrash_needs_path_400(client):
    r = client.post("/untrash", json={}, headers=_h())
    assert r.status_code == 400
