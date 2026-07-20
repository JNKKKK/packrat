"""Colorizer tests — assert role→span mapping, never a concrete hex (§Theming).

``colorize`` applies theme role colors to a finished plain frame *post-layout*, so
the golden frames stay colorless (tested elsewhere) and this is the separate,
cheaper color check. We assert which theme *color* a span gets by comparing to the
theme's own ``color(role)`` lookup — so a theme retune (changing the hex) never
breaks a test, because the assertion is "this glyph gets the success role's color",
not "this glyph is #5fd75f".
"""

from __future__ import annotations

from packrat.tui import tokens
from packrat.tui.colorize import colorize
from packrat.tui.tokens import DEFAULT_THEME as T


def _span_color(frame: str, needle: str) -> str | None:
    """The color applied to the first occurrence of `needle` in the colorized frame."""
    text = colorize(frame)
    idx = frame.index(needle)
    # Later spans override earlier ones (Text applies in order); take the last
    # span that covers the needle's first cell.
    color = text.style  # base default
    for span in text.spans:
        if span.start <= idx < span.end:
            color = span.style
    return str(color) if color else None


def test_deduped_dot_gets_success_color():
    assert _span_color(f" {tokens.DOT_DEDUPED} x", tokens.DOT_DEDUPED) == T.color("success")


def test_scanned_dot_gets_warn_color():
    assert _span_color(f" {tokens.DOT_SCANNED} x", tokens.DOT_SCANNED) == T.color("warn")


def test_never_dot_gets_dim_color():
    assert _span_color(f" {tokens.DOT_NEVER} x", tokens.DOT_NEVER) == T.color("dim")


def test_warn_glyph_gets_warn_color():
    assert _span_color(f"{tokens.WARN} review", tokens.WARN) == T.color("warn")


def test_running_marker_gets_running_color():
    assert _span_color(f"{tokens.RUNNING} scan", tokens.RUNNING) == T.color("running")


def test_bar_fill_gets_running_color():
    assert _span_color(f"{tokens.BAR_FILL * 3} 50%", tokens.BAR_FILL) == T.color("running")


def test_key_hint_gets_accent_color():
    assert _span_color("[r] focus Roots", "[r]") == T.color("accent")


def test_cursor_gets_accent_color():
    assert _span_color(f"{tokens.CURSOR} Downloads", tokens.CURSOR) == T.color("accent")


def test_guillemet_hint_gets_dim_color():
    assert _span_color("Name ‹defaults to leaf›", "‹") == T.color("dim")


def test_error_glyph_gets_error_color():
    assert _span_color(f"{tokens.CROSS} failed", tokens.CROSS) == T.color("error")


def test_plain_text_gets_default_color():
    # a run of ordinary body text carries the theme default foreground
    assert _span_color("Assets    124,803", "Assets") == T.color("default")


def test_heavy_border_gets_focus_border_color():
    # a focused Panel's heavy frame glyphs carry the focus-border (accent) color
    assert _span_color("┏━ [Q]UEUE ━┓", "┏") == T.color("focus-border")


def test_light_border_stays_default():
    # the outer AppFrame + unfocused panels use light glyphs → NOT accented
    assert _span_color("┌─ Collection ─┐", "┌") == T.color("default")


def test_focused_section_header_is_fully_accented():
    # a focused queue section header is fully UPPERCASED + ':' → whole line accent
    frame = "[Q]UEUED (RUNS TOP-DOWN):"
    assert _span_color(frame, "UEUED") == T.color("accent")
    assert _span_color(frame, "RUNS") == T.color("accent")     # not just the [Q]
    assert _span_color(frame, "DOWN") == T.color("accent")


def test_focused_section_header_accented_even_with_pager():
    # a focused header carrying a right-aligned pager still accents as a whole line
    frame = "[Q]UEUED:                                                   page 1/2"
    assert _span_color(frame, "UEUED") == T.color("accent")
    assert _span_color(frame, "[Q]") == T.color("accent")


def test_active_panel_inactive_section_header_only_key_accented():
    # a mixed-case (active panel, inactive section) header keeps default body text;
    # only its [h] hint pops
    frame = "[H]istory:"
    assert _span_color(frame, "istory") == T.color("default")
    assert _span_color(frame, "[H]") == T.color("accent")


