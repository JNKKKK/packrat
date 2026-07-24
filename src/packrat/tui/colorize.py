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
    # dim: the ○ never dot + ◐ probed-new dot (both grey — §12 4-state), ░ bar remainder
    # (the guillemet-hint rule is applied LAST, below, so a ‹…› aside stays dim even when
    # it contains a `[k]` hint).
    ("dim", re.escape(tokens.DOT_NEVER)),
    ("dim", re.escape(tokens.DOT_PROBED)),
    ("dim", re.escape(tokens.BAR_EMPTY) + "+"),
    # success: ◉ dot BASE color (deduped-green). ◉ is also yellow (need-dedup) — a
    # distinction the glyph pass can't make — so `recolor_root_dots` recolors each root
    # ROW's dot to its true role post-pass; this is the standalone/legend default. ✓ applied.
    ("success", re.escape(tokens.DOT_DEDUPED)),
    ("success", re.escape(tokens.CHECK)),
    # warn: ⚠ attention
    ("warn", re.escape(tokens.WARN)),
    # running: ▶ marker, █ bar fill
    ("running", re.escape(tokens.RUNNING)),
    ("running", re.escape(tokens.BAR_FILL) + "+"),
    # error: ✗
    ("error", re.escape(tokens.CROSS)),
    # accent: the ▸ selection cursor, `[k]`-style key hints (1–6 chars in brackets:
    # covers [r] [q] [x] [ ] [Enter] [Tab] [Esc], but NOT [undecodable]/(trash)).
    # `[^\]\n]` — no newline inside, so an unclosed `[` at a line end can't run the
    # accent span across the frame's `│\n│` borders onto the next line.
    ("accent", re.escape(tokens.CURSOR)),
    ("accent", r"\[[^\]\n]{1,6}\]"),
    # accent: a FOCUSED panel's heavy border (┏━┓┃┗┛). Only a focused Panel uses
    # the heavy box glyphs — the outer AppFrame + unfocused panels use light ones
    # — so tinting every heavy glyph colors exactly the focused box's frame.
    ("focus-border", "[" + re.escape("".join(tokens.HEAVY_BOX)) + "]+"),
    # daemon health dot in the header: ● up (success) / ○ down (error)
    ("success", r"●(?= up)"),
    ("error", r"○(?= down)"),
    # dim ‹guillemet asides› — LAST so a whole ‹…› span reads dim even when it wraps a
    # `[k]` hint (an inactive section's dimmed action hints), overriding the accent above.
    # `[^›\n]` so an unclosed ‹ can't run the dim span across line borders.
    ("dim", r"‹[^›\n]*›"),
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


