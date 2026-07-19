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


def test_unfocused_section_header_only_key_accented():
    # a mixed-case (unfocused) header keeps default body text; only its [e] hint pops
    frame = "Rec[e]nt:"
    assert _span_color(frame, "Rec") == T.color("default")
    assert _span_color(frame, "[e]") == T.color("accent")


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
