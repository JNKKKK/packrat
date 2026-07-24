"""Transport-stream (.ts / .m2ts / .mts) support (§5.3 duration fallback + allowlist).

Transport streams routinely carry NO stream/container duration, which used to collapse
``_probe_video`` to a single frame → too few comparable frames to ever dedup (§5.3
min_comparable_frames). These tests build a real mpegts-muxed clip and assert the
demux-based duration fallback recovers a timeline so the clip samples across it, plus
that ``.ts`` is recognized as video by the allowlist + type classifier.
"""

from __future__ import annotations

import pytest

pytest.importorskip("numpy")
pytest.importorskip("pdqhash")

import numpy as np  # noqa: E402

from packrat import media  # noqa: E402
from packrat.config import VIDEO_EXTS, Config  # noqa: E402


def _write_ts(path, *, frames: int = 30, w: int = 320, h: int = 240) -> None:
    """Encode a real MPEG-TS clip (h264 in a transport-stream mux) at ``path``."""
    av = pytest.importorskip("av")
    container = av.open(str(path), "w", format="mpegts")
    try:
        s = container.add_stream("libx264", rate=15)
        s.width, s.height, s.pix_fmt = w, h, "yuv420p"
        for i in range(frames):
            arr = np.random.default_rng(i).integers(0, 256, (h, w, 3), dtype=np.uint8)
            for pk in s.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")):
                container.mux(pk)
        for pk in s.encode():
            container.mux(pk)
    finally:
        container.close()


# --- allowlist / type classification ---------------------------------------
def test_ts_family_is_allowlisted_video():
    """`.ts` joins the transport-stream cousins already in the default video allowlist,
    and the type classifier assigns it 'video' (both read the one VIDEO_EXTS set)."""
    assert "ts" in VIDEO_EXTS
    for name in ("clip.ts", "CLIP.TS", "cam.m2ts", "cam.mts"):
        assert media.media_type_of(name) == "video", name
    # Sanity: TypeScript-looking non-media is unaffected only by extension — .ts IS now
    # media, so a media root must not contain code. (Documented caveat, asserted here so
    # the behavior is explicit, not a surprise.)
    assert media.media_type_of("app.tsx") is None


# --- decode: duration fallback recovers a timeline from packet timestamps -----
def test_duration_by_demux_recovers_timeline_from_packets(tmp_path):
    """The helper estimates duration from the last packet's presentation end when the
    container reports none — ~frames / rate (15 fps here → ~2 s for 30 frames)."""
    av = pytest.importorskip("av")
    p = tmp_path / "len.ts"
    _write_ts(p, frames=30)
    c = av.open(str(p))
    try:
        vs = c.streams.video[0]
        dur = media._duration_by_demux(c, vs, vs.time_base)
    finally:
        c.close()
    assert dur is not None
    assert 1.0 <= dur <= 4.0, dur                        # generous band around ~2 s


def test_duration_by_demux_none_without_time_base(tmp_path):
    """No time_base → no way to convert timestamps → None (caller keeps single-frame path)."""
    av = pytest.importorskip("av")
    p = tmp_path / "n.ts"
    _write_ts(p, frames=10)
    c = av.open(str(p))
    try:
        assert media._duration_by_demux(c, c.streams.video[0], None) is None
    finally:
        c.close()


# --- duration source selection (pure ladder, no container) -------------------
def test_video_duration_s_prefers_stream_then_container_then_demux():
    """The best-first ladder: stream duration → container duration → demux fallback. The
    fallback thunk runs ONLY when neither native source exists (so the extra packet scan
    hits just the header-less .ts case), and None when there's no timeline at all."""
    calls = {"n": 0}

    def demux():
        calls["n"] += 1
        return 9.0

    # Stream duration wins (in time_base units): 90000 * (1/90000) = 1.0 s; demux untouched.
    assert media._video_duration_s(90000, 0.00001111111, 2_000_000, 10 ** 6, demux_dur=demux) == \
        pytest.approx(90000 * 0.00001111111)
    assert calls["n"] == 0
    # No stream duration → container duration (av.time_base units): 2_000_000 / 1e6 = 2.0 s.
    assert media._video_duration_s(0, 1 / 90000, 2_000_000, 10 ** 6, demux_dur=demux) == 2.0
    assert calls["n"] == 0
    # Neither native source → demux fallback runs exactly once.
    assert media._video_duration_s(None, 1 / 90000, None, 10 ** 6, demux_dur=demux) == 9.0
    assert calls["n"] == 1
    # No time_base at all → no timeline, and the fallback is NOT consulted.
    assert media._video_duration_s(None, None, None, 10 ** 6, demux_dur=demux) is None
    assert calls["n"] == 1


def test_ts_fingerprint_samples_multiple_frames(tmp_path):
    """End-to-end public path: a real .ts yields a multi-frame, decodable signature with
    enough comparable frames to participate in dedup (§5.3 min_comparable_frames) — not the
    single frame the old no-duration path produced."""
    p = tmp_path / "clip.ts"
    _write_ts(p, frames=45)
    fp = media.fingerprint(str(p), p.stat().st_size, Config())
    assert not fp.undecodable, fp.decode_error
    assert fp.duration_s and fp.duration_s > 0
    assert len(fp.frames) >= Config().video.min_comparable_frames
    assert all(len(fr.pdq_bits) == 32 for fr in fp.frames)


def test_mp4_still_samples_full_frame_count_via_seek(tmp_path):
    """Regression: the seek-path restructure must not degrade a well-behaved container. A
    normal seekable mp4 still samples the full sample_frames count (12) — proving the seek
    path delivers and the sequential fallback isn't silently masking a seek regression."""
    av = pytest.importorskip("av")
    p = tmp_path / "clip.mp4"
    container = av.open(str(p), "w")
    try:
        s = container.add_stream("libx264", rate=15)
        s.width, s.height, s.pix_fmt = 320, 240, "yuv420p"
        for i in range(60):                              # 4 s @ 15 fps, plenty to sample 12
            arr = np.random.default_rng(i).integers(0, 256, (240, 320, 3), dtype=np.uint8)
            for pk in s.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")):
                container.mux(pk)
        for pk in s.encode():
            container.mux(pk)
    finally:
        container.close()

    cfg = Config()
    fp = media.fingerprint(str(p), p.stat().st_size, cfg)
    assert not fp.undecodable, fp.decode_error
    assert len(fp.frames) == cfg.video.sample_frames    # all 12 sampled via seek


def test_undecodable_ts_still_flagged_not_frame_faked(tmp_path):
    """The ≥1-frame guarantee must NOT mask a genuinely undecodable file: bytes that only
    LOOK like a .ts (no real stream) stay undecodable=1 with no frames (§9.1)."""
    p = tmp_path / "bogus.ts"
    p.write_bytes(b"\x47" + b"\x00" * 4096)              # 0x47 is the TS sync byte; no real stream
    fp = media.fingerprint(str(p), p.stat().st_size, Config())
    assert fp.undecodable
    assert fp.frames == []
    assert fp.decode_error