def test_inactive_panel_header_is_fully_dim():
    # a lowercase-key header (the panel itself is unfocused) → whole line dim, so its
    # [k] key hint reads grey too (not accent)
    frame = "[h]istory:                                                  page 1/3"
    assert _span_color(frame, "istory") == T.color("dim")
    assert _span_color(frame, "[h]") == T.color("dim")     # key hint dimmed, not accented
    assert _span_color(frame, "page 1/3") == T.color("dim")


def test_header_coloring_survives_leading_frame_and_box_borders():
    # colorize runs on the COMPOSED frame, where a sub-section header sits inside the
    # outer frame AND its panel box: `│ │ [h]istory: … │ │`. The classifier must see
    # past those border glyphs (regression: it anchored `^[` and colored nothing).
    frame = "│ │ [h]istory:                                             page 1/3 │ │"
    assert _span_color(frame, "istory") == T.color("dim")     # dim reaches the label
    assert _span_color(frame, "[h]") == T.color("dim")
    # the leading border glyph is NOT tinted by the header rule (it stays default)
    assert _span_color(frame, "│ │ [h]"[:1]) == T.color("default")


def test_focused_header_accent_survives_box_borders():
    frame = "│ ┃ [H]ISTORY:                                             page 1/3 ┃ │"
    assert _span_color(frame, "ISTORY") == T.color("accent")
    assert _span_color(frame, "[H]") == T.color("accent")


def test_trash_label_not_treated_as_key_hint():
    # `(trash)` uses parens, not brackets — must NOT get the accent key-hint color
    assert _span_color("_Trash  (trash)", "(trash)") == T.color("default")


def test_long_bracket_label_not_a_key_hint():
    # `[undecodable]` is 12 chars inside brackets — beyond the 1–6 key-hint range,
    # so it stays default, not accent (the hint pattern is scoped to short keys).
    assert _span_color("[undecodable] file.heic", "[undecodable]") == T.color("default")


def test_colorize_preserves_text_content():
    # coloring must not change a single character (width/content invariant)
    frame = "┌─ [R]oots ─┐ ◉ 98,412  ‹hint›"
    assert colorize(frame).plain == frame


# --- logo gem gradient animation -------------------------------------------
def test_gem_gradient_wraps_and_interpolates():
    from packrat.tui.colorize import gem_gradient_color
    from packrat.tui.tokens import GEM_GRADIENT

    # phase 0 and phase 1.0 land on the same (first) stop — the loop closes.
    assert gem_gradient_color(0.0) == GEM_GRADIENT[0]
    assert gem_gradient_color(1.0) == gem_gradient_color(0.0)
    # a mid-stop phase is a blend, i.e. NOT equal to either bracketing stop.
    mid = gem_gradient_color(0.5 / len(GEM_GRADIENT))
    assert mid not in GEM_GRADIENT
    assert mid.startswith("#") and len(mid) == 7


def test_recolor_gem_tints_only_the_gems():
    from packrat.tui.colorize import recolor_gem
    from packrat.tui import render

    logo = "\n".join(render.logo_lines(1234, gem="◆"))
    text = recolor_gem(colorize(logo), logo, "◆", "#ff00ff")
    # exactly the two ◆ cells in `(>◆◆<)` are tinted with the gradient color
    tinted = [(s.start, s.end) for s in text.spans if str(s.style) == "#ff00ff"]
    positions = [i for i, ch in enumerate(logo) if ch == "◆"]
    assert len(positions) == 2
    assert tinted == [(positions[0], positions[0] + 1), (positions[1], positions[1] + 1)]
    # content is untouched (recolor is style-only)
    assert text.plain == logo


def test_recolor_hoard_count_tints_only_the_number():
    from packrat.tui.colorize import recolor_hoard_count
    from packrat.tui import render

    logo = "\n".join(render.logo_lines(1234567, gem="◆"))
    text = recolor_hoard_count(colorize(logo), logo, "#ff00ff")
    tinted = [(s.start, s.end) for s in text.spans if str(s.style) == "#ff00ff"]
    # exactly one span — the "1,234,567" count in "· N assets hoarded ·"
    assert len(tinted) == 1
    a, b = tinted[0]
    assert logo[a:b] == "1,234,567"           # digits + commas, nothing else
    assert text.plain == logo                 # style-only, content unchanged
