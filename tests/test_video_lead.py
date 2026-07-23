"""Video stage-2 keep-lead ranking (§8 B): resolution → bitrate band → codec → raw bitrate.

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
             "duration_s": None, "codec": None}
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
    # but per-second bitrate is *exactly* equal → must not win on raw size alone;
    # fully-tied key (band + weight + raw eff) → stable tiebreak picks the smaller path.
    # (42_000_000/10.5 == 40_000_000/10.0 == 4_000_000 exactly, so the raw-bitrate
    # tiebreak also ties and the decision genuinely falls to the path.)
    rank = _rank(
        **{"1": {"width": 1920, "height": 1080, "size": 40_000_000, "duration_s": 10.0, "codec": "h264"},
           "2": {"width": 1920, "height": 1080, "size": 42_000_000, "duration_s": 10.5, "codec": "h264"}},
    )
    assert dedup._pick_lead(m, rank, cfg) == 1


def test_video_lead_same_codec_raw_bitrate_breaks_band_tie():
    """Same codec + resolution, effective bitrates within the tie band but NOT equal:
    the trailing raw effective bitrate breaks the tie (higher bitrate = better master),
    NOT the path. Both clips sit in one log band, so the old key would have fallen to
    the path tiebreak (asset 1, smaller path) — the raw-bitrate tiebreak flips it."""
    cfg = Config()
    m = _members(1, 2)
    # 4_050_000 vs 4_300_000 eff (h264, weight 1.0): ~6% apart → same log band 160.
    # Asset 2 has the higher bitrate but the LARGER path, so a win proves raw bitrate
    # (not path) decided.
    rank = _rank(
        **{"1": {"width": 1920, "height": 1080, "size": 40_500_000, "duration_s": 10.0, "codec": "h264"},
           "2": {"width": 1920, "height": 1080, "size": 43_000_000, "duration_s": 10.0, "codec": "h264"}},
    )
    assert dedup._log_band(4_050_000, 10.0) == dedup._log_band(4_300_000, 10.0)  # same band
    lead, level = dedup._group_lead_and_level(m, rank, cfg)
    assert lead == 2  # higher raw bitrate wins, despite the larger path
    assert level == "resolution + bitrate + codec + fine bitrate"


# ---------------------------------------------------------------------------
# internal/external keep-preference tiebreak (§8 B --prefer-internal)
# ---------------------------------------------------------------------------
def _mixed_members(*specs):
    """specs: (asset_id, root_id) — build members spanning internal (root 1) + external."""
    return [(i, {"fid": i, "root_id": rid, "path": f"C:\\lib\\{i}.mp4"}) for i, rid in specs]


def _tied_rank(*ids):
    """Identical rank for every id (a full-key tie) so only the tiebreak decides."""
    return _rank(**{str(i): {"width": 1920, "height": 1080, "size": 10_000_000,
                             "duration_s": 10.0, "codec": "h264"} for i in ids})


def test_video_lead_mixed_tie_defaults_to_external():
    """Full-key tie in a MIXED group → the external copy is the keep-lead by default."""
    cfg = Config()
    m = _mixed_members((1, 1), (2, 2))          # asset 1 internal, asset 2 external
    lead, level = dedup._group_lead_and_level(m, _tied_rank(1, 2), cfg, root_id=1)
    assert lead == 2                             # external wins
    assert level == "internal/external preference"


def test_video_lead_mixed_tie_prefer_internal():
    """--prefer-internal flips a mixed-group tie to the internal copy."""
    cfg = Config()
    m = _mixed_members((1, 1), (2, 2))
    lead, level = dedup._group_lead_and_level(m, _tied_rank(1, 2), cfg,
                                              root_id=1, prefer_internal=True)
    assert lead == 1                             # internal wins
    assert level == "internal/external preference"


def test_video_lead_all_internal_tie_uses_path():
    """An all-internal tie is unaffected by the preference → stable path tiebreak."""
    cfg = Config()
    m = _mixed_members((1, 1), (2, 1))           # both internal
    lead, level = dedup._group_lead_and_level(m, _tied_rank(1, 2), cfg, root_id=1)
    assert lead == 1 and level == "path tiebreak (identical rank)"   # 1.mp4 < 2.mp4


def test_video_lead_preference_never_overrides_quality():
    """The preference sits BELOW the ranking key: a higher-resolution internal copy still
    beats an external one even without --prefer-internal (quality wins, not location)."""
    cfg = Config()
    m = _mixed_members((1, 1), (2, 2))
    rank = _rank(
        **{"1": {"width": 3840, "height": 2160, "size": 10_000_000, "duration_s": 10, "codec": "h264"},
           "2": {"width": 1920, "height": 1080, "size": 10_000_000, "duration_s": 10, "codec": "h264"}},
    )
    lead, level = dedup._group_lead_and_level(m, rank, cfg, root_id=1)   # default (external-pref)
    assert lead == 1 and level == "resolution"   # the 4K internal copy wins on resolution


def test_photo_lead_mixed_tie_prefers_external_then_internal():
    """The internal/external tiebreak applies to photos too (same code path)."""
    cfg = Config()
    m = [(1, {"fid": 1, "root_id": 1, "path": "C:\\lib\\a.jpg"}),
         (2, {"fid": 2, "root_id": 2, "path": "C:\\lib\\b.jpg"})]
    rank = _photo_rank(**{"1": {"width": 4000, "height": 3000, "size": 500_000},
                          "2": {"width": 4000, "height": 3000, "size": 500_000}})
    assert dedup._pick_lead(m, rank, cfg, root_id=1) == 2                       # external default
    assert dedup._pick_lead(m, rank, cfg, root_id=1, prefer_internal=True) == 1  # internal flip


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
# photo keep-lead (§8 B): resolution → format rank → file size
# ---------------------------------------------------------------------------
def _photo_members(*specs):
    """specs: (id, ext) pairs → [(id, instance-dict)] with the ext in the path."""
    return [(i, {"fid": i, "root_id": 1, "path": f"C:\\lib\\{i}.{ext}"}) for i, ext in specs]


def _photo_rank(**by_id):
    out = {}
    for aid, d in by_id.items():
        r = {"media_type": "photo", "width": None, "height": None, "size": None,
             "duration_s": None, "codec": None}
        r.update(d)
        out[int(aid)] = r
    return out


def test_photo_format_rank_ordering():
    assert dedup._photo_format_rank("a.png") == 2      # lossless
    assert dedup._photo_format_rank("a.dng") == 2      # RAW → lossless tier
    assert dedup._photo_format_rank("a.heic") == 1     # efficient lossy
    assert dedup._photo_format_rank("a.avif") == 1
    assert dedup._photo_format_rank("a.jpg") == 0      # other lossy
    assert dedup._photo_format_rank("a.webp") == 0


def test_photo_lead_resolution_dominates():
    cfg = Config()
    m = _photo_members((1, "jpg"), (2, "png"))
    # Asset 2 is a lossless PNG with a bigger file, but LOWER resolution → asset 1 wins.
    rank = _photo_rank(
        **{"1": {"width": 4000, "height": 3000, "size": 100_000},
           "2": {"width": 2000, "height": 1500, "size": 900_000}},
    )
    assert dedup._pick_lead(m, rank, cfg) == 1  # higher resolution wins outright


def test_photo_lead_lossless_beats_lossy():
    """At equal resolution a lossless master outranks a lossy sibling regardless of size (§8 B)."""
    cfg = Config()
    m = _photo_members((1, "png"), (2, "jpg"))
    rank = _photo_rank(
        **{"1": {"width": 4000, "height": 3000, "size": 300_000},   # small lossless master
           "2": {"width": 4000, "height": 3000, "size": 900_000}},  # big lossy export
    )
    assert dedup._pick_lead(m, rank, cfg) == 1  # PNG master wins on format rank


def test_photo_lead_heic_master_beats_jpeg_export():
    """A HEIC master outranks its JPEG export at equal resolution (§8 B).

    HEIC/AVIF are efficient-lossy (format rank 1) vs JPEG's 0, so an iPhone HEIC
    original beats its JPEG export even when the JPEG's file is larger — file size
    is only a tiebreak WITHIN a format, never across (it lies cross-format).
    """
    cfg = Config()
    m = _photo_members((1, "heic"), (2, "jpg"))
    rank = _photo_rank(
        **{"1": {"width": 4000, "height": 3000, "size": 1_400_000},
           "2": {"width": 4000, "height": 3000, "size": 1_900_000}},  # bigger JPEG, still loses
    )
    assert dedup._pick_lead(m, rank, cfg) == 1  # HEIC master


def test_photo_lead_size_breaks_tie_within_format():
    """Same resolution + same format → the larger (less-compressed) file wins (§8 B)."""
    cfg = Config()
    m = _photo_members((1, "jpg"), (2, "jpg"))
    rank = _photo_rank(
        **{"1": {"width": 4000, "height": 3000, "size": 620_000},   # less compressed
           "2": {"width": 4000, "height": 3000, "size": 416_000}},  # more compressed
    )
    assert dedup._pick_lead(m, rank, cfg) == 1  # bigger JPEG = higher quality within-format


# ---------------------------------------------------------------------------
# keep-lead DECISION LEVEL (drives the stage-2 lead-pick stats, §8 B)
# ---------------------------------------------------------------------------
def test_lead_level_resolution():
    cfg = Config()
    m = _photo_members((1, "jpg"), (2, "jpg"))
    rank = _photo_rank(**{"1": {"width": 4000, "height": 3000, "size": 100_000},
                          "2": {"width": 2000, "height": 1500, "size": 900_000}})
    lead, level = dedup._group_lead_and_level(m, rank, cfg)
    assert lead == 1 and level == "resolution"


def test_lead_level_resolution_plus_format():
    cfg = Config()
    m = _photo_members((1, "png"), (2, "jpg"))
    rank = _photo_rank(**{"1": {"width": 4000, "height": 3000, "size": 100_000},
                          "2": {"width": 4000, "height": 3000, "size": 900_000}})
    lead, level = dedup._group_lead_and_level(m, rank, cfg)
    assert lead == 1 and level == "resolution + format"


def test_lead_level_resolution_plus_format_plus_size():
    cfg = Config()
    m = _photo_members((1, "jpg"), (2, "jpg"))
    rank = _photo_rank(**{"1": {"width": 4000, "height": 3000, "size": 620_000},
                          "2": {"width": 4000, "height": 3000, "size": 416_000}})
    lead, level = dedup._group_lead_and_level(m, rank, cfg)
    assert lead == 1 and level == "resolution + format + size"


def test_lead_level_path_tiebreak():
    """Fully-tied key (same res/format/size) → decided by the stable path tiebreak."""
    cfg = Config()
    m = _photo_members((1, "jpg"), (2, "jpg"))
    rank = _photo_rank(**{"1": {"width": 4000, "height": 3000, "size": 500_000},
                          "2": {"width": 4000, "height": 3000, "size": 500_000}})
    lead, level = dedup._group_lead_and_level(m, rank, cfg)
    assert lead == 1 and level == "path tiebreak (identical rank)"  # 1.jpg < 2.jpg


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
