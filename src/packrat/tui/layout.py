"""Pure text-grid helpers for the fixed monospace frame (component-plan §Layout).

Textual's CSS covers *widget-box* layout; these cover the **text-cell** concerns
CSS can't — horizontal cell alignment in a monospace grid (:func:`row`) and
vertical height budgeting inside the fixed 24-row frame (:func:`fit`), plus the
§12 long-path rule (:func:`middle_elide`). All functions are **pure and return
plain, colorless text** — width math and the golden-frame snapshot tests must
never see color markup (component-plan §Theming "the hard rule"). Color is a
separate layer, applied by a widget's render step *after* layout.

Everything here is importable without a Textual runtime (only :mod:`tokens`),
so the mockup generator can reuse it headless.

Invariant that makes "never widen the window" mechanical, not vigilant:
``len(row(width, …)) == width`` always, and ``len(fit(…, budget).rows) == budget``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .tokens import ELLIPSIS


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


def middle_elide(text: str, width: int, ellipsis: str = ELLIPSIS) -> str:
    """Collapse ``text`` from the middle to exactly ``width`` cells (§12 path rule).

    Keeps the **head** (drive + start) and **tail** (leaf) visible — the two ends
    carry a path's identity; the middle folders are the throwaway. The odd leftover
    cell is biased to the head so the drive/prefix stays as intact as possible.
    """
    if len(text) <= width:
        return text
    if width <= len(ellipsis):
        return ellipsis[:width]
    keep = width - len(ellipsis)
    head = (keep + 1) // 2          # bias the extra cell to the head (drive side)
    tail = keep - head
    return text[:head] + ellipsis + (text[-tail:] if tail else "")


def _elide(text: str, width: int, mode: str) -> str:
    """Shrink ``text`` to ``width`` cells per ``mode`` (used within a cell)."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if mode == "middle":
        return middle_elide(text, width)
    if mode == "none":
        return text[:width]        # hard cut, no ellipsis
    # 'end' (default): trailing ellipsis, drive/start kept
    if width <= len(ELLIPSIS):
        return ELLIPSIS[:width]
    return text[: width - len(ELLIPSIS)] + ELLIPSIS


def _align(text: str, width: int, align: str) -> str:
    """Place ``text`` (already ≤ ``width``) within ``width`` cells per ``align``."""
    pad = width - len(text)
    if pad <= 0:
        return text[:width]
    if align == "right":
        return " " * pad + text
    if align == "center":
        left = pad // 2
        return " " * left + text + " " * (pad - left)
    return text + " " * pad        # left (default)


def _render_cell(cell: Cell, width: int) -> str:
    """Render one cell into exactly ``width`` cells (elide over-width, then align)."""
    if width <= 0:
        return ""
    return _align(_elide(cell.text, width, cell.elide), width, cell.align)


def fit_width(s: str, width: int) -> str:
    """Pad/hard-truncate ``s`` to exactly ``width`` cells (the invariant backstop).

    Matches the generator's ``pad(s, n)``: left-aligned, right-padded, hard cut —
    so ``row(width, [Cell(text)])`` equals ``pad(text, width)`` for the common case,
    keeping runtime renders byte-identical to the generated mockup frames.
    """
    if len(s) < width:
        return s + " " * (width - len(s))
    return s[:width]


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
        w = c.width if c.width is not None else len(c.text)
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
            while len(w) > width:                # hard-break an over-wide token
                if cur:
                    lines.append(cur)
                    cur = ""
                lines.append(w[:width])
                w = w[width:]
            if not cur:
                cur = w
            elif len(cur) + 1 + len(w) <= width:
                cur += " " + w
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


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
