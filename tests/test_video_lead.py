"""Video stage-2 keep-lead ranking (§8 B): resolution → effective-bitrate band → codec.

Unit-level: drives ``dedup._pick_lead`` + the band/effective-bitrate helpers with
synthetic rank rows, so no video decode is needed. The codec-weight fixes the
HEVC-master-vs-H.264-export trap; resolution still dominates.
"""

from __future__ import annotations

import pytest

from packrat.config import Config
from packrat.jobs import dedup


def _members(*ids):
    # (asset_id, instance-dict) — path only matters for the stable tiebreak.
    return [(i, {"fid": i, "root_id": 1, "path": f"C:\\lib\\{i}.mp4"}) for i in ids]


def _rank(**by_id):
    """by_id: asset_id -> dict of rank fields; media_type defaults to video."""
    out = {}
    for aid, d in by_id.items():
        r = {"media_type": "video", "width": None, "height": None, "size": None,
             "detail_score": None, "duration_s": None, "codec": None}
        r.update(d)
        out[int(aid)] = r
    return out


def test_log_band_ties_within_pct():
    assert dedup._log_band(4.0, 10.0) == dedup._log_band(4.3, 10.0)   # within 10%
    assert dedup._log_band(4.0, 10.0) != dedup._log_band(8.0, 10.0)   # 2x apart
    assert dedup._log_band(0.0, 10.0) == -1                            # sentinel


def test_effective_bitrate_weight_and_duration_fallback():
    # size/duration × weight
    assert dedup._effective_bitrate(4_000_000, 10.0, 2.0) == 800_000.0
    # no duration → raw size × weight (still comparable within a group)
    assert dedup._effective_bitrate(4_000_000, None, 2.0) == 8_000_000.0
    assert dedup._effective_bitrate(0, 10.0, 2.0) == 0.0


def test_video_lead_resolution_dominates():
    cfg = Config()
    m = _members(1, 2)
    rank = _rank(
        **{"1": {"width": 1920, "height": 1080, "size": 20_000_000, "duration_s": 10, "codec": "h264"},
           "2": {"width": 1280, "height": 720, "size": 40_000_000, "duration_s": 10, "codec": "hevc"}},
    )
    # Asset 1 is lower-bitrate but HIGHER resolution → it wins outright.
    assert dedup._pick_lead(m, rank, cfg) == 1


def test_video_lead_codec_weight_breaks_bitrate_tie():
    cfg = Config()  # hevc=2.0, h264=1.0, tie_pct=10
    m = _members(1, 2)
    # Same resolution + duration. HEVC master 4 Mb/s vs H.264 export ~8 Mb/s → equal
    # EFFECTIVE bitrate (4×2 == 8×1) → same band → codec weight picks the HEVC master.
    rank = _rank(
        **{"1": {"width": 1920, "height": 1080, "size": 5_000_000, "duration_s": 10, "codec": "hevc"},
           "2": {"width": 1920, "height": 1080, "size": 10_000_000, "duration_s": 10, "codec": "h264"}},
    )
    assert dedup._pick_lead(m, rank, cfg) == 1  # HEVC master, not the H.264 re-export


def test_video_lead_higher_effective_bitrate_wins_when_bands_differ():
    cfg = Config()
    m = _members(1, 2)
    # Same codec + resolution; asset 2 has clearly higher bitrate (>10% apart) → wins.
    rank = _rank(
        **{"1": {"width": 1920, "height": 1080, "size": 4_000_000, "duration_s": 10, "codec": "h264"},
           "2": {"width": 1920, "height": 1080, "size": 12_000_000, "duration_s": 10, "codec": "h264"}},
    )
    assert dedup._pick_lead(m, rank, cfg) == 2


def test_video_lead_duration_normalizes_size():
    cfg = Config()
    m = _members(1, 2)
    # Asset 2 is a hair longer (within the duration tolerance) so its FILE is bigger,
    # but per-second bitrate is essentially equal → must not win on raw size alone;
    # equal band → stable tiebreak picks the smaller path (asset 1).
    rank = _rank(
        **{"1": {"width": 1920, "height": 1080, "size": 40_000_000, "duration_s": 10.0, "codec": "h264"},
           "2": {"width": 1920, "height": 1080, "size": 41_200_000, "duration_s": 10.3, "codec": "h264"}},
    )
    assert dedup._pick_lead(m, rank, cfg) == 1


def test_video_lead_config_weights_override():
    # If the user flips the weights (say they distrust the HEVC-is-better heuristic),
    # the lead follows config — no hardcoded codec preference.
    from dataclasses import replace

    base = Config()
    flipped = replace(base, match=replace(base.match, codec_weights={"hevc": 1.0, "h264": 2.0}))
    m = _members(1, 2)
    rank = _rank(
        **{"1": {"width": 1920, "height": 1080, "size": 5_000_000, "duration_s": 10, "codec": "hevc"},
           "2": {"width": 1920, "height": 1080, "size": 5_000_000, "duration_s": 10, "codec": "h264"}},
    )
    # Equal raw bitrate; with h264 weighted higher, asset 2 wins.
    assert dedup._pick_lead(m, rank, flipped) == 2


