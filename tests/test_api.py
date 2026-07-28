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
    r = client.post("/jobs", json={"type": "sleeper", "params": {"steps": 3, "delay_s": 0.01}}, headers=_h())
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


def test_second_submit_enqueues_not_rejected(client):
    """§3: a submission while the worker is busy is QUEUED, not rejected (no 409)."""
    r1 = client.post(
        "/jobs", json={"type": "sleeper", "params": {"steps": 50, "delay_s": 0.05}}, headers=_h()
    )
    assert r1.status_code == 200
    r2 = client.post("/jobs", json={"type": "sleeper", "params": {"steps": 2}}, headers=_h())
    assert r2.status_code == 200
    jid2 = r2.json()["job_id"]
    # The second job is parked in the durable backlog behind the running one.
    assert client.get(f"/jobs/{jid2}", headers=_h()).json()["status"] == "queued"


def test_queued_job_runs_after_predecessor(client):
    """The backlog drains: the queued job runs once the first finishes (§3)."""
    r1 = client.post(
        "/jobs", json={"type": "sleeper", "params": {"steps": 4, "delay_s": 0.02}}, headers=_h()
    )
    r2 = client.post(
        "/jobs", json={"type": "sleeper", "params": {"steps": 2, "delay_s": 0.01}}, headers=_h()
    )
    jid1, jid2 = r1.json()["job_id"], r2.json()["job_id"]
    for _ in range(400):
        s2 = client.get(f"/jobs/{jid2}", headers=_h()).json()["status"]
        if s2 == "done":
            break
        time.sleep(0.02)
    assert client.get(f"/jobs/{jid1}", headers=_h()).json()["status"] == "done"
    assert s2 == "done"


def test_cancel_queued_job_drops_it(client):
    """Cancelling a still-queued job drops it from the backlog (cancelled, never ran)."""
    client.post(
        "/jobs", json={"type": "sleeper", "params": {"steps": 50, "delay_s": 0.05}}, headers=_h()
    )
    r2 = client.post("/jobs", json={"type": "sleeper", "params": {"steps": 2}}, headers=_h())
    jid2 = r2.json()["job_id"]
    assert client.get(f"/jobs/{jid2}", headers=_h()).json()["status"] == "queued"
    client.post(f"/jobs/{jid2}/cancel", headers=_h())
    assert client.get(f"/jobs/{jid2}", headers=_h()).json()["status"] == "cancelled"


def test_prioritize_queued_job(client):
    """POST /jobs/{id}/prioritize bumps a queued job to the front (§11)."""
    client.post(
        "/jobs", json={"type": "sleeper", "params": {"steps": 50, "delay_s": 0.05}}, headers=_h()
    )
    a = client.post("/jobs", json={"type": "sleeper", "params": {"steps": 2}}, headers=_h()).json()["job_id"]
    b = client.post("/jobs", json={"type": "sleeper", "params": {"steps": 2}}, headers=_h()).json()["job_id"]
    r = client.post(f"/jobs/{b}/prioritize", headers=_h())
    assert r.status_code == 200 and r.json()["prioritized"] is True
    # The backlog snapshot now lists b (prioritized) ahead of a (enqueued earlier).
    queued = client.get("/jobs/queued", headers=_h()).json()["queued"]
    ids = [q["id"] for q in queued]
    assert ids.index(b) < ids.index(a)


def test_prioritize_running_job_is_noop(client):
    """A running (not queued) job can't be prioritized → prioritized:false (§11)."""
    jid = client.post(
        "/jobs", json={"type": "sleeper", "params": {"steps": 4, "delay_s": 0.05}}, headers=_h()
    ).json()["job_id"]
    for _ in range(50):
        if client.get(f"/jobs/{jid}", headers=_h()).json()["status"] == "running":
            break
        time.sleep(0.02)
    r = client.post(f"/jobs/{jid}/prioritize", headers=_h())
    assert r.status_code == 200 and r.json()["prioritized"] is False


def test_roots_snapshot_empty(client):
    assert client.get("/roots", headers=_h()).json() == {"roots": []}


