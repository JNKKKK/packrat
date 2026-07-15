"""Ignore set: allowlist + gitignore-style glob matching (§8 A1)."""

from __future__ import annotations

from packrat.config import Config
from packrat.ignore import IgnoreSet, ext_of, is_junk_dirent


def _set(*globs):
    return IgnoreSet.build(Config(), list(globs))


def test_ext_of():
    assert ext_of("IMG_1234.JPG") == "jpg"
    assert ext_of("movie.MP4") == "mp4"
    assert ext_of("noext") == ""


def test_media_allowlist():
    s = _set()
    assert s.is_media("x.jpg") and s.is_media("y.HEIC") and s.is_media("v.mp4")
    assert not s.is_media("x.txt") and not s.is_media("x.pdf") and not s.is_media("x.aae")


def test_raw_off_by_default():
    assert not _set().is_media("x.cr3")


def test_raw_on_when_enabled():
    from dataclasses import replace

    cfg = Config()
    cfg = replace(cfg, allowlist=replace(cfg.allowlist, raw=True))
    s = IgnoreSet.build(cfg, [])
    assert s.is_media("x.cr3") and s.is_media("y.dng")


def test_builtin_junk_names():
    s = _set()
    assert s.is_file_ignored("Thumbs.db")
    assert s.is_file_ignored("a/b/desktop.ini")
    assert s.is_file_ignored(".DS_Store")


def test_builtin_review_and_lnk():
    s = _set()
    assert s.is_dir_pruned("_packrat_review")
    assert s.is_dir_pruned("a/_packrat_review")
    assert s.is_file_ignored("_packrat_review/x.lnk")
    assert s.is_file_ignored("anywhere/shortcut.lnk")


def test_extension_glob_at_depth():
    s = _set("*.tmp")
    assert s.is_file_ignored("a/b/c.tmp")
    assert s.is_file_ignored("c.tmp")
    assert not s.is_file_ignored("c.jpg")


def test_double_star_cache():
    s = _set("**/cache/**")
    assert s.is_file_ignored("a/cache/x.jpg")
    assert s.is_dir_pruned("cache")
    assert s.is_dir_pruned("a/b/cache")


def test_dir_only_trailing_slash():
    s = _set("Screenshots/")
    assert s.is_dir_pruned("Screenshots")
    # a *file* named Screenshots is not matched by a dir-only rule
    assert not s.is_file_ignored("Screenshots")


def test_anchored_leading_slash():
    s = _set("/top.txt")
    assert s.is_file_ignored("top.txt")
    assert not s.is_file_ignored("sub/top.txt")


def test_char_class_and_question():
    s = _set("IMG_?.AAE", "vid[0-9].mov")
    assert s.is_file_ignored("IMG_4.AAE")
    assert not s.is_file_ignored("IMG_44.AAE")  # ? is exactly one char
    assert s.is_file_ignored("vid3.mov")
    assert not s.is_file_ignored("vidx.mov")


def test_junk_dirent_attrs():
    assert is_junk_dirent(100, 0x2) == "hidden"
    assert is_junk_dirent(100, 0x4) == "system"
    assert is_junk_dirent(0, 0) == "zero-byte"
    assert is_junk_dirent(100, 0) is None
