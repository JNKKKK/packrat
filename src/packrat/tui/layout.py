"""Pure text-grid helpers for the monospace frame.

Textual's CSS covers *widget-box* layout; these cover the **text-cell** concerns
CSS can't — horizontal cell alignment in a monospace grid (:func:`row`), vertical
height budgeting (:func:`fit`), the long-path rule (:func:`middle_elide`), and
CJK-aware display width (:func:`cell_width` — East-Asian wide chars count as 2).
All functions are **pure and return plain, colorless text** — width math and the
frame snapshot tests must never see color markup (§Theming "the hard rule"). Color
is a separate layer, applied by a widget's render step *after* layout.

Everything here is importable without a Textual runtime (only :mod:`tokens`), so it
renders headless.

Invariant that makes "never overflow the frame" mechanical, not vigilant:
``cell_width(row(width, …)) == width``, and ``len(fit(…, budget).rows) == budget``.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass

from .tokens import ELLIPSIS


# --- display width (CJK-aware) ---------------------------------------------
# A monospace terminal renders East-Asian Wide/Fullwidth characters (Chinese,
# Japanese, fullwidth forms) as TWO cells, but ``len()`` counts them as one — so
# any width math on strings containing them under-measures and breaks alignment.
# These helpers measure and slice by *display cells*. For ASCII + all our box/dot
# glyphs (which are narrow/ambiguous → 1 cell) they equal len()/slicing, so the
# 100×24 golden frames are byte-identical; only real CJK content differs.
def char_width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cell_width(text: str) -> int:
    """Display width of ``text`` in terminal cells (CJK wide chars count as 2)."""
    return sum(char_width(c) for c in text)


def cell_truncate(text: str, width: int) -> str:
    """Longest prefix of ``text`` whose display width is ≤ ``width`` (never splits a
    wide char across the boundary; may leave 1 cell short if a wide char straddles it)."""
    if width <= 0:
        return ""
    out, used = [], 0
    for ch in text:
        w = char_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def cell_pad(text: str, width: int, align: str = "left") -> str:
    """Pad ``text`` to exactly ``width`` display cells (truncates if over-wide).

    A trailing 1-cell gap can appear when a wide char straddles the cut — filled
    with a space so the result is always exactly ``width`` cells."""
    cut = cell_truncate(text, width)
    pad = width - cell_width(cut)
    if pad <= 0:
        return cut
    if align == "right":
        return " " * pad + cut
    if align == "center":
        left = pad // 2
        return " " * left + cut + " " * (pad - left)
    return cut + " " * pad


@dataclass
class Cell:
    """One labelled cell in a :func:`row` (component-plan §Layout).

    ``width`` fixes the cell to exactly that many cells (the RootRow norm — why
    the mockup columns align); ``grow`` shares leftover space weighted by its
    value (the exception, for a single flexible cell). ``align`` positions text
    within the cell; ``elide`` shrinks over-width text. ``style`` is a **semantic
    role name** (e.g. ``'running'``/``'warn'``/``'dim'``) — layout *ignores* it
    entirely; only a widget's render step maps it to a theme color (§Theming).
    """

    text: str
    width: int | None = None       # FIXED cell: exactly this many cells
    grow: int = 0                  # GROW cell: shares leftover, weighted
    align: str = "left"            # 'left' | 'right' | 'center'
    elide: str = "end"             # 'none' | 'middle' | 'end'
    style: str | None = None       # SEMANTIC role, never a raw color


def _tail_by_cells(text: str, width: int) -> str:
    """Longest *suffix* of ``text`` whose display width is ≤ ``width`` (CJK-aware)."""
    if width <= 0:
        return ""
    out, used = [], 0
    for ch in reversed(text):
        w = char_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(reversed(out))


def middle_elide(text: str, width: int, ellipsis: str = ELLIPSIS) -> str:
    """Collapse ``text`` from the middle to ≤ ``width`` display cells (§12 path rule).

    Keeps the **head** (drive + start) and **tail** (leaf) visible — the two ends
    carry a path's identity; the middle folders are the throwaway. The odd leftover
    cell is biased to the head. CJK-aware: measures/slices by display cells (a wide
    char straddling the boundary can leave the result 1 cell short — the cell caller
    pads it, so the row still ends up exactly ``width``).
    """
    if cell_width(text) <= width:
        return text
    ell = cell_width(ellipsis)
    if width <= ell:
        return cell_truncate(ellipsis, width)
    keep = width - ell
    head = (keep + 1) // 2          # bias the extra cell to the head (drive side)
    tail = keep - head
    return cell_truncate(text, head) + ellipsis + (_tail_by_cells(text, tail) if tail else "")


def end_elide(text: str, width: int, ellipsis: str = ELLIPSIS) -> str:
    """Shrink ``text`` from the END to ≤ ``width`` display cells (``head…``; CJK-aware).

    The ``elide='end'`` cell rule (keep the drive/start, drop the tail), exposed so a
    post-layout pass can reproduce EXACTLY what a fixed-width :class:`Cell` renders for
    over-width text (e.g. a long root NAME the colorizer must re-locate in the frame) —
    one source of truth, so the display form and the match form can never drift."""
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    if width <= cell_width(ellipsis):
        return cell_truncate(ellipsis, width)
    return cell_truncate(text, width - cell_width(ellipsis)) + ellipsis


def _elide(text: str, width: int, mode: str) -> str:
    """Shrink ``text`` to ≤ ``width`` display cells per ``mode`` (used within a cell)."""
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    if mode == "middle":
        return middle_elide(text, width)
    if mode == "none":
        return cell_truncate(text, width)        # hard cut, no ellipsis
    return end_elide(text, width)                # 'end' (default): trailing ellipsis


def _align(text: str, width: int, align: str) -> str:
    """Place ``text`` within ``width`` display cells per ``align`` (pads/truncates)."""
    return cell_pad(text, width, align)


def _render_cell(cell: Cell, width: int) -> str:
    """Render one cell into exactly ``width`` cells (elide over-width, then align)."""
    if width <= 0:
        return ""
    return _align(_elide(cell.text, width, cell.elide), width, cell.align)


def fit_width(s: str, width: int) -> str:
    """Pad/hard-truncate ``s`` to exactly ``width`` display cells (invariant backstop).

    Matches the generator's ``pad(s, n)`` for ASCII/glyph text; CJK-aware so a row
    containing Chinese still ends up exactly ``width`` terminal cells wide.
    """
    return cell_pad(s, width, "left")


def row(width: int, cells: list[Cell], *, gap: int = 1, justify: str = "pack") -> str:
    """Compose one fixed-width line from ``cells`` (result is ALWAYS ``width`` cells).

    Sizing: fixed cells (``width=``) keep their width; the remaining space (minus
    ``gap`` between cells) is divided among ``grow`` cells in proportion to their
    ``grow`` weight. ``justify`` places the cells when their widths don't fill the
    row *and there are no grow cells*: ``'pack'`` (left, default) | ``'between'``
    (edge-to-edge, e.g. HintBar) | ``'center'``. Each cell's own text is positioned
    by its ``align``; an over-width cell is shrunk by its ``elide``.

    Color is orthogonal — this returns PLAIN text; the caller colors spans after.
    """
    if not cells:
        return " " * width
    n = len(cells)
    gaps_total = gap * (n - 1)
    grow_weight = sum(c.grow for c in cells)

    widths: list[int] = [0] * n
    fixed_total = 0
    for i, c in enumerate(cells):
        if c.grow > 0:
            continue
        w = c.width if c.width is not None else cell_width(c.text)
        widths[i] = w
        fixed_total += w

    if grow_weight > 0:
        leftover = max(0, width - fixed_total - gaps_total)
        grow_idxs = [i for i, c in enumerate(cells) if c.grow > 0]
        assigned = 0
        for j, i in enumerate(grow_idxs):
            if j == len(grow_idxs) - 1:
                widths[i] = leftover - assigned      # last grow cell absorbs the remainder
            else:
                share = leftover * cells[i].grow // grow_weight
                widths[i] = share
                assigned += share
        line = (" " * gap).join(_render_cell(cells[i], widths[i]) for i in range(n))
        return fit_width(line, width)

    rendered = [_render_cell(cells[i], widths[i]) for i in range(n)]
    slack = width - fixed_total - gaps_total
    if slack <= 0:
        return fit_width((" " * gap).join(rendered), width)
    if justify == "center":
        left = slack // 2
        return " " * left + (" " * gap).join(rendered) + " " * (slack - left)
    if justify == "between" and n > 1:
        base, extra = divmod(slack, n - 1)     # spread slack edge-to-edge across gaps
        out = rendered[0]
        for i in range(1, n):
            out += " " * (gap + base + (1 if i <= extra else 0)) + rendered[i]
        return fit_width(out, width)
    # pack (default): cells left, all slack at the right
    return fit_width((" " * gap).join(rendered) + " " * slack, width)


def pager_line(width: int, cur: int = 1, total: int = 1) -> str:
    """A centered ``page i/N`` indicator (component-plan §Layout, §2 paginator).

    Drawn directly beneath a scrollable list; shown even at ``1/1`` so the paging
    control is always visible (the ``←/→`` keys move between pages). Full-width so
    ``len == width``; equals ``row(width, [Cell(s, align='center')], justify='center')``.
    """
    return row(width, [Cell(f"page {cur}/{total}", align="center")], justify="center")


def wrap_cells(text: str, width: int) -> list[str]:
    """Word-wrap ``text`` to ``width`` cells (monospace), hard-breaking long tokens.

    The roomy detail/card views (§3, §5) wrap a long NAS path over multiple lines
    rather than eliding it (vertical space is cheaper there — §12). Preserves
    explicit newlines; a token longer than ``width`` is hard-broken.
    """
    if width <= 0:
        return [""]
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for w in para.split(" "):
            while cell_width(w) > width:         # hard-break an over-wide token
                if cur:
                    lines.append(cur)
                    cur = ""
                head = cell_truncate(w, width)
                lines.append(head)
                w = w[len(head):]
            if not cur:
                cur = w
            elif cell_width(cur) + 1 + cell_width(w) <= width:
                cur += " " + w
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def wrap_hints(footer: str, width: int, *, sep: str = "   ") -> list[str]:
    """Wrap a hint-bar string to ``width`` cells, breaking only between hint *groups*.

    A footer is ``[k] label`` groups separated by runs of 2+ spaces; we never split
    inside a group (so ``[x] cancel all`` stays intact). Fits on one line → returns
    ``[footer]`` unchanged; otherwise greedily packs groups into successive lines,
    joined by ``sep``. This is what lets a long footer become two lines on a narrow
    terminal instead of being truncated or hand-abbreviated."""
    if cell_width(footer) <= width or width <= 0:
        return [footer]
    import re

    groups = [g for g in re.split(r" {2,}", footer.strip()) if g]
    lines: list[str] = []
    cur = ""
    for g in groups:
        if not cur:
            cur = g
        elif cell_width(cur) + cell_width(sep) + cell_width(g) <= width:
            cur += sep + g
        else:
            lines.append(cur)
            cur = g
    if cur:
        lines.append(cur)
    return lines or [footer]


@dataclass
class Fitted:
    """Result of :func:`fit` — a budget-sized block ready to drop into a Panel."""

    rows: list[str]        # EXACTLY `budget` lines (padded with "" as needed)
    overflow: int          # source lines that didn't fit (0 if all shown)
    scrollable: bool       # True if mode='scroll' and overflow>0 (a PagedList pages)
    total_pages: int       # ceil(len(lines)/budget); feeds pager_line(width, cur, total)


def fit(lines: list[str], budget: int, *, mode: str = "scroll", page: int = 0) -> Fitted:
    """Fit ``lines`` into ``budget`` rows (``Fitted.rows`` is always exactly ``budget``).

    - ``'scroll'``   → page through all lines (PagedList/detail Jobs); render the
      ``page``-th window of ``budget`` lines.
    - ``'truncate'`` → keep ``budget-1`` lines + a ``… N more`` marker (compact
      previews, e.g. the §1.2 dashboard queue box).
    - ``'clip'``     → hard cut to ``budget`` (last resort).

    Returning more than ``budget`` rows is impossible by construction — that is what
    makes §12's "if it can't fit, trim it, don't grow the window" mechanical.
    """
    budget = max(0, budget)
    total = len(lines)
    total_pages = max(1, math.ceil(total / budget)) if budget else 1

    if mode == "truncate" and total > budget and budget >= 1:
        n_more = total - (budget - 1)
        shown = lines[: budget - 1] + [f"{ELLIPSIS} {n_more} more"]
        return Fitted(_pad_rows(shown, budget), n_more, False, total_pages)

    if mode == "scroll":
        start = max(0, page) * budget
        window = lines[start : start + budget]
        overflow = max(0, total - budget)
        return Fitted(_pad_rows(window, budget), overflow, overflow > 0, total_pages)

    # 'clip' (and truncate/short lists that already fit)
    window = lines[:budget]
    return Fitted(_pad_rows(window, budget), max(0, total - budget), False, total_pages)


def _pad_rows(rows: list[str], budget: int) -> list[str]:
    """Pad/clip a line list to exactly ``budget`` entries (fill with empty rows)."""
    rows = list(rows[:budget])
    rows += [""] * (budget - len(rows))
    return rows
