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
    """The helper estimates LENGTH from packet timestamps when the container reports none —
    ~frames / rate (15 fps here → ~2 s for 30 frames). Opens its own container by path, so
    it leaves the caller's container untouched."""
    pytest.importorskip("av")
    p = tmp_path / "len.ts"
    _write_ts(p, frames=30)
    dur = media._duration_by_demux(str(p))
    assert dur is not None
    assert 1.0 <= dur <= 4.0, dur                        # generous band around ~2 s


def test_duration_by_demux_subtracts_start_time(tmp_path):
    """The estimate is a LENGTH, not an absolute end-timestamp: a transport stream's non-zero
    start_time (PCR offset) is subtracted, so it stays ~ (last_pts − start)·tb and matches the
    native duration — otherwise seek targets offset by start_time would overshoot past EOF."""
    av = pytest.importorskip("av")
    p = tmp_path / "off.ts"
    _write_ts(p, frames=30)
    # A synthetic .ts carries start_time = 12000 (1/90000 tb ≈ 0.133 s). The demux length must
    # NOT include it: last_pts (~end) minus start ≈ the true content length (~2 s), not ~2.13 s.
    c = av.open(str(p))
    try:
        start_s = (c.streams.video[0].start_time or 0) * c.streams.video[0].time_base
    finally:
        c.close()
    dur = media._duration_by_demux(str(p))
    assert dur is not None
    # Length excludes start_time: it's below (last_pts·tb), i.e. below length+start.
    assert dur < 4.0 and dur < (4.0 + float(start_s) + 1.0)
    assert 1.0 <= dur <= 4.0


def test_duration_by_demux_none_on_unopenable(tmp_path):
    """A path that isn't a decodable container → None (caller keeps the single-frame path),
    never raises."""
    pytest.importorskip("av")
    p = tmp_path / "bogus.ts"
    p.write_bytes(b"\x47" + b"\x00" * 512)               # TS sync byte, no real stream
    assert media._duration_by_demux(str(p)) is None


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
    cfg = Config()
    fp = media.fingerprint(str(p), p.stat().st_size, cfg)
    assert not fp.undecodable, fp.decode_error
    assert fp.duration_s and fp.duration_s > 0
    # Full sample count (seek path works on this synthetic .ts); well above the dedup floor.
    assert len(fp.frames) == cfg.video.sample_frames
    assert len(fp.frames) >= cfg.video.min_comparable_frames
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


# --- the core robustness path: broken seek → sequential fallback -------------
def test_broken_seek_triggers_sequential_fallback(tmp_path, monkeypatch):
    """THE scenario the feature exists for: mid-file seeking silently under-delivers on a
    transport stream, so _probe_video must fall back to _sample_sequential and recover the
    full sample. Simulated by forcing the seek sampler to under-deliver (the real .ts seek
    breakage), then asserting the end result still has the full frame count — i.e. the
    fallback ran and won. Guards the fallback against silent regression (it isn't reached by
    the seekable synthetic clips otherwise)."""
    p = tmp_path / "clip.ts"
    _write_ts(p, frames=60)
    cfg = Config()

    real_seek_sampler = media._sample_by_seek
    seq_calls = {"n": 0}

    def crippled_seek(container, vs, tb, targets, *, max_edge, profiler):
        # Model a transport stream where seeking returns only the first couple targets
        # (5–11 range) — enough to have passed the OLD `< min_comparable_frames` gate.
        full = real_seek_sampler(container, vs, tb, targets, max_edge=max_edge, profiler=profiler)
        return full[:6]

    real_seq = media._sample_sequential

    def counting_seq(*a, **k):
        seq_calls["n"] += 1
        return real_seq(*a, **k)

    monkeypatch.setattr(media, "_sample_by_seek", crippled_seek)
    monkeypatch.setattr(media, "_sample_sequential", counting_seq)

    dur, w, h, cap, codec, frames = media._probe_video(str(p), cfg.video)
    assert seq_calls["n"] == 1                           # a 6-frame shortfall (< 12) triggered it
    assert len(frames) == cfg.video.sample_frames        # sequential recovered the full count
    assert all(len(fr.pdq_bits) == 32 for fr in frames)


def test_sample_sequential_fills_every_covered_slot(tmp_path):
    """_sample_sequential must not drop target slots when one frame covers several (short /
    low-fps clips): every satisfied target gets a FrameSig, so the slot count == targets
    satisfied, preserving frame_index alignment with a full-count peer (§5.3)."""
    av = pytest.importorskip("av")
    p = tmp_path / "few.ts"
    _write_ts(p, frames=3)                               # far fewer real frames than 12 targets
    c = av.open(str(p))
    try:
        vs = c.streams.video[0]
        tb = vs.time_base
        start = vs.start_time or 0
        # 12 ascending targets across a duration that outruns the 3 real frames, so late
        # targets are covered by the last decoded frame — the multi-cover case.
        dur = media._duration_by_demux(str(p)) or 0.2
        targets = [start + int(dur * (k + 0.5) / 12 / tb) for k in range(12)]
        frames = media._sample_sequential(c, vs, tb, targets, max_edge=0,
                                          profiler=media.NULL_PROFILER)
    finally:
        c.close()
    # Indices are contiguous 0..len-1 (no gaps) even though real frames << targets.
    assert [fr.frame_index for fr in frames] == list(range(len(frames)))
    assert len(frames) >= 3                              # at least one slot per real frame
