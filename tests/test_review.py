"""review.py — audit-write atomicity + root-name sanitization (§8.1)."""

from __future__ import annotations

import json
import os

from packrat import review


def test_write_audit_is_atomic_and_leaves_no_tmp(tmp_path):
    # A completed write lands the full JSON and leaves no stray .tmp sibling.
    d = tmp_path / "run"
    d.mkdir()
    p = review.write_audit(str(d), "proposed.json", {"a": 1, "nested": [1, 2, 3]})
    assert json.loads(open(p, encoding="utf-8").read()) == {"a": 1, "nested": [1, 2, 3]}
    assert [f for f in os.listdir(d) if f.endswith(".tmp")] == []


def test_write_audit_overwrites_completely(tmp_path):
    # A re-write (dedup appends across stages) replaces the file atomically.
    d = tmp_path / "run"
    d.mkdir()
    review.write_audit(str(d), "proposed.json", {"stage": 1})
    p = review.write_audit(str(d), "proposed.json", {"stage": 2, "more": "data"})
    assert json.loads(open(p, encoding="utf-8").read()) == {"stage": 2, "more": "data"}


def test_safe_name_neutralizes_dot_traversal():
    # A root named ".." / "." must NOT become a path-traversal audit-dir component.
    assert review._safe_name("..") == "root"
    assert review._safe_name(".") == "root"
    assert review._safe_name("...") == "root"
    assert review._safe_name("  ..  ") == "root"
    # Normal names still pass through (alnum + space/._- kept).
    assert review._safe_name("iPhone") == "iPhone"
    assert review._safe_name("My Photos_2024") == "My Photos_2024"
    # Illegal chars → underscore, not dropped into traversal.
    assert "/" not in review._safe_name("a/b") and "\\" not in review._safe_name("a\\b")
