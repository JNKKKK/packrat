"""Unit tests for the pure navigation state machine (component-plan §Nav).

The focus→maximize table (§focus model) is pure logic — tested here without a
Textual pilot. The live end-to-end drive is in ``test_tui_app.py``.
"""

from __future__ import annotations

from packrat.tui.nav import DashboardFocus


# --- DashboardFocus: the focus→maximize table -----------------------------
def test_first_press_focuses():
    fs = DashboardFocus()
    assert fs.press("r") is None
    assert fs.target == "roots" and fs.focused


def test_second_press_maximizes():
    fs = DashboardFocus()
    fs.press("r")
    assert fs.press("r") == "maximize:roots"


def test_peer_press_switches_focus():
    fs = DashboardFocus()
    fs.press("r")
    assert fs.press("q") is None          # focusing queue unfocuses roots
    assert fs.target == "queue"


def test_queue_second_press_maximizes():
    fs = DashboardFocus()
    fs.press("q")
    assert fs.press("q") == "maximize:queue"


def test_escape_unfocuses():
    fs = DashboardFocus()
    fs.press("r")
    assert fs.escape() is True and fs.target is None
    assert fs.escape() is False           # already unfocused → not consumed


def test_move_clamps_within_roots():
    fs = DashboardFocus(roots_len=3)
    fs.press("r")
    fs.move(-1)
    assert fs.roots_cursor == 0           # clamped at top
    fs.move(1); fs.move(1); fs.move(1); fs.move(1)
    assert fs.roots_cursor == 2           # clamped at bottom (len-1)


def test_move_targets_focused_box_only():
    fs = DashboardFocus(roots_len=3, queue_len=4)
    fs.press("q")
    fs.move(1)
    assert fs.queue_cursor == 1 and fs.roots_cursor == 0


def test_unknown_key_is_noop():
    fs = DashboardFocus()
    assert fs.press("z") is None and fs.target is None
