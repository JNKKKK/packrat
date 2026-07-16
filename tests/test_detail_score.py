"""Photo detail_score (§8 B stage-2 keep-lead): the retained-detail estimate.

The measure is zlib size of the full-res horizontal pixel residual. Property under
test: at the SAME resolution, a less-compressed photo scores higher, and a
PNG-of-a-JPEG (caveat 2) does NOT beat its lossy source (decode makes it codec-fair).
"""

from __future__ import annotations

import io

import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

from packrat import media  # noqa: E402
from packrat.config import Config  # noqa: E402


def _structured(w=384, h=288, seed=0):
    yy, xx = np.mgrid[0:h, 0:w]
    base = np.sin(xx / w * 6.28) * 60 + np.cos(yy / h * 9.42) * 50 + 128
    im = Image.fromarray(np.stack([base, base * 0.8 + 30, base * 0.6 + 60], -1).clip(0, 255).astype("uint8"))
    dr = ImageDraw.Draw(im)
    rng = np.random.default_rng(seed)
    for _ in range(10):
        x0, y0 = int(rng.integers(0, w)), int(rng.integers(0, h))
        r = int(rng.integers(w // 8, w // 3))
        dr.ellipse([x0, y0, x0 + r, y0 + r], fill=tuple(int(v) for v in rng.integers(0, 255, 3)))
    return im.filter(ImageFilter.GaussianBlur(0.6))


def _rgb(img):
    return np.asarray(img.convert("RGB"))


def _reencode(im, fmt, **kw):
    b = io.BytesIO()
    im.save(b, fmt, **kw)
    return _rgb(Image.open(io.BytesIO(b.getvalue())))


def test_detail_score_monotonic_in_quality():
    im = _structured()
    hi = media._detail_score(_reencode(im, "JPEG", quality=95))
    mid = media._detail_score(_reencode(im, "JPEG", quality=80))
    lo = media._detail_score(_reencode(im, "JPEG", quality=30))
    # Less compression → more retained detail → higher score.
    assert hi > lo and mid > lo


def test_detail_score_png_of_jpeg_matches_jpeg_not_inflated():
    im = _structured()
    j80 = _reencode(im, "JPEG", quality=80)
    # PNG-of-the-JPEG decodes to the same pixels → same score (caveat 2 resolved),
    # despite a much larger file. Decode makes the measure codec-fair.
    png_of_j80 = io.BytesIO()
    Image.open(io.BytesIO(_jpeg_bytes(im, 80))).convert("RGB").save(png_of_j80, "PNG")
    score_j = media._detail_score(j80)
    score_png_of_j = media._detail_score(_rgb(Image.open(io.BytesIO(png_of_j80.getvalue()))))
    assert abs(score_j - score_png_of_j) <= max(2, score_j // 100)  # essentially equal


def _jpeg_bytes(im, q):
    b = io.BytesIO()
    im.save(b, "JPEG", quality=q)
    return b.getvalue()


def test_detail_score_never_raises_on_degenerate():
    assert media._detail_score(np.zeros((1, 1, 3), dtype=np.uint8)) == 0   # width < 2
    assert media._detail_score(np.zeros((4, 4), dtype=np.uint8)) >= 0       # 2-D input tolerated


def test_fingerprint_carries_detail_score(tmp_path):
    im = _structured()
    p = tmp_path / "a.png"
    im.save(p)
    fp = media.fingerprint(str(p), p.stat().st_size, Config())
    assert fp.media_type == "photo"
    assert fp.detail_score is not None and fp.detail_score > 0