# ---------------------------------------------------------------------------
# photo keep-lead (§8 B): resolution → lossless tier → detail_score BAND → size
# ---------------------------------------------------------------------------
def _photo_members(*ids, ext="jpg"):
    return [(i, {"fid": i, "root_id": 1, "path": f"C:\\lib\\{i}.{ext}"}) for i in ids]


def _photo_rank(**by_id):
    out = {}
    for aid, d in by_id.items():
        r = {"media_type": "photo", "width": None, "height": None, "size": None,
             "detail_score": None, "duration_s": None, "codec": None}
        r.update(d)
        out[int(aid)] = r
    return out


def test_photo_lead_size_breaks_detail_tie():
    """Near-equal detail_scores (within detail_tie_pct) tie → file size decides (§8 B).

    This is the high-quality-band flip fix: a slightly-more-compressed copy can score
    a marginally HIGHER detail_score (JPEG blocking is high-freq), so within the band
    the larger FILE — the clean monotonic quality proxy — must win, not raw detail.
    """
    cfg = Config()  # detail_tie_pct = 15
    m = _photo_members(1, 2)
    # Asset 2 has a hair-higher detail (noise) but asset 1 is the larger, less-
    # compressed file. Within 15% → same band → size picks asset 1.
    rank = _photo_rank(
        **{"1": {"width": 4000, "height": 3000, "size": 620_000, "detail_score": 554_000},
           "2": {"width": 4000, "height": 3000, "size": 416_000, "detail_score": 558_000}},
    )
    assert dedup._pick_lead(m, rank, cfg) == 1  # larger file wins the tie, not higher detail


def test_photo_lead_detail_separates_heavy_compression():
    """When detail_scores are far apart (>tie_pct), detail_score still decides (§8 B)."""
    cfg = Config()
    m = _photo_members(1, 2)
    # Heavy compression drops detail well outside the band → higher detail wins even
    # though (here) it's also the bigger file; the point is detail still separates.
    rank = _photo_rank(
        **{"1": {"width": 4000, "height": 3000, "size": 600_000, "detail_score": 550_000},
           "2": {"width": 4000, "height": 3000, "size": 165_000, "detail_score": 400_000}},
    )
    assert dedup._pick_lead(m, rank, cfg) == 1


def test_photo_lead_lossless_tier_above_detail_band():
    """A lossless master outranks a lossy sibling regardless of detail/size (§8 B)."""
    cfg = Config()
    m = [(1, {"fid": 1, "root_id": 1, "path": "C:\\lib\\1.png"}),   # lossless
         (2, {"fid": 2, "root_id": 1, "path": "C:\\lib\\2.jpg"})]   # lossy, bigger + higher detail
    rank = _photo_rank(
        **{"1": {"width": 4000, "height": 3000, "size": 300_000, "detail_score": 500_000},
           "2": {"width": 4000, "height": 3000, "size": 900_000, "detail_score": 900_000}},
    )
    assert dedup._pick_lead(m, rank, cfg) == 1  # PNG master wins on the lossless tier


def test_photo_lead_resolution_dominates():
    cfg = Config()
    m = _photo_members(1, 2)
    rank = _photo_rank(
        **{"1": {"width": 4000, "height": 3000, "size": 100_000, "detail_score": 100_000},
           "2": {"width": 2000, "height": 1500, "size": 900_000, "detail_score": 900_000}},
    )
    assert dedup._pick_lead(m, rank, cfg) == 1  # higher resolution wins outright


# ---------------------------------------------------------------------------
# scan-capture half: codec persisted to assets (unit tests can't cover this)
# ---------------------------------------------------------------------------
def test_scan_captures_video_codec(tmp_path, monkeypatch):
    av = pytest.importorskip("av")
    import time

    import numpy as np

    from packrat import db as _db
    from packrat.jobs import JobQueue
    from packrat.jobs import scan as _scan  # noqa: F401
    from packrat.roots import register

    monkeypatch.setenv("PACKRAT_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    p = lib / "clip.mp4"
    c = av.open(str(p), "w")
    s = c.add_stream("libx264", rate=15)
    s.width, s.height, s.pix_fmt = 320, 240, "yuv420p"
    for i in range(30):
        f = np.random.default_rng(i).integers(0, 256, (240, 320, 3), dtype=np.uint8)
        for pk in s.encode(av.VideoFrame.from_ndarray(f, format="rgb24")):
            c.mux(pk)
    for pk in s.encode():
        c.mux(pk)
    c.close()

    _db.init_db().close()
    conn = _db.connect(check_same_thread=False)
    d = _db.Database(conn)
    q = JobQueue(d)
    try:
        root = register(d, str(lib))
        jid = q.submit("scan", {"root_id": root["id"]})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = d.query_one("SELECT status FROM jobs WHERE id=?", (jid,))
            if row and row["status"] != "running":
                break
            time.sleep(0.02)
        a = d.query_one("SELECT media_type, codec, duration_s FROM assets")
        assert a["media_type"] == "video"
        assert a["codec"] == "h264"          # captured from the decode probe
        assert a["duration_s"] and a["duration_s"] > 0
    finally:
        q.shutdown()
        d.close()
