"""Color-role tests — assert the ROLE a cell carries, never a concrete hex (§Theming).

Geometry is colorless (the golden frames are plain text — color is applied
post-layout); color correctness is this separate, cheaper test: a running span →
``running``, a ``◉`` deduped dot → ``success``, a trash count → ``dim``. Asserting
the *role* (not the hex) means a theme retune never breaks a test — roles are
stable, colors are free to change (component-plan §Theming).
"""

from __future__ import annotations

from packrat.tui import fixtures, tokens
from packrat.tui.layout import Cell, row
from packrat.tui.tokens import DEFAULT_THEME


def _cells_of(fn, *args, **kwargs) -> list[Cell]:
    """Capture the Cells a renderer builds by monkeypatching `row` to record them."""
    captured: list[Cell] = []
    import packrat.tui.render as render_mod

    orig = render_mod.row

    def spy(width, cells, **kw):
        captured.extend(cells)
        return orig(width, cells, **kw)

    render_mod.row = spy
    try:
        fn(*args, **kwargs)
    finally:
        render_mod.row = orig
    return captured


def _role_for(cells: list[Cell], text_contains: str) -> str | None:
    for c in cells:
        if text_contains in c.text:
            return c.style
    raise AssertionError(f"no cell containing {text_contains!r}")


def _dot_role(cells: list[Cell]) -> str | None:
    """The role of the freshness-dot cell (width-1 cell holding a dot glyph).

    ◉ is both green + yellow, so the row's dot cell carries the true role directly
    (from render.root_dot_pair) — assert THAT, not the ambiguous glyph."""
    dots = {tokens.DOT_DEDUPED, tokens.DOT_NEEDS_DEDUP, tokens.DOT_PROBED, tokens.DOT_NEVER}
    for c in cells:
        if c.width == 1 and c.text in dots:
            return c.style
    raise AssertionError("no freshness-dot cell found")


def test_deduped_dot_has_success_role():
    from packrat.tui import render
    iphone = next(r for r in fixtures.ROOTS if r["name"] == "iPhone")   # ◉ green (dedup>scan)
    cells = _cells_of(render.root_row_compact, iphone)
    # ◉ appears for both deduped(green) and need-dedup(yellow); assert the ROLE the cell
    # carries (not just the glyph) — iPhone's dedup is newer than its scan → success.
    assert _dot_role(cells) == "success"


def test_need_dedup_dot_has_warn_role():
    from packrat.tui import render
    camera = next(r for r in fixtures.ROOTS if r["name"] == "Camera")   # ◉ yellow (scan>dedup)
    cells = _cells_of(render.root_row_compact, camera)
    assert _dot_role(cells) == "warn"


def test_probed_new_dot_has_dim_role():
    from packrat.tui import render
    photos = next(r for r in fixtures.ROOTS if r["name"] == "Photos")   # ◐ probe found new
    cells = _cells_of(render.root_row_compact, photos)
    assert _dot_role(cells) == "dim"


def test_trash_count_has_dim_role():
    from packrat.tui import render
    trash = next(r for r in fixtures.ROOTS if r["kind"] == "trash")
    cells = _cells_of(render.root_row_compact, trash)
    assert _role_for(cells, "(trash)") == "dim"


def test_selected_cursor_has_highlighted_role():
    from packrat.tui import render
    r = fixtures.ROOTS[0]
    cells = _cells_of(render.root_row_compact, r, selected=True)
    assert _role_for(cells, tokens.CURSOR) == "highlighted"


def test_queued_note_is_dim():
    from packrat.tui import render
    cells = _cells_of(render.queue_row, dict(fixtures.QUEUED_SCAN), index=2)
    # the status/blocked note cell carries the dim role
    assert any(c.style == "dim" for c in cells)


def test_theme_maps_every_role_to_a_color():
    for role in tokens.ROLES:
        assert isinstance(DEFAULT_THEME.color(role), str)
        assert DEFAULT_THEME.color(role).startswith("#")


def test_theme_roles_are_the_closed_set():
    # tokens.ROLES is the closed vocabulary; the theme covers exactly it (+ no less).
    assert set(DEFAULT_THEME.colors) == set(tokens.ROLES)


def test_tcss_modal_size_matches_composed_box():
    """The .tcss #modal-frame width/height must equal the composed box (no drift).

    The modal is an EXPLICIT fixed size in the stylesheet (auto collapsed to 0×0,
    the "invisible modal / only acrylic" bug). If MODAL_W/MODAL_H change, the CSS
    must change too — this guard fails loudly if they drift apart."""
    import re
    from pathlib import Path

    from packrat.tui.modals import MODAL_H, MODAL_W

    tcss = (Path(__file__).resolve().parents[1]
            / "src" / "packrat" / "tui" / "packrat.tcss").read_text(encoding="utf-8")

    def _block(selector: str) -> str:
        m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", tcss)
        assert m, f"no {selector} block in packrat.tcss"
        return m.group(1)

    def _dim(block: str, prop: str) -> int:
        m = re.search(rf"{prop}\s*:\s*(\d+)", block)
        assert m, f"no {prop} in block"
        return int(m.group(1))

    frame = _block("#modal-frame")
    assert _dim(frame, "width") == MODAL_W
    assert _dim(frame, "height") == MODAL_H
    # the outer #modal is the frame height + 1 row for the optional count Input
    assert _dim(_block("#modal"), "width") == MODAL_W
