"""The §5.3 perceptual matching engine (matcher.py) — photo + video decision rules.

Pure fingerprint math; no DB or files needed for the core functions. We build PDQ
blobs by hand (32 packed bytes) so we control exact Hamming distances.
"""

from __future__ import annotations

import pytest

pytest.importorskip("numpy")

import numpy as np  # noqa: E402

from packrat import matcher  # noqa: E402
from packrat.config import Config, VideoConfig  # noqa: E402


def _bits(seed: int, flip: int = 0) -> bytes:
    """A deterministic 256-bit PDQ blob; ``flip`` flips the first ``flip`` bits."""
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 2, size=256, dtype=np.uint8)
    for i in range(flip):
        arr[i] ^= 1
    return np.packbits(arr).tobytes()


def test_hamming_counts_bit_differences():
    a = _bits(1)
    b = _bits(1, flip=5)
    assert matcher.hamming(a, b) == 5
    assert matcher.hamming(a, a) == 0


def test_match_photos_threshold_boundary():
    base = _bits(10)
    close = _bits(10, flip=20)   # within default t_photo_edit=32
    far = _bits(10, flip=60)     # outside
    targets = [matcher.PhotoSig(1, base)]
    pool = [matcher.PhotoSig(2, close), matcher.PhotoSig(3, far)]
    edges = matcher.match_photos(targets, pool, t_match=32)
    assert len(edges) == 1
    e = edges[0]
    assert (e.asset_a, e.asset_b) == (1, 2)  # canonical order
    assert e.distance == 20 and e.algo == "pdq"


def test_match_photos_canonical_order_and_dedup_both_directions():
    base = _bits(10)
    close = _bits(10, flip=8)
    # Both assets in the target set → pair discoverable from both sides, emit once.
    targets = [matcher.PhotoSig(5, base), matcher.PhotoSig(3, close)]
    pool = targets
    edges = matcher.match_photos(targets, pool, t_match=32)
    assert len(edges) == 1
    assert (edges[0].asset_a, edges[0].asset_b) == (3, 5)


def test_match_photos_quality_never_gates():
    base = _bits(10)
    close = _bits(10, flip=4)
    # Even a zero-quality photo must still match (§5.3 annotate-never-gate).
    targets = [matcher.PhotoSig(1, base, quality=0)]
    pool = [matcher.PhotoSig(2, close, quality=0)]
    assert len(matcher.match_photos(targets, pool, t_match=32)) == 1


def _vsig(asset_id, dur, frame_specs):
    """frame_specs: list of (frame_index, seed, flip, quality)."""
    frames = [(fi, _bits(seed, flip), q) for fi, seed, flip, q in frame_specs]
    return matcher.VideoSig(asset_id, dur, frames)


def test_match_videos_frame_vote_passes():
    cfg = VideoConfig()  # frac 0.60, min_comparable 5, min_quality 50, t default via caller
    # 6 comparable frames, 5 match within t=90 (flip small), 1 doesn't.
    a = _vsig(1, 10.0, [(i, 100 + i, 0, 80) for i in range(6)])
    b_specs = [(i, 100 + i, 2, 80) for i in range(5)] + [(5, 999, 0, 80)]
    b = _vsig(2, 10.2, b_specs)
    edges = matcher.match_videos([a], [b], cfg, t_match=90)
    assert len(edges) == 1 and edges[0].algo == "video"


def test_match_videos_duration_prefilter_blocks():
    cfg = VideoConfig()
    a = _vsig(1, 10.0, [(i, 100 + i, 0, 80) for i in range(6)])
    b = _vsig(2, 30.0, [(i, 100 + i, 0, 80) for i in range(6)])  # far duration
    assert matcher.match_videos([a], [b], cfg, t_match=90) == []


def test_match_videos_min_comparable_frames_gate():
    cfg = VideoConfig()  # min_comparable_frames=5
    # Only 3 frames clear the quality gate (rest below 50) → insufficient evidence.
    a = _vsig(1, 10.0, [(i, 100 + i, 0, 80 if i < 3 else 10) for i in range(6)])
    b = _vsig(2, 10.0, [(i, 100 + i, 0, 80 if i < 3 else 10) for i in range(6)])
    assert matcher.match_videos([a], [b], cfg, t_match=90) == []


def test_match_videos_fraction_gate():
    cfg = VideoConfig()  # frac 0.60
    # 6 comparable, only 2 match (flip huge on the rest) → below 0.60.
    a = _vsig(1, 10.0, [(i, 100 + i, 0, 80) for i in range(6)])
    b_specs = [(i, 100 + i, 2, 80) for i in range(2)] + [(i, 500 + i, 0, 80) for i in range(2, 6)]
    b = _vsig(2, 10.0, b_specs)
    assert matcher.match_videos([a], [b], cfg, t_match=90) == []


def test_find_matches_uses_t_photo_edit_as_cutoff():
    # A pair at distance 20 is inside t_photo_edit (32) but outside t_photo_recompress
    # (10) — find_matches must still MATCH it (banding into stages is dedup's job, §8 B).
    cfg = Config()
    base, mid = _bits(7), _bits(7, flip=20)
    edges = matcher.find_matches(
        matcher.Signatures(photos=[matcher.PhotoSig(1, base)]),
        matcher.Signatures(photos=[matcher.PhotoSig(2, mid)]),
        cfg,
    )
    assert len(edges) == 1 and edges[0].distance == 20
    # A pair beyond t_photo_edit is not a match at all.
    far = _bits(7, flip=40)
    assert matcher.find_matches(
        matcher.Signatures(photos=[matcher.PhotoSig(1, base)]),
        matcher.Signatures(photos=[matcher.PhotoSig(3, far)]),
        cfg,
    ) == []


def test_find_matches_splits_media():
    cfg = Config()
    pbase, pclose = _bits(1), _bits(1, flip=3)
    va = _vsig(10, 5.0, [(i, 200 + i, 0, 80) for i in range(6)])
    vb = _vsig(11, 5.0, [(i, 200 + i, 1, 80) for i in range(6)])
    targets = matcher.Signatures(photos=[matcher.PhotoSig(1, pbase)], videos=[va])
    pool = matcher.Signatures(photos=[matcher.PhotoSig(2, pclose)], videos=[vb])
    edges = matcher.find_matches(targets, pool, cfg)
    kinds = {e.media_type for e in edges}
    assert kinds == {"photo", "video"}
    assert len(edges) == 2
