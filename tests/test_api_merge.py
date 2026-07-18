"""Daemon HTTP API for M5: /merge (§8 C).

In-process TestClient (never binds the port), mirroring test_api_cleanup.py. Verifies
the HTTP surface — routing, --into resolution (name / subfolder / no-library-root),
validation, param passthrough — not the full copy behavior (that's tests/test_merge.py
against the handler).
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
        if d["status"] not in ("running", "queued"):
            return d
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def _png(path, seed):
    import numpy as np
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    Image.fromarray(rng.integers(0, 256, size=(16, 16, 3), dtype=np.uint8)).save(path, "PNG")


def test_merge_needs_source_400(client):
    r = client.post("/merge", json={"into": "Lib"}, headers=_h())
    assert r.status_code == 400
    assert "source" in r.json()["detail"]


def test_merge_needs_into_400(client, tmp_path):
    src = tmp_path / "src"
    _png(src / "a.png", 1)
    r = client.post("/merge", json={"source": str(src)}, headers=_h())
    assert r.status_code == 400
    assert "into" in r.json()["detail"]


def test_merge_into_unregistered_400(client, tmp_path):
    src = tmp_path / "src"
    _png(src / "a.png", 1)
    r = client.post("/merge", json={"source": str(src), "into": str(tmp_path / "nowhere")},
                    headers=_h())
    assert r.status_code == 400
    assert "library root" in r.json()["detail"]


def test_merge_into_trash_root_400(client, tmp_path):
    trash = tmp_path / "Trash"
    trash.mkdir()
    client.post("/roots", json={"path": str(trash), "kind": "trash"}, headers=_h())
    src = tmp_path / "src"
    _png(src / "a.png", 1)
    r = client.post("/merge", json={"source": str(src), "into": "Trash"}, headers=_h())
    assert r.status_code == 400
    assert "library root" in r.json()["detail"]


def test_merge_into_by_name(client, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    client.post("/roots", json={"path": str(lib), "name": "Lib"}, headers=_h())
    src = tmp_path / "src"
    _png(src / "new.png", 1)
    r = client.post("/merge", json={"source": str(src), "into": "Lib"}, headers=_h())
    assert r.status_code == 200
    d = _wait(client, r.json()["job_id"])
    assert d["status"] == "done" and d["type"] == "merge"
    assert (lib / "new.png").exists()


def test_merge_into_subfolder(client, tmp_path):
    """--into may be a (not-yet-existing) SUBFOLDER of a library root (containment)."""
    lib = tmp_path / "lib"
    lib.mkdir()
    client.post("/roots", json={"path": str(lib), "name": "Lib"}, headers=_h())
    src = tmp_path / "src"
    _png(src / "new.png", 1)
    into = lib / "incoming" / "2024"
    r = client.post("/merge", json={"source": str(src), "into": str(into)}, headers=_h())
    assert r.status_code == 200
    d = _wait(client, r.json()["job_id"])
    assert d["status"] == "done"
    assert (into / "new.png").exists()  # copied under the requested subfolder


def test_merge_dry_run_passthrough(client, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    client.post("/roots", json={"path": str(lib), "name": "Lib"}, headers=_h())
    src = tmp_path / "src"
    _png(src / "new.png", 1)
    r = client.post("/merge", json={"source": str(src), "into": "Lib", "dry_run": True},
                    headers=_h())
    assert r.status_code == 200
    d = _wait(client, r.json()["job_id"])
    assert d["status"] == "done"
    assert not (lib / "new.png").exists()  # dry-run copied nothing
