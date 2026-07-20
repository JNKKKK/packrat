"""Responsive-layout (Level B) tests — full-terminal frames at multiple sizes.

The TUI fills the whole terminal (≥ the 100×24 reference) and reflows via
:mod:`packrat.tui.geometry` (the surplus model). These tests lock in the
invariants that make that safe:

- every screen renders to *exactly* the terminal size, every row full-width, at
  several sizes (100×24, 140×40, 200×60);
- larger terminals show MORE rows (pagination budgets scale with height) and wider
  content (longer paths visible) — i.e. surplus is actually used;
- at the 100×24 reference the geometry reduces to the original fixed constants
  (guarding that the golden/reference frames never drift).
"""

from __future__ import annotations

import asyncio

from packrat.tui import demo
from packrat.tui.app import PackratApp
from packrat.tui.geometry import REFERENCE, Geometry

SIZES = [(100, 24), (140, 40), (200, 60), (120, 30), (100, 50)]


def _drive(size, coro_fn):
    async def runner():
        app = PackratApp(offline=True)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await coro_fn(app, pilot)
    asyncio.run(runner())


def _assert_exact_size(frame: str, w: int, h: int) -> None:
    # DISPLAY width, not len(): a row with CJK wide chars has fewer characters than
    # cells, so we measure terminal cells (the frame must fill exactly w×h on screen).
    from packrat.tui.layout import cell_width
    rows = frame.split("\n")
    assert len(rows) == h, f"height {len(rows)} != {h}"
    for i, r in enumerate(rows):
        assert cell_width(r) == w, f"row {i} display width {cell_width(r)} != {w}: {r!r}"


# --- every screen renders to exactly the terminal size ---------------------
def test_dashboard_fills_every_size():
    for w, h in SIZES:
        async def scenario(app, pilot, w=w, h=h):
            _assert_exact_size(app.screen.current_frame, w, h)
        _drive((w, h), scenario)


def test_all_screens_fill_at_140x40():
    async def scenario(app, pilot):
        # dashboard
        _assert_exact_size(app.screen.current_frame, 140, 40)
        # roots max
        await pilot.press("r"); await pilot.press("r")
        _assert_exact_size(app.screen.current_frame, 140, 40)
        # add-root form
        await pilot.press("a")
        _assert_exact_size(app.screen.current_frame, 140, 40)
        await pilot.press("escape")
        # root detail
        await pilot.press("enter")
        _assert_exact_size(app.screen.current_frame, 140, 40)
        # job card
        await pilot.press("enter")
        _assert_exact_size(app.screen.current_frame, 140, 40)
        await pilot.press("escape"); await pilot.press("escape"); await pilot.press("escape")
        # queue max
        await pilot.press("q"); await pilot.press("q")
        _assert_exact_size(app.screen.current_frame, 140, 40)
    _drive((140, 40), scenario)


# --- surplus is actually used: more rows + wider content -------------------
def test_taller_terminal_shows_more_dashboard_root_rows():
    # The dashboard Roots box window grows with terminal height (dash_roots_rows).
    def run(size):
        holder = {}
        async def scenario(app, pilot):
            holder["n"] = _visible_root_rows(app)   # dashboard (default screen)
        _drive(size, scenario)
        return holder["n"]
    n_small = run((100, 24))            # dash_roots_rows == 4 → ≤4 rows
    n_big = run((100, 44))              # taller → more rows shown
    assert n_big > n_small, (n_small, n_big)


def test_wider_terminal_shows_longer_paths():
    # A very long path elides in a narrow row and shows fully in a wide one — the
    # path is a grow cell, so more terminal width == more path visible.
    from packrat.tui import render
    long_root = {
        "name": "Deep", "kind": "library", "asset_count": 5,
        "last_scan_at": "2026-07-10T00:00:00", "last_dedup_at": None,
        "path": r"\\nas\share\a\very\deeply\nested\and\overlong\folder\structure\that\wont\fit\file",
    }
    narrow = render.root_row_wide(long_root, now="2026-07-15T00:00:00", width=80)
    wide = render.root_row_wide(long_root, now="2026-07-15T00:00:00", width=200)
    assert "…" in narrow                      # elided in a narrow row
    assert long_root["path"] in wide          # fully shown in a wide row
    assert len(narrow) == 80 and len(wide) == 200


def test_queue_sections_grow_with_height():
    def recent_rows_visible(size):
        holder = {}
        async def scenario(app, pilot):
            await pilot.press("q"); await pilot.press("q")  # QueueMax
            # count recent-job lines (they start with " #6..")
            holder["f"] = app.screen.current_frame
        _drive(size, scenario)
        return holder["f"].count(" #6")
    small = recent_rows_visible((100, 24))
    big = recent_rows_visible((100, 50))
    assert big > small, (small, big)


