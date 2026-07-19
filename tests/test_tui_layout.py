"""Invariant tests for the pure TUI layout layer (M6-component-plan §Testing).

These are the cheap, high-value "invariant net" that catches the whole class of
"window grew / border eaten / column misaligned" bugs §12 exists to prevent:
``len(row(w, …)) == w`` for any cells, ``fit(…, budget).rows`` is always exactly
``budget``, and the §12 middle-elide / status-dot rules. The pure functions need
no Textual runtime, so these run as plain string assertions.
"""

from __future__ import annotations

import pytest

from packrat.tui import tokens
from packrat.tui.layout import (
    Cell,
    Fitted,
    fit,
    fit_width,
    middle_elide,
    pager_line,
    row,
    wrap_cells,
)

WIDTHS = [1, 2, 5, 10, 29, 40, 55, 80, 96, 98, 100, 200]


# --- row(): len == width, always ------------------------------------------
@pytest.mark.parametrize("w", WIDTHS)
def test_row_len_equals_width_fixed_cells(w):
    cells = [
        Cell(tokens.CURSOR, width=1),
        Cell("Downloads", width=9),
        Cell(r"D:\dump", width=20, elide="middle"),
        Cell(tokens.DOT_SCANNED, width=1),
        Cell("241", width=7, align="right"),
    ]
    assert len(row(w, cells)) == w


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("justify", ["pack", "between", "center"])
def test_row_len_equals_width_justify(w, justify):
    cells = [Cell("[a]"), Cell("[b] longer label"), Cell("[c]")]
    assert len(row(w, cells, justify=justify)) == w


@pytest.mark.parametrize("w", WIDTHS)
def test_row_len_equals_width_grow(w):
    cells = [Cell("4", width=4), Cell("label", grow=1), Cell("right", width=6, align="right")]
    assert len(row(w, cells)) == w


@pytest.mark.parametrize("w", WIDTHS)
def test_row_multi_grow_splits_by_weight(w):
    cells = [Cell("a", grow=1), Cell("b", grow=2), Cell("c", grow=1)]
    out = row(w, cells)
    assert len(out) == w


def test_row_empty_cells_is_blank_width():
    assert row(10, []) == " " * 10


def test_row_pack_is_left_justified():
    out = row(20, [Cell("hi", width=2)])
    assert out.startswith("hi") and out == "hi" + " " * 18


def test_row_between_spreads_to_edges():
    out = row(20, [Cell("a", width=1), Cell("b", width=1)], justify="between")
    assert out[0] == "a" and out[-1] == "b" and len(out) == 20


def test_row_center_justify_places_cell():
    # justify centers the whole cell group within the row's slack
    out = row(11, [Cell("x", width=1)], justify="center")
    assert out == " " * 5 + "x" + " " * 5


def test_cell_align_center_within_its_own_width():
    # a cell's align centers text WITHIN its width; row slack is the justify's job
    out = row(11, [Cell("x", width=5, align="center")], justify="pack")
    assert out[:5] == "  x  " and len(out) == 11


def test_row_right_align_cell():
    out = row(10, [Cell("9", width=10, align="right")])
    assert out == " " * 9 + "9"


# --- middle_elide: §12 path rule ------------------------------------------
def test_middle_elide_keeps_head_and_tail():
    p = (
        r"W:\[Nekomoe kissaten&VCB-Studio] Yahari Ore no Seishun Lovecome "
        r"wa Machigatte Iru. Kan [Ma10p_1080p]"
    )
    e = middle_elide(p, 50)
    assert len(e) == 50
    assert e.startswith(r"W:\[Nekomoe")          # head/drive preserved
    assert e.endswith("[Ma10p_1080p]")           # tail/leaf preserved
    assert tokens.ELLIPSIS in e


def test_middle_elide_noop_when_fits():
    assert middle_elide("short", 20) == "short"


def test_middle_elide_head_biased():
    # odd leftover cell → head is one longer than tail
    e = middle_elide("ABCDEFGHIJ", 6)  # keep=5 → head=3, tail=2
    assert e == "ABC" + tokens.ELLIPSIS + "IJ"
    assert len(e) == 6


def test_middle_elide_degenerate_width():
    assert middle_elide("abcdef", 1) == tokens.ELLIPSIS[:1]


