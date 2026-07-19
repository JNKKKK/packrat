"""Post-layout colorizer — apply theme role colors to a finished plain frame.

The layout/render layers produce **plain, colorless** text (so width math and the
golden-frame snapshot tests never see color markup — §Theming "the hard rule").
This module is the *separate* color layer: it takes the composed 100×24 frame
string and returns a Rich :class:`~rich.text.Text` with each span colored by its
semantic **role** → the active :class:`~packrat.tui.tokens.Theme`'s color.

Rather than thread per-cell roles up through composition, we re-derive the role
from the distinctive glyphs/patterns the render layer emits (the ◉/◐/○ dots, the
▶ running marker, the █ bar fill, ⚠, ✓/✗, `[k]` key hints, ‹dim hints›). The
mapping is stable; only the *colors* change with a theme, so a retune touches one
table (:data:`ROLE_PATTERNS` keys are roles, values are regexes). Applied only in
the live widget — the plain frame stays the source of truth for tests.
"""

from __future__ import annotations

import re

from rich.text import Text

from . import tokens
from .tokens import Theme

# role → regex of spans that carry that role in a composed frame. Order matters:
# earlier roles win a cell (Text.stylize is applied in list order; later spans can
# override, so put broad/lowest-priority first, specific/highest-priority last).
ROLE_PATTERNS: list[tuple[str, str]] = [
    # dim: ‹guillemet hints›, the ○ never dot, ░ bar remainder
    ("dim", r"‹[^›]*›"),
    ("dim", re.escape(tokens.DOT_NEVER)),
    ("dim", re.escape(tokens.BAR_EMPTY) + "+"),
    # success: ◉ deduped dot, ✓ applied
    ("success", re.escape(tokens.DOT_DEDUPED)),
    ("success", re.escape(tokens.CHECK)),
    # warn: ◐ scanned-only dot, ⚠ attention
    ("warn", re.escape(tokens.DOT_SCANNED)),
    ("warn", re.escape(tokens.WARN)),
    # running: ▶ marker, █ bar fill
    ("running", re.escape(tokens.RUNNING)),
    ("running", re.escape(tokens.BAR_FILL) + "+"),
    # error: ✗
    ("error", re.escape(tokens.CROSS)),
    # accent: the ▸ selection cursor, `[k]`-style key hints (1–6 chars in brackets:
    # covers [r] [q] [x] [ ] [Enter] [Tab] [Esc], but NOT [undecodable]/(trash))
    ("accent", re.escape(tokens.CURSOR)),
    ("accent", r"\[[^\]]{1,6}\]"),
    # accent: a FOCUSED panel's heavy border (┏━┓┃┗┛). Only a focused Panel uses
    # the heavy box glyphs — the outer AppFrame + unfocused panels use light ones
    # — so tinting every heavy glyph colors exactly the focused box's frame.
    ("focus-border", "[" + re.escape("".join(tokens.HEAVY_BOX)) + "]+"),
    # daemon health dot in the header: ● up (success) / ○ down (error)
    ("success", r"●(?= up)"),
    ("error", r"○(?= down)"),
]


def _is_focused_header(line: str) -> bool:
    """A focused section header is fully UPPERCASED and ends with ':'.

    The queue screen uppercases the focused section's header (``[Q]UEUED (RUNS
    TOP-DOWN):``); a line with no lowercase letters, at least one A–Z, ending in
    ':' is such a header. Normal (unfocused) headers keep mixed case and don't
    match, so only the focused one is accented as a whole line."""
    s = line.strip()
    if not s.endswith(":") or not any(c.isupper() for c in s):
        return False
    return not any(c.islower() for c in s)


def colorize(frame: str, theme: Theme = tokens.DEFAULT_THEME) -> Text:
    """Return a Rich ``Text`` of ``frame`` with theme role colors applied by pattern.

    Base text is the theme's ``default`` foreground; each :data:`ROLE_PATTERNS`
    span is recolored to its role's theme color. A **focused section header** line
    (fully uppercased, ending ':') is accented as a whole line first, so the entire
    header — not just its ``[K]`` — reads as focused. Background stays untouched
    (the terminal's own — the app runs with a transparent ``ansi_default`` bg).
    """
    text = Text(frame, style=theme.color("default"), no_wrap=True, end="")
    # Whole-line accent for focused section headers (computed per line — Rich's
    # highlight_regex doesn't do MULTILINE `$`, so we scan lines by offset).
    accent = theme.color("accent")
    offset = 0
    for line in frame.split("\n"):
        if _is_focused_header(line):
            text.stylize(accent, offset, offset + len(line))
        offset += len(line) + 1  # +1 for the '\n'
    for role, pattern in ROLE_PATTERNS:
        text.highlight_regex(pattern, theme.color(role))
    return text
