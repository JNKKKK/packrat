"""Daemon HTTP API: auth, submit, snapshots, SSE (§3), in-process via TestClient."""

from __future__ import annotations

import time
import warnings

import pytest

warnings.simplefilter("ignore")

from starlette.testclient import TestClient  # noqa: E402

from packrat.daemon.server import build_app  # noqa: E402

TOKEN = "test-token"


@pytest.fixture()
def client(packrat_home):
    app = build_app(TOKEN)
    with TestClient(app) as c:
        yield c


def _h():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_health_unauthenticated(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_auth_required(client):
    assert client.get("/status").status_code == 401
    assert client.get("/status", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_submit_and_status(client):
    r = client.post("/jobs", json={"type": "demo", "params": {"steps": 3, "delay_s": 0.01}}, headers=_h())
    assert r.status_code == 200
    jid = r.json()["job_id"]
    # wait for completion
    for _ in range(200):
        d = client.get(f"/jobs/{jid}", headers=_h()).json()
        if d["status"] != "running":
            break
        time.sleep(0.02)
    assert d["status"] == "done"
    snap = client.get("/status", headers=_h()).json()
    assert snap["assets"] == 0 and "roots" in snap


def test_busy_returns_409(client):
    client.post("/jobs", json={"type": "demo", "params": {"steps": 50, "delay_s": 0.05}}, headers=_h())
    r = client.post("/jobs", json={"type": "demo", "params": {"steps": 2}}, headers=_h())
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "busy" and body["kind"] == "global"


def test_roots_snapshot_empty(client):
    assert client.get("/roots", headers=_h()).json() == {"roots": []}


def test_late_attach_stream_closes(client):
    r = client.post("/jobs", json={"type": "demo", "params": {"steps": 2, "delay_s": 0.01}}, headers=_h())
    jid = r.json()["job_id"]
    for _ in range(200):
        if client.get(f"/jobs/{jid}", headers=_h()).json()["status"] != "running":
            break
        time.sleep(0.02)
    # attaching to a finished job returns its terminal state then closes
    with client.stream("GET", f"/jobs/{jid}/stream", headers=_h()) as s:
        lines = [ln for ln in s.iter_lines() if ln.startswith("data:")]
    assert any("done" in ln for ln in lines)