def _run_sleepers(client, n: int) -> None:
    """Submit n sleeper jobs sequentially, each to completion (terminal history)."""
    for _ in range(n):
        jid = client.post(
            "/jobs", json={"type": "sleeper", "params": {"steps": 1, "delay_s": 0.0}},
            headers=_h()).json()["job_id"]
        for _ in range(200):
            if client.get(f"/jobs/{jid}", headers=_h()).json()["status"] != "running":
                break
            time.sleep(0.01)


def test_stats_and_live_decompose_status(client):
    """/stats + /jobs/live + /roots recompose to the /status shape (§12 decomposition)."""
    snap = client.get("/status", headers=_h()).json()
    stats = client.get("/stats", headers=_h()).json()
    live = client.get("/jobs/live", headers=_h()).json()
    roots = client.get("/roots", headers=_h()).json()["roots"]
    # Stats keys are the collection box; live keys are the job sections; together (+roots)
    # they are exactly the status snapshot.
    assert set(stats) == {"assets", "photos", "videos", "trashed", "size_bytes",
                          "lifetime_deduped"}
    assert set(live) == {"running", "queued", "interrupted", "pending_reviews"}
    composed = {**stats, **live, "roots": roots}
    assert composed == snap


def test_jobs_live_route_not_shadowed_by_job_id(client):
    """/jobs/live must resolve to the live endpoint, not be parsed as job id 'live'."""
    r = client.get("/jobs/live", headers=_h())
    assert r.status_code == 200 and "running" in r.json()


def test_history_pagination_offset_and_total(client):
    """/jobs?terminal_only&limit&offset pages finished jobs; total is the true count."""
    _run_sleepers(client, 5)
    page0 = client.get("/jobs?limit=2&offset=0&terminal_only=true", headers=_h()).json()
    page1 = client.get("/jobs?limit=2&offset=2&terminal_only=true", headers=_h()).json()
    assert page0["total"] == 5 and page1["total"] == 5
    assert len(page0["jobs"]) == 2 and len(page1["jobs"]) == 2
    ids0 = [j["id"] for j in page0["jobs"]]
    ids1 = [j["id"] for j in page1["jobs"]]
    assert ids0 == sorted(ids0, reverse=True)          # newest-first
    assert not set(ids0) & set(ids1)                   # disjoint pages
    assert min(ids0) > max(ids1)                       # page 1 is strictly older
    # terminal_only excludes nothing here (all sleepers finished), but the flag is honored:
    assert all(j["status"] in ("done", "error", "cancelled", "interrupted")
               for j in page0["jobs"])


def test_history_terminal_only_excludes_queued(client):
    """terminal_only=true drops running/queued rows (they live in the live sections)."""
    # A long runner + a queued follower: the backlog job must NOT appear in history.
    client.post("/jobs", json={"type": "sleeper", "params": {"steps": 50, "delay_s": 0.05}},
                headers=_h())
    qid = client.post("/jobs", json={"type": "sleeper", "params": {"steps": 2}},
                      headers=_h()).json()["job_id"]
    assert client.get(f"/jobs/{qid}", headers=_h()).json()["status"] == "queued"
    hist = client.get("/jobs?terminal_only=true&limit=50", headers=_h()).json()
    assert qid not in [j["id"] for j in hist["jobs"]]      # queued excluded from history
    # The default (non-terminal) list still includes the backlog (CLI `jobs list` shape).
    allj = client.get("/jobs?limit=50", headers=_h()).json()
    assert qid in [j["id"] for j in allj["jobs"]]


def test_late_attach_stream_closes(client):
    r = client.post("/jobs", json={"type": "sleeper", "params": {"steps": 2, "delay_s": 0.01}}, headers=_h())
    jid = r.json()["job_id"]
    for _ in range(200):
        if client.get(f"/jobs/{jid}", headers=_h()).json()["status"] != "running":
            break
        time.sleep(0.02)
    # attaching to a finished job returns its terminal state then closes
    with client.stream("GET", f"/jobs/{jid}/stream", headers=_h()) as s:
        lines = [ln for ln in s.iter_lines() if ln.startswith("data:")]
    assert any("done" in ln for ln in lines)