@pytest.mark.parametrize("w", [1, 2, 3, 8, 20, 49, 50, 99])
def test_middle_elide_len_exact(w):
    p = r"\\tubie_nas\Res-v2\deep\nested\folders\file.heic"
    assert len(middle_elide(p, w)) == min(w, len(p))


def test_cell_elide_end_default_trailing_ellipsis():
    out = row(10, [Cell("abcdefghijklmno", width=10, elide="end")])
    assert out.endswith(tokens.ELLIPSIS) and len(out) == 10


# --- fit(): rows == budget, always ----------------------------------------
@pytest.mark.parametrize("budget", [0, 1, 3, 5, 10, 22])
@pytest.mark.parametrize("n", [0, 1, 4, 5, 6, 50])
@pytest.mark.parametrize("mode", ["scroll", "truncate", "clip"])
def test_fit_rows_always_budget(budget, n, mode):
    lines = [f"line {i}" for i in range(n)]
    f = fit(lines, budget, mode=mode)
    assert isinstance(f, Fitted)
    assert len(f.rows) == budget


def test_fit_scroll_pages():
    lines = [f"l{i}" for i in range(20)]
    f = fit(lines, 5, mode="scroll", page=0)
    assert f.rows[0] == "l0" and f.total_pages == 4 and f.scrollable
    f2 = fit(lines, 5, mode="scroll", page=1)
    assert f2.rows[0] == "l5"


def test_fit_truncate_marker():
    lines = [f"l{i}" for i in range(20)]
    f = fit(lines, 5, mode="truncate")
    assert f.rows[-1] == f"{tokens.ELLIPSIS} 16 more"     # budget-1 shown + marker
    assert f.overflow == 16


def test_fit_no_overflow_when_fits():
    f = fit(["a", "b"], 5, mode="scroll")
    assert f.overflow == 0 and not f.scrollable and f.rows[2:] == ["", "", ""]


# --- pager_line ------------------------------------------------------------
@pytest.mark.parametrize("w", WIDTHS)
def test_pager_line_len(w):
    assert len(pager_line(w, 2, 5)) == w


def test_pager_line_centered_text():
    out = pager_line(20, 1, 1)
    assert out.strip() == "page 1/1"
    assert out.startswith(" ") and out.endswith(" ")


# --- wrap_cells ------------------------------------------------------------
def test_wrap_cells_within_width():
    out = wrap_cells("the quick brown fox jumps", 10)
    assert all(len(line) <= 10 for line in out)
    assert " ".join(out).split() == "the quick brown fox jumps".split()


def test_wrap_cells_hard_breaks_long_token():
    out = wrap_cells(r"\\server\aVeryLongUnbreakableShareName\x", 12)
    assert all(len(line) <= 12 for line in out)


def test_wrap_cells_preserves_newlines():
    out = wrap_cells("a\nb", 10)
    assert out == ["a", "b"]


# --- fit_width (== generator pad) -----------------------------------------
def test_fit_width_pads_right():
    assert fit_width("abc", 5) == "abc  "


def test_fit_width_hard_truncates():
    assert fit_width("abcdef", 3) == "abc"


# --- status_dot: the four branches ----------------------------------------
def test_status_dot_trash_blank():
    assert tokens.status_dot("trash", "2024", "2024") == tokens.DOT_TRASH


def test_status_dot_deduped():
    assert tokens.status_dot("library", "2024", "2024") == tokens.DOT_DEDUPED


def test_status_dot_scanned_only():
    assert tokens.status_dot("library", "2024", None) == tokens.DOT_SCANNED


def test_status_dot_never():
    assert tokens.status_dot("library", None, None) == tokens.DOT_NEVER


# --- token derivations -----------------------------------------------------
def test_roots_w_derivation():
    # dashboard: Collection + gap + Roots must fill the inner content width
    assert tokens.COLLECTION_W + tokens.GAP + tokens.ROOTS_W == tokens.CW - 2


def test_default_theme_covers_all_roles():
    for role in tokens.ROLES:
        assert role in tokens.DEFAULT_THEME.colors, role


def test_theme_color_fallback():
    assert tokens.DEFAULT_THEME.color("nonexistent") == tokens.DEFAULT_THEME.colors["default"]