def recolor_root_dots(text: Text, frame: str, roots: list[dict],
                      theme: Theme = tokens.DEFAULT_THEME) -> Text:
    """Recolor each root ROW's freshness dot to its true role (in place, post-colorize).

    The ◉ dot is BOTH green (deduped) and yellow (need-dedup) — a distinction the
    glyph-based :func:`colorize` can't draw (one glyph → one color). So after the base
    pass we walk the displayed ``roots`` (query rows) and, for each, find its row line by
    the root NAME and recolor the ``◉``/``◐``/``○`` dot that follows the name to the role
    :func:`packrat.tui.render.root_dot_pair` computes. A trash root has no dot (skipped);
    a root whose row isn't on screen (paged out) simply isn't found (no-op). Robust to
    the row layout: it anchors on the name span, then colors the first dot glyph at/after
    it — never a dot elsewhere on the line (there is only one per row).

    ``roots`` is the SAME list the frame was built from (post-sort/-mask), so names match
    what's rendered. Called by the dashboard/roots screens' ``_colorize`` hooks (which own
    the displayed roots); no-op if ``roots`` is empty."""
    from . import render

    lines = frame.split("\n")
    # Precompute each line's absolute start offset in `frame` (for stylize positions).
    offsets, off = [], 0
    for ln in lines:
        offsets.append(off)
        off += len(ln) + 1  # +1 for '\n'

    for r in roots:
        if r.get("kind") == "trash":
            continue
        glyph, role = render.root_dot_pair(r)
        if not glyph.strip():          # blank (shouldn't happen for library) → nothing to color
            continue
        if not r.get("name"):
            continue
        # Anchor on the name AS THE ROW RENDERS IT — a name wider than NAME_W is
        # end-elided to `head…` in the row, so matching the raw name would miss it and
        # leave a long-named root's dot mis-colored (the glyph pass's default). Both come
        # from render.root_name_display, so the display + match forms can't drift.
        name = render.root_name_display(r)
        for i, ln in enumerate(lines):
            npos = ln.find(name)
            # Anchor on the row's NAME CELL, matching the WHOLE name, not a prefix:
            #  (a) only frame/box borders, the ▸ cursor, and spaces may precede it (the
            #      name is the first TEXT cell of a row) — rejects a name occurring inside
            #      another root's PATH (e.g. "Photos" inside `E:\Photos2`); AND
            #  (b) the char right after it must be a space (the name cell is fixed-width,
            #      space-padded) — rejects a shorter name matching a longer one as a prefix
            #      ("Photo" must NOT anchor on the "Photos" row).
            if npos == -1 or not all(c in _ROOT_NAME_PREFIX for c in ln[:npos]):
                continue
            after = npos + len(name)
            if after < len(ln) and ln[after] != " ":
                continue               # matched a longer name as a prefix — not this row
            dpos = ln.find(glyph, after)
            if dpos == -1:
                continue
            base = offsets[i] + dpos
            text.stylize(theme.color(role), base, base + len(glyph))
            break                      # one row per root
    return text


#: Chars allowed BEFORE a root name on its row line (frame/box borders, the ▸ cursor,
#: spaces) — the anchor that identifies the NAME cell, not a name inside a path.
_ROOT_NAME_PREFIX = frozenset("│┃ " + tokens.CURSOR)

# The dot legend's ◉ appears twice — "◉ deduped" (green) and "◉ need dedup" (yellow) —
# the same green/yellow split the row dots use. The glyph pass colors BOTH ◉ green, so
# recolor the "need dedup" one to warn. Matched by the label that follows each ◉.
_LEGEND_DEDUP_RE = re.compile(re.escape(tokens.DOT_DEDUPED) + r"(?=\s+need dedup)")


def recolor_dot_legend(text: Text, frame: str, theme: Theme = tokens.DEFAULT_THEME) -> Text:
    """Recolor the dot legend's second ``◉`` (``◉ need dedup``) to warn (in place).

    The legend shows both ◉ states; the base glyph pass paints every ◉ green, so the
    "need dedup" one must be corrected to yellow to match the row dots (§12). The
    "deduped" ◉ and the grey ◐/○ are already correct from the glyph pass. No-op if the
    legend isn't on the frame (a screen without it)."""
    for m in _LEGEND_DEDUP_RE.finditer(frame):
        text.stylize(theme.color("warn"), m.start(), m.end())
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


#: Border glyphs that bound a row's content on the frame (outer ``│``) or a focused
#: panel (heavy ``┃``). A list-row cursor sits just inside these; the add-root form's
#: mid-line ``▸`` field marker has a text label before it, so it's excluded.
_BORDER_GLYPHS = frozenset("│┃")


def _row_cursor_index(line: str, marker: str) -> int | None:
    """Index of ``marker`` iff it is the row's LEADING content glyph (a list-row
    cursor), else ``None``.

    A selected list row is ``│ [┃ ]▸ …`` — only frame/box borders + spaces precede the
    ``▸``. The add-root form instead uses ``▸`` as a *field* marker after a label
    (``  Path   ▸ …``), which this rejects (a letter precedes the marker), so emphasis
    never lands on a form field."""
    idx = line.find(marker)
    if idx == -1:
        return None
    if all(ch == " " or ch in _BORDER_GLYPHS for ch in line[:idx]):
        return idx
    return None


def _row_content_end(line: str, start: int) -> int:
    """End (exclusive) of the row's content — the first border glyph at/after ``start``
    (the enclosing panel's ``┃`` or the frame's ``│``), else the rstrip'd length.

    Content never contains a box-drawing glyph, so the first one scanning right is the
    row's right border — emphasis stops just before it, never tinting a border."""
    for i in range(start, len(line)):
        if line[i] in _BORDER_GLYPHS:
            return i
    return len(line.rstrip())


