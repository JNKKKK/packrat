"""Roots interface (§2) — maximized root list + add-root form, pure body builders.

``roots_body`` builds the §2.1 list (sortable via the ``[s]`` cycle, paginated);
``add_root_body`` builds the §2.2 register form. Both are pure (dict → lines) and
golden-frame testable; the Textual screens display them and own key routing.
"""

from __future__ import annotations

from .. import render
from ..layout import fit, pager_line
from ..tokens import CW

RULE = "─" * (CW - 4)
DOTKEY_WIDE = "◉ scanned + deduped   ◐ scanned only   ○ never scanned"

# Body rows available to the list before the footer/header (§2.1 has a header
# line + rule, the paginator, a blank, and the dot legend around the list).
LIST_ROWS = 5


def roots_body(roots: list[dict], *, now: str, sort_mode: int = 0,
               cursor: int = 0, page: int = 0) -> list[str]:
    """The §2.1 maximized root list, sorted per the ``[s]`` cycle + paginated."""
    ordered = render.sort_roots(roots, sort_mode)
    rows = [
        render.root_row_wide(r, now=now, selected=(i == cursor))
        for i, r in enumerate(ordered)
    ]
    fitted = fit(rows, LIST_ROWS, mode="scroll", page=page)
    return [
        render.sort_header(sort_mode),
        RULE,
        *fitted.rows,
        pager_line(CW - 2, page + 1, fitted.total_pages),
        "",
        DOTKEY_WIDE,
    ]


def add_root_body(*, path: str = "", name: str = "", kind: str = "library",
                  scan: bool = True, full: bool = False, embed: bool = False,
                  focus_field: str = "path", error: str | None = None) -> list[str]:
    """The §2.2 add-root (register) form body.

    Radio/checkbox glyphs reflect the current selection; ``focus_field`` (one of
    :data:`ADD_ROOT_FIELDS`) puts the ``▸`` cursor on the focused field so ``[Tab]``
    navigation is visible; an inline ``error`` (a ``RootError``) shows under the
    path (component-plan: validate inline on Enter).
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

    # The path field shows typed text padded with underscores as an input
    # affordance (55-cell field, matching the §2.2 mockup).
    filled = path + "_" * max(0, 55 - len(path))
    lines = [
        "Register a new root (metadata-only; scan it afterward).",
        RULE,
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
        f"{cur('scan')}{check(scan)} scan immediately after registering   "
        f"{radio(full)} --full   {radio(embed)} --embed",
        "",
        "  ‹trash roots are never scanned; --full/--embed apply only with scan›",
    ]
    return lines


# The [Tab] focus order across the add-root form fields (§2.2).
ADD_ROOT_FIELDS = ("path", "name", "kind", "scan")
