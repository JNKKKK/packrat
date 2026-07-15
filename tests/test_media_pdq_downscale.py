"""Downscale-before-PDQ (§ profiler finding): dims preserved, hash near full-res."""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")
pytest.importorskip("pdqhash")

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

from packrat import media  # noqa: E402
from packrat.config import Config  # noqa: E402


def _hamming(a: bytes, b: bytes) -> int:
    return int(np.count_nonzero(np.unpackbits(np.frombuffer(a, np.uint8))
                                != np.unpackbits(np.frombuffer(b, np.uint8))))


def _big_photo(tmp_path, w=2400, h=1800):
    """A photo-like JPEG well above the 512px edge, with low/mid-freq detail."""
    yy, xx = np.mgrid[0:h, 0:w]
    base = np.sin(xx / w * 6.28) * 60 + np.cos(yy / h * 9.42) * 50 + 128
    im = Image.fromarray(np.stack([base, base * 0.8 + 30, base * 0.6 + 60], -1)
                         .clip(0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    rng = np.random.default_rng(0)
    for _ in range(8):
        x0, y0 = int(rng.integers(0, w)), int(rng.integers(0, h))
        r = int(rng.integers(w // 10, w // 4))
        d.ellipse([x0, y0, x0 + r, y0 + r], fill=tuple(int(v) for v in rng.integers(0, 255, 3)))
    p = tmp_path / "big.jpg"
    im.filter(ImageFilter.GaussianBlur(2)).save(p, "JPEG", quality=92)
    return p, w, h


def test_dimensions_stay_full_res(tmp_path):
    p, w, h = _big_photo(tmp_path)
    fp = media.fingerprint(str(p), p.stat().st_size, Config())  # default pdq_max_edge=512
    assert (fp.width, fp.height) == (w, h)  # stored dims are original, not 512
    assert fp.phash_bits is not None and len(fp.phash_bits) == 32


def test_downscaled_hash_within_threshold_of_full_res(tmp_path):
    p, _w, _h = _big_photo(tmp_path)
    cfg512 = Config()
    cfg_full = replace(cfg512, match=replace(cfg512.match, pdq_max_edge=0))
    fp512 = media.fingerprint(str(p), p.stat().st_size, cfg512)
    fp_full = media.fingerprint(str(p), p.stat().st_size, cfg_full)
    dist = _hamming(fp512.phash_bits, fp_full.phash_bits)
    # Drift must be comfortably inside the photo match cutoff (default 32).
    assert dist < cfg512.match.t_match_photo, f"drift {dist} too large"


def test_max_edge_zero_disables_downscale(tmp_path, monkeypatch):
    p, _w, _h = _big_photo(tmp_path)
    cfg_full = replace(Config(), match=replace(Config().match, pdq_max_edge=0))

    seen = {}
    orig = media._downscale_for_pdq

    def spy(arr, max_edge):
        seen["max_edge"] = max_edge
        return orig(arr, max_edge)

    monkeypatch.setattr(media, "_downscale_for_pdq", spy)
    media.fingerprint(str(p), p.stat().st_size, cfg_full)
    assert seen["max_edge"] == 0  # full-res path


def test_downscale_helper_shrinks_only_when_larger():
    small = np.zeros((100, 100, 3), dtype=np.uint8)
    # Already under the edge → returned unchanged (same object).
    assert media._downscale_for_pdq(small, 512) is small
    big = np.zeros((800, 600, 3), dtype=np.uint8)
    out = media._downscale_for_pdq(big, 512)
    assert max(out.shape[0], out.shape[1]) == 512
    # 0 disables.
    assert media._downscale_for_pdq(big, 0) is big


def test_video_frames_downscaled_dims_preserved(tmp_path):
    av = pytest.importorskip("av")
    p = tmp_path / "v.mp4"
    container = av.open(str(p), "w")
    s = container.add_stream("libx264", rate=15)
    s.width, s.height, s.pix_fmt = 640, 480, "yuv420p"
    for i in range(30):
        f = np.random.default_rng(i).integers(0, 256, (480, 640, 3), dtype=np.uint8)
        for pk in s.encode(av.VideoFrame.from_ndarray(f, format="rgb24")):
            container.mux(pk)
    for pk in s.encode():
        container.mux(pk)
    container.close()

    fp = media.fingerprint(str(p), p.stat().st_size, Config())
    assert (fp.width, fp.height) == (640, 480)  # dims original despite 512 PDQ edge
    assert fp.frames and all(len(fr.pdq_bits) == 32 for fr in fp.frames)
