"""Smoke-test sample generation (§9.1).

Skipped when the media deps aren't installed — the generator degrades to a
``failed`` summary rather than raising, and there's nothing to assert on a bare
runtime install.
"""

from __future__ import annotations

import importlib.util

import pytest

from packrat import smoke

_HAS_MEDIA = all(
    importlib.util.find_spec(m) is not None
    for m in ("numpy", "PIL", "pillow_heif", "av")
)

pytestmark = pytest.mark.skipif(not _HAS_MEDIA, reason="media extra not installed")


def test_generate_samples_covers_photo_and_video(tmp_path):
    summary = smoke.generate_samples(tmp_path)
    # Every synthesizable (non-RAW) extension should be produced.
    assert set(summary["photos"]) == set(smoke._PHOTO_SAVE)
    assert set(summary["videos"]) == set(smoke._VIDEO_ENCODE)
    assert summary["failed"] == {}
    # Files actually landed on disk.
    written = {p.suffix.lstrip(".") for p in tmp_path.glob("sample.*")}
    assert "heic" in written and "avif" in written and "mp4" in written


def test_run_smoke_test_generate_is_self_contained(tmp_path, capsys):
    code = smoke.run_smoke_test(str(tmp_path), generate=True)
    assert code == 0  # no decode/hash hard failures
    out = capsys.readouterr().out
    assert "generated" in out
    # HEIC/AVIF (the ⚠ cells) decode + hash + PDQ cleanly.
    assert "avif" in out and "heic" in out
