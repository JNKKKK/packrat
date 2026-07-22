"""Shared test fixtures: isolate PACKRAT_HOME so tests never touch real state.

Also registers a tiny test-only ``sleeper`` job — a cancellable, progress-emitting
job used to exercise the *runtime* (submit / progress / cancel / busy / SSE) in
``test_jobs.py`` / ``test_api.py`` without coupling those tests to a real operation
(``scan`` needs files + a root). It owns no root, so it never trips per-root
exclusivity. (This replaces the removed ``demo`` job, which served the same role.)
"""

from __future__ import annotations

import os
import time

import pytest

from packrat.jobs.registry import JobSpec, get_job_spec, register_job


def _run_sleeper(ctx) -> None:
    steps = int(ctx.params.get("steps", 10))
    delay = float(ctx.params.get("delay_s", 0.2))
    ctx.set_total(steps)
    for i in range(steps):
        ctx.check_cancelled()  # cooperative cancellation checkpoint (§9)
        time.sleep(delay)
        ctx.progress(i + 1, message=f"step {i + 1}/{steps}")


# Register once for the whole session (idempotent — get_job_spec guards re-import).
if get_job_spec("sleeper") is None:
    register_job(JobSpec(type="sleeper", handler=_run_sleeper, owned_root=None))


@pytest.fixture()
def packrat_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PACKRAT_HOME", str(home))
    # paths.home_dir() reads the env each call, so nothing else to reset.
    return home


@pytest.fixture()
def tiny_photos(tmp_path):
    """A folder with a few tiny real PNGs (distinct + one exact duplicate).

    Pure Pillow — no HEIC/video deps — so scan tests run wherever the ``media``
    extra's decode wheels are present (all decode paths are proven by the M0 smoke
    test; scan tests just need *some* decodable media + an exact-dup + a subfolder).
    """
    import numpy as np
    from PIL import Image

    d = tmp_path / "lib"
    d.mkdir()

    def _png(path, seed):
        rng = np.random.default_rng(seed)
        arr = rng.integers(0, 256, size=(32, 32, 3), dtype=np.uint8)
        Image.fromarray(arr).save(path, format="PNG")

    _png(d / "a.png", 1)
    _png(d / "b.png", 2)
    sub = d / "sub"
    sub.mkdir()
    import shutil

    shutil.copy(d / "a.png", sub / "a_copy.png")  # exact byte-dup of a.png
    (d / "notes.txt").write_text("ignored non-media")
    return d
