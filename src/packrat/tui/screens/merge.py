"""Merge-from picker (§3.3) — pure body builder for the merge source form.

``[m]`` on a root opens this: the root is the fixed **destination**, and the user
picks the **source** via a radio — a paginated list of **library** roots (a trash
root is never a merge source; the dest is excluded), or an arbitrary **external
folder** path. A ``--dry-run`` toggle previews classification without copying.
Pure (dicts → lines); the Textual screen displays it and owns key routing. It
submits ``merge <source> --into <dest>`` (§8 C).
"""

from __future__ import annotations

from ..geometry import REFERENCE, Geometry
from ..layout import Cell, fit, row
from ..tokens import CURSOR


def merge_sources(roots: list[dict], dest_name: str) -> list[dict]:
    """The roots eligible as a merge source: library roots, excluding the dest."""
    return [r for r in roots
            if r.get("kind") == "library" and r.get("name") != dest_name]


def merge_body(dest: dict, sources: list[dict], *, geo: Geometry = REFERENCE,
               source_mode: str = "root", cursor: int = 0, page: int = 0,
               ext_path: str = "", dry_run: bool = False) -> list[str]:
    """Build the §3.3 merge-from body.

    ``source_mode`` is ``'root'`` (a paginated registered-root list) or ``'ext'``
    (a typed external-folder path). ``cursor``/``page`` navigate the root list;
    ``ext_path`` is the typed path; ``dry_run`` toggles the preview checkbox.
    """
    w = geo.content_w
    rule = "─" * (w - 2)

    def radio(on: bool) -> str:
        return "(•)" if on else "( )"

    def check(on: bool) -> str:
        return "[x]" if on else "[ ]"

    lines = [
        "Copy files new to the whole collection INTO this root, by exact hash.",
        rule,
        f"Destination   {dest['name']}   {dest['path']}",
        "",
        f"Source   {radio(source_mode == 'root')} Registered root     "
        f"{radio(source_mode == 'ext')} External folder",
        rule,
    ]

    if source_mode == "root":
        lines += _source_list(sources, geo, cursor, page)
    else:
        # external-folder path field (55-cell input affordance, like the add-root form)
        filled = ext_path + "_" * max(0, 55 - len(ext_path))
        lines += [
            f"  Path   {CURSOR} {filled}",
            "           (any readable folder — a temp export, a card, a share)",
        ]

    lines.append("")
    lines.append(f"{check(dry_run)} --dry-run   "
                 "(classify + preview counts; copies nothing — still empties trash)")
    return lines


# Rows the source-root list can show (rest of the frame below the header block).
# Header block above the list = 6 lines (intro, rule, dest, blank, source, rule);
# below it = blank + dry-run line = 2, plus the list's own paginator line = 1.
def source_list_rows(geo: Geometry) -> int:
    return max(1, geo.content_rows - 6 - 1 - 2)      # ref: 21-9 = 12


def _source_list(sources: list[dict], geo: Geometry, cursor: int, page: int) -> list[str]:
    from ..layout import pager_line
    budget = source_list_rows(geo)
    w = geo.content_w
    if not sources:
        rows = ["  (no eligible library roots to merge from)"]
        fitted = fit(rows, budget, mode="clip")
        return [*fitted.rows, pager_line(w, 1, 1)]
    rendered = [_source_row(r, w, selected=(i == cursor)) for i, r in enumerate(sources)]
    fitted = fit(rendered, budget, mode="scroll", page=page)
    return [*fitted.rows, pager_line(w, page + 1, fitted.total_pages)]


def _source_row(r: dict, width: int, *, selected: bool = False) -> str:
    """``▸ Name   path……………   N assets`` — path grows, count right-aligned (§3.3)."""
    cur = CURSOR if selected else " "
    return row(
        width,
        [
            Cell(cur, width=1, style="highlighted" if selected else None),
            Cell(r["name"], width=13),
            Cell(r["path"], grow=1, elide="middle"),
            Cell(f"{r['asset_count']:,} assets", width=15, align="right"),
        ],
    ).rstrip()
