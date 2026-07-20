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
    # dim: the ○ never dot, ░ bar remainder (the guillemet-hint rule is applied LAST,
    # below, so a ‹…› aside stays dim even when it contains a `[k]` hint).
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
    # dim ‹guillemet asides› — LAST so a whole ‹…› span reads dim even when it wraps a
    # `[k]` hint (an inactive section's dimmed action hints), overriding the accent above.
    ("dim", r"‹[^›]*›"),
]


# A section header line: ``[K]abel:`` (bracket key + word + colon) after any leading
# frame/box border glyphs, optionally trailed by a right-aligned ``page i/N`` pager.
# Border glyphs (``│``/``┃`` + spaces) are allowed before the ``[`` because colorize
# runs on the FULLY COMPOSED frame, where a header sits inside the outer frame AND its
# panel box (``│ │ [q]ueued: … │ │``). The casing of the label encodes the focus state.
_HEADER_RE = re.compile(r"^[\s│┃]*(\[[A-Za-z]\][A-Za-z ()-]*:)(\s+page \d+/\d+)?")


def _header_span(line: str) -> tuple[str, int, int] | None:
    """Classify a section-header line, returning ``(role, start, end)`` or ``None``.

    ``start``/``end`` bound the header CONTENT (the ``[K]abel:`` + optional pager),
    excluding the surrounding frame/box borders — so coloring the header never tints
    the border glyphs (which the focus-border rule owns). Three casing-encoded states:
    - **fully UPPERCASED** label (``[Q]UEUED:``) → *focused section* → ``accent``;
    - **lowercase key + word** (``[q]ueued:``)    → *inactive panel*  → ``dim``;
    - mixed (``[Q]ueued:`` — uppercase key, lowercase word) → active panel, inactive
      section → ``None`` (default text, just its ``[K]`` bracket accented by regex)."""
    m = _HEADER_RE.match(line)
    if not m:
        return None
    label = m.group(1)
    letters = [c for c in label if c.isalpha()]
    if not letters:
        return None
    if all(c.isupper() for c in letters):
        role = "accent"        # focused section (uppercased header)
    elif all(c.islower() for c in letters):
        role = "dim"           # inactive panel (lowercase-key header)
    else:
        return None            # mixed case → default (bracket-only accent)
    return role, m.start(1), m.end()


def _header_role(line: str) -> str | None:
    """The whole-line role for a section header (``None`` for the default case)."""
    span = _header_span(line)
    return span[0] if span else None


def _lerp_hex(a: str, b: str, t: float) -> str:
    """Linear-interpolate two ``#rrggbb`` colors at fraction ``t`` in [0,1]."""
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    r = round(ar + (br - ar) * t)
    g = round(ag + (bg - ag) * t)
    bl = round(ab + (bb - ab) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


def gem_gradient_color(phase: float, stops: tuple = tokens.GEM_GRADIENT) -> str:
    """The gem's shimmer color at animation ``phase`` (any float; wraps mod 1.0).

    ``phase`` walks a loop around ``stops`` (the last stop blends back to the first),
    so the returned ``#rrggbb`` sweeps smoothly and repeats — the faceted-glint effect
    the dashboard timer advances a little each tick."""
    n = len(stops)
    pos = (phase % 1.0) * n           # position along the ring [0, n)
    i = int(pos)
    frac = pos - i
    return _lerp_hex(stops[i % n], stops[(i + 1) % n], frac)


def recolor_gem(text: Text, frame: str, gem: str, color: str) -> Text:
    """Tint every ``gem`` glyph in an already-colorized ``Text`` to ``color`` (in place).

    Applied AFTER :func:`colorize` so the gradient sweep wins over the base default;
    the gem glyphs (◆/◇/◈) appear only in the logo, so this touches nothing else. Both
    gems in ``(>◆◆<)`` are recolored. Returns ``text`` for chaining."""
    start = 0
    while True:
        idx = frame.find(gem, start)
        if idx == -1:
            break
        text.stylize(color, idx, idx + 1)
        start = idx + 1
    return text


# The live hoard count in the logo's "· N assets hoarded ·" line — tinted the same as
# the mascot's gem so the number glints with it. Matched by its surrounding words (the
# count itself is dynamic), digits + thousands commas only.
_HOARD_COUNT_RE = re.compile(r"·\s([\d,]+)\sassets hoarded")


def recolor_hoard_count(text: Text, frame: str, color: str) -> Text:
    """Tint the ``N`` in ``· N assets hoarded ·`` to ``color`` (in place, post-colorize).

    Matches only the count's digit span, so the surrounding text keeps its default color.
    Returns ``text`` for chaining."""
    m = _HOARD_COUNT_RE.search(frame)
    if m:
        text.stylize(color, m.start(1), m.end(1))
    return text


def colorize(frame: str, theme: Theme = tokens.DEFAULT_THEME) -> Text:
    """Return a Rich ``Text`` of ``frame`` with theme role colors applied by pattern.

    Base text is the theme's ``default`` foreground; each :data:`ROLE_PATTERNS`
    span is recolored to its role's theme color. Section-header lines are then
    colored **as a whole line** per :func:`_header_role` — accent for a focused
    section, dim for an inactive panel — applied *after* the regex pass so they
    override the per-``[K]`` bracket accent (a dim header greys its own key hint).
    Background stays untouched (the terminal's own — transparent ``ansi_default``).
    """
    text = Text(frame, style=theme.color("default"), no_wrap=True, end="")
    for role, pattern in ROLE_PATTERNS:
        text.highlight_regex(pattern, theme.color(role))
    # Header coloring, applied AFTER the regex pass so it wins over the `[K]` bracket
    # accent. Scanned per line by offset (Rich's highlight_regex has no MULTILINE `$`);
    # only the header CONTENT span is tinted (not the frame/box borders around it), and
    # the trailing pager is included so it tracks its header.
    offset = 0
    for line in frame.split("\n"):
        span = _header_span(line)
        if span:
            role, start, end = span
            text.stylize(theme.color(role), offset + start, offset + end)
        offset += len(line) + 1  # +1 for the '\n'
    return text
