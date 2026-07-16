"""Config auto-create / reload / fallback / malformed handling (§9.2)."""

from __future__ import annotations

import pytest

from packrat import config


def test_defaults_match_plan():
    c = config.Config()
    assert c.match.t_photo_recompress == 10
    assert c.match.t_photo_edit == 32
    assert c.match.t_photo_recompress < c.match.t_photo_edit  # band ordering (§5.3)
    assert c.match.t_match_video == 90
    # video keep-lead knobs (§8 B)
    assert c.match.video_bitrate_tie_pct == 10.0
    assert c.match.codec_weights["hevc"] == 2.0 > c.match.codec_weights["h264"] == 1.0
    assert c.video.sample_frames == 12
    assert c.video.frame_match_fraction == 0.60
    assert c.video.min_comparable_frames == 5
    assert c.smb.scan_workers == 6
    assert c.review.low_quality_hint == 50
    assert c.audit.retention_days == 0
    assert c.fastpath.mtime_tolerance_s == 2.0
    assert c.allowlist.raw is False


def test_ensure_config_is_idempotent(packrat_home):
    p = config.ensure_config()
    assert p.exists()
    first = p.read_text(encoding="utf-8")
    config.ensure_config()  # must not overwrite
    assert p.read_text(encoding="utf-8") == first
    # the written file parses back to defaults
    c = config.load_config(p)
    assert c.match.t_photo_edit == 32


def test_missing_file_yields_defaults(tmp_path):
    c = config.load_config(tmp_path / "nope.toml")
    assert c.match.t_photo_edit == 32


def test_partial_and_unknown_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        "[match]\nt_photo_edit = 99\n\n[video]\nsample_frames = 6\nbogus = 1\n"
        "\n[unknownsection]\nx = 1\n",
        encoding="utf-8",
    )
    c = config.load_config(p)
    assert c.match.t_photo_edit == 99  # set
    assert c.match.t_photo_recompress == 10  # default preserved
    assert c.match.t_match_video == 90  # default preserved
    assert c.video.sample_frames == 6
    assert c.video.frame_match_fraction == 0.60  # default preserved


def test_malformed_raises(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("not = = valid [[[", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load_config(p)


def test_allowlist_raw_and_media_exts(tmp_path):
    p = tmp_path / "al.toml"
    p.write_text('[allowlist]\nraw = true\n', encoding="utf-8")
    c = config.load_config(p)
    assert c.allowlist.raw is True
    exts = c.allowlist.media_exts()
    assert "cr3" in exts and "heic" in exts and "mp4" in exts

    c2 = config.Config()
    assert "cr3" not in c2.allowlist.media_exts()  # RAW off by default