# --- live resize -----------------------------------------------------------
def test_live_resize_reflows():
    async def scenario(app, pilot):
        _assert_exact_size(app.screen.current_frame, 100, 24)
        await pilot.resize_terminal(160, 45)
        await pilot.pause()
        _assert_exact_size(app.screen.current_frame, 160, 45)
        await pilot.resize_terminal(110, 28)
        await pilot.pause()
        _assert_exact_size(app.screen.current_frame, 110, 28)
    _drive((100, 24), scenario)


# --- footer wraps instead of truncating ------------------------------------
def test_queue_footer_wraps_at_narrow_width():
    """The long Queue footer wraps to 2 rows at 100 wide (not truncated)."""
    async def scenario(app, pilot):
        await pilot.press("q"); await pilot.press("q")   # QueueMax
        rows = app.screen.current_frame.split("\n")
        # the full "[x] cancel all" wording survives (would be trimmed if truncated)
        assert "[x] cancel all" in app.screen.current_frame
        assert "Esc back" in app.screen.current_frame
        # footer occupies the last TWO body rows (rows[-3], rows[-2]) at 100 wide
        assert "section" in rows[-3] and "[Enter] detail" in rows[-2]
    _drive((100, 24), scenario)


def test_queue_footer_single_line_when_wide():
    """On a wide terminal the same footer fits on one line."""
    async def scenario(app, pilot):
        await pilot.press("q"); await pilot.press("q")
        rows = app.screen.current_frame.split("\n")
        # the whole footer is on the last body row; the row above is a job/blank line
        assert "section" in rows[-2] and "Esc back" in rows[-2]
        assert "section" not in rows[-3]
    _drive((150, 30), scenario)


def test_geometry_footer_rows_shrinks_content():
    from packrat.tui.geometry import Geometry
    one = Geometry(100, 24, footer_rows=1)
    two = Geometry(100, 24, footer_rows=2)
    assert two.content_rows == one.content_rows - 1   # a 2-line footer eats a row


# --- CJK content keeps the frame exactly sized -----------------------------
def test_cjk_root_does_not_break_layout():
    """A root with Chinese name + path renders every row at the exact display width."""
    from packrat.tui import demo
    assert any("手机相册" in r["name"] for r in demo.ROOTS)   # demo has the CJK root

    async def scenario(app, pilot):
        # dashboard, roots interface, and the CJK root's detail all stay exact-size
        _assert_exact_size(app.screen.current_frame, 120, 34)
        await pilot.press("r"); await pilot.press("r")        # RootsMax
        assert "手机相册" in app.screen.current_frame
        _assert_exact_size(app.screen.current_frame, 120, 34)
    _drive((120, 34), scenario)


# --- reference reduces to the fixed constants ------------------------------
def test_reference_geometry_matches_fixed_constants():
    g = REFERENCE
    assert (g.w, g.h) == (100, 24)
    assert g.cw == 98 and g.content_w == 96
    # roots/queue boxes are full content width now; collection is fixed, logo fills the rest
    assert g.roots_w == 96 and g.collection_w == 29 and g.queue_w == 96
    assert g.logo_w == 66 and g.row_w_compact == 92
    # TOP_ROWS=8 now (Collection box gained the "Size" line), so the dashboard
    # interiors split content_rows−8−5 == 8 → 4 roots + 4 queue.
    assert g.dash_roots_rows == 4 and g.dash_queue_rows == 4
    assert g.roots_list_rows == 18 and g.jobs_rows == 4
    assert g.queued_rows == 8 and g.recent_rows == 7   # pagers moved to headers


def test_geometry_surplus_is_split_and_conserved():
    # vertical surplus is fully distributed (no rows lost to rounding)
    for h in (24, 30, 40, 41, 60):
        g = Geometry(100, h)
        # dashboard: roots + queue interiors share the space below the fixed top
        assert g.dash_roots_rows + g.dash_queue_rows == g.content_rows - g.TOP_ROWS - 5
        # queue interface: queued + recent fill the frame (pagers on the headers)
        assert g.queued_rows + g.recent_rows == g.content_rows - 6
        # roots interface fills the frame below its 3 header rows
        assert g.roots_list_rows == g.content_rows - 3


def _visible_root_rows(app) -> int:
    """Count root data rows in a RootsMax frame (a dot glyph + a drive/UNC path)."""
    import re
    n = 0
    for ln in app.screen.current_frame.split("\n"):
        has_dot = any(d in ln for d in ("◉", "◐", "○"))
        is_legend = "scanned only" in ln          # the dot-legend line
        has_path = bool(re.search(r"[A-Z]:\\|\\\\", ln))
        if has_dot and has_path and not is_legend:
            n += 1
    return n