def emphasize_selected_row(text: Text, frame: str, marker: str = tokens.CURSOR,
                           theme: Theme = tokens.DEFAULT_THEME) -> Text:
    """Emphasize the list row carrying the ``▸`` selection ``marker`` (in place,
    post-colorize) — **bold + the brighter ``selected`` foreground** (``#ffffff`` vs.
    the ``#d0d0d0`` body default), the highlighted-cursor-row look the ``selected``
    role was defined for (§Theming).

    Applies from **just after** the ``▸`` (the cursor keeps its accent color) to the
    row's right border, so the row *text* pops while the frame/box borders stay
    untouched. Runs AFTER :func:`colorize`, so the ``bold #ffffff`` overrides the row's
    default text — then the semantic glyph/role colors within the row are
    **re-asserted on top** (bolded), so the dedup dots (◉/◐/○), a running ``▶``, and a
    progress bar's ``█``/``░`` keep their meaning instead of washing out to white. Only
    rows whose ``▸`` is the leading content glyph are touched (:func:`_row_cursor_index`
    excludes the add-root form's mid-line field marker); no-op on any other line."""
    white = f"bold {theme.color('selected')}"
    offset = 0
    for line in frame.split("\n"):
        idx = _row_cursor_index(line, marker)
        if idx is not None:
            start = idx + len(marker)
            end = _row_content_end(line, start)
            base = offset + start
            text.stylize(white, base, offset + end)
            # Re-assert semantic colors within the row (bolded) so meaningful glyphs
            # aren't flattened to white — same patterns/order as colorize's own pass.
            seg = line[start:end]
            for role, pattern in ROLE_PATTERNS:
                for m in re.finditer(pattern, seg):
                    text.stylize(f"bold {theme.color(role)}",
                                 base + m.start(), base + m.end())
        offset += len(line) + 1             # +1 for the '\n'
    return text


def shade_box_title(text: Text, frame: str, title: str, right: str = "",
                    theme: Theme = tokens.DEFAULT_THEME) -> Text:
    """Shade the `` <title> `` (and optional `` <right> ``) tabs of a focused box's top
    border (in place, post-colorize).

    Paints the ``accent`` color as a BACKGROUND block behind the box title *and its
    flanking spaces* (``┏━ [R]oots ━┓`` → the `` [R]oots `` reads like a highlighted
    accent tab) and forces the whole tab — including the ``[R]`` key — to the dark
    ``accent-fg`` foreground (high contrast on the bright accent bg), overriding the
    per-``[k]`` accent from the regex pass (so ``[R]`` isn't invisible accent-on-accent).
    ``right`` (e.g. the ``page 1/2`` paginator) is shaded the same way at its spot near
    the right corner. Only the FIRST heavy-border line carrying the title is touched (the
    box's top border), so the identical string elsewhere isn't affected. Returns ``text``
    for chaining."""
    style = f"{theme.color('accent-fg')} on {theme.color('accent')}"
    # The heavy top border is `┏━ <title> ━…━ <right> ━┓`; shade each label plus the
    # single space on either side (the `━ [R]oots ━` / `━ page 1/2 ━` runs).
    for line in frame.split("\n"):
        if "┏" in line and f" {title} " in line:
            base = frame.index(line)
            col = line.index(f" {title} ")
            text.stylize(style, base + col, base + col + len(title) + 2)
            # Right label: caller-supplied, else auto-detect a trailing `page i/N`.
            rlabel = right or (_TITLE_PAGER_RE.search(line).group(1)
                               if _TITLE_PAGER_RE.search(line) else "")
            if rlabel and f" {rlabel} " in line:
                rcol = line.rindex(f" {rlabel} ")  # rindex: the label is near the right end
                text.stylize(style, base + rcol, base + rcol + len(rlabel) + 2)
            break
    return text


# A `page i/N` paginator riding a box's top border (shaded with the title tab).
_TITLE_PAGER_RE = re.compile(r" (page \d+/\d+) ")


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
