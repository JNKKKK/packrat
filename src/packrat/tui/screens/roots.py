"""Roots interface (§2) — maximized root list + add-root form, pure body builders.

``roots_body`` builds the §2.1 list (sortable via the ``[s]`` cycle, paginated);
``add_root_body`` builds the §2.2 register form. Both are pure (dict → lines) and
golden-frame testable; the Textual screens display them and own key routing.
"""

from __future__ import annotations

from .. import render
from ..geometry import REFERENCE, Geometry
from ..layout import Cell, fit, row

DOTKEY_WIDE = "◉ scanned + deduped   ◐ scanned only   ○ never scanned"

# Reference list-row budget (the mockup count); the live budget comes from Geometry.
LIST_ROWS = 5


def roots_body(roots: list[dict], *, now: str, geo: Geometry = REFERENCE,
               sort_mode: int = 0, cursor: int = 0, page: int = 0) -> list[str]:
    """The §2.1 maximized root list, sorted per the ``[s]`` cycle + paginated.

    Layout: the sort header, then one line with the dot legend (left) + ``page i/N``
    (right), then a rule, then the list filling the rest of the frame. Rows lay out
    to ``geo``'s content width (dot/count/recency right-aligned, path grows)."""
    w = geo.content_w
    rule = "─" * (w - 2)
    ordered = render.sort_roots(roots, sort_mode)
    rows = [
        render.root_row_wide(r, now=now, selected=(i == cursor), width=w)
        for i, r in enumerate(ordered)
    ]
    fitted = fit(rows, geo.roots_list_rows, mode="scroll", page=page)
    # dot legend (left) + paginator (right) share one line at the top.
    pager = f"page {page + 1}/{fitted.total_pages}"
    legend_line = row(w, [Cell(DOTKEY_WIDE, grow=1), Cell(pager, align="right")], gap=2)
    return [
        render.sort_header(sort_mode),
        legend_line,
        rule,
        *fitted.rows,
    ]


def add_root_body(*, path: str = "", name: str = "", kind: str = "library",
                  scan: bool = True, full: bool = False,
                  focus_field: str = "path", error: str | None = None,
                  geo: Geometry = REFERENCE) -> list[str]:
    """The §2.2 add-root (register) form body.

    Radio/checkbox glyphs reflect the current selection; ``focus_field`` (one of
    :data:`ADD_ROOT_FIELDS`) puts the ``▸`` cursor on the focused field so ``[Tab]``
    navigation is visible; an inline ``error`` (a ``RootError``) shows under the
    path (component-plan: validate inline on Enter). The form fields are fixed-width;
    only the rule line spans ``geo``'s content width.

    ``--embed`` is intentionally NOT offered — the embedding pass is deferred to M7
    (a plain scan writes no embeddings), so the form exposes only the flags that do
    something today: ``scan`` and its ``--full`` re-hash. Both are togglable fields.
    """
    def radio(on: bool) -> str:
        return "(•)" if on else "( )"

    def check(on: bool) -> str:
        return "[x]" if on else "[ ]"

    # A 2-cell focus marker ("▸ " when focused, "  " otherwise) placed WITHIN each
    # field's existing indentation, so the unfocused form is byte-identical to the
    # §2.2 mockup and every line keeps the same width (the golden-frame contract).
    def cur(field: str) -> str:
        return "▸ " if focus_field == field else "  "

    rule = "─" * (geo.content_w - 2)
    # The path field shows typed text padded with underscores as an input affordance.
    # The prefix "  Path   " (9) + the 2-cell focus marker = 11 cells, so the field
    # extends from there toward the rule's right end, minus a small right margin — it
    # grows with the terminal width instead of a fixed 55.
    PATH_PREFIX = 11
    RIGHT_MARGIN = 4
    field_w = max(20, (geo.content_w - 2) - PATH_PREFIX - RIGHT_MARGIN)
    filled = path + "_" * max(0, field_w - len(path))
    lines = [
        "Register a new root (metadata-only; scan it afterward).",
        rule,
        f"  Path   {cur('path')}{filled}",
        "           (must exist, be a readable directory, not overlap a root)",
    ]
    if error:
        lines.append(f"           ⚠ {error}")
    else:
        lines.append("")
    lines += [
        # "Name   ▸ [ … ]" focused / "Name     [ … ]" not — same width either way.
        f"  Name   {cur('name')}[ {name} ]   ‹defaults to the folder leaf; must be unique›",
        "",
        f"  Kind   {cur('kind')}{radio(kind == 'library')} library    {radio(kind == 'trash')} trash",
        "",
        # scan line has no label; the marker replaces the leading 2-space indent.
        f"{cur('scan')}{check(scan)} scan immediately after registering",
        # --full is its own focusable checkbox, left-aligned WITH the scan line (marker at
        # col 0, checkbox at col 2) so the two stack as a clean two-item list.
        f"{cur('full')}{check(full)} --full  (re-hash every file, not just new/changed)",
        "",
        "  ‹trash roots are never scanned; --full applies only with scan›",
    ]
    return lines


# The [Tab] focus order across the add-root form fields (§2.2). `--embed` is omitted —
# the embedding pass is deferred (M7), so the form only offers flags that do something.
ADD_ROOT_FIELDS = ("path", "name", "kind", "scan", "full")
