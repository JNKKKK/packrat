"""Dashboard content builder (§1) — the pure lines for the default screen.

Composes the Logo + Collection/Roots row + Queue box into the fixed frame body
(component-plan §1). Kept as a **pure builder** (snapshot dict → list[str]) so it
is golden-frame testable without a Textual pilot; the :class:`Dashboard` Textual
screen (in :mod:`packrat.tui.app`) just displays these lines and owns the
focus→maximize state machine + key routing (§ Navigation).
"""

from __future__ import annotations

from .. import render
from ..framing import box, hjoin
from ..geometry import REFERENCE, Geometry
from ..layout import fit

# Reference row budgets (the mockup counts) — the live budgets come from Geometry
# so a taller terminal shows more rows. Kept as names for the golden tests.
QUEUE_PREVIEW_ROWS = 4
ROOTS_PREVIEW_ROWS = 5

DOTKEY = "  ◉ deduped   ◐ scanned only   ○ never"


def dashboard_body(
    snap: dict,
    *,
    now: str,
    geo: Geometry = REFERENCE,
    focus: str | None = None,       # None | 'roots' | 'queue'
    roots_cursor: int = 0,
    roots_page: int = 0,
    queue_cursor: int = 0,
    queue_page: int = 0,
) -> list[str]:
    """Build the dashboard body lines (§1.1–§1.4), laid out to ``geo`` size.

    ``focus`` heavies the focused box's frame + shows a ``▸`` cursor + a title-bar
    paginator (§focus model). Roots are shown most-recently-registered first (a
    TUI-side id-DESC reorder of the id-ascending snapshot), windowed to
    ``geo.dash_roots_rows`` and paged so a long list never overflows the box. At the
    reference size this equals the original fixed layout.
    """
    assets = snap["assets"]
    roots = render.sort_roots(snap.get("roots", []), 0)  # default: most-recent-registered
    roots_rows = geo.dash_roots_rows

    # -- Top section: logo (left) + Collection box (right) --
    coll_box = box("Collection", render.collection_lines(snap, now=now), geo.collection_w)
    logo = render.logo_lines(assets, rows=geo.TOP_ROWS, width=geo.logo_w)
    top = hjoin(logo, coll_box)

    # -- Roots box (full width): a fixed-height window over the root list --
    all_rows = [
        render.root_row_compact(r, selected=(focus == "roots" and i == roots_cursor),
                               width=geo.row_w_compact, path_w=geo.path_w_compact)
        for i, r in enumerate(roots)
    ]
    fitted = fit(all_rows, roots_rows, mode="scroll", page=roots_page)
    root_lines = [*fitted.rows, DOTKEY]
    if focus == "roots":
        pager = f"page {roots_page + 1}/{fitted.total_pages}"
        roots_box = box("[R]OOTS", root_lines, geo.roots_w, right=pager, heavy=True)
    else:
        roots_box = box("[R]oots", root_lines, geo.roots_w)

    # -- Queue box (full width): running bar + queued preview, or idle message --
    if focus == "queue":
        queue_lines, pager = _queue_preview(snap, geo, focused=True,
                                            cursor=queue_cursor, page=queue_page)
        queue_box = box("[Q]UEUE", queue_lines, geo.queue_w, right=pager, heavy=True)
    else:
        queue_lines, _ = _queue_preview(snap, geo, focused=False)
        queue_box = box("[Q]ueue", queue_lines, geo.queue_w)

    return top + roots_box + queue_box


def _queue_rows(snap: dict, geo: Geometry) -> list[str]:
    """The queued-only rows (positionally numbered), running row prepended if any."""
    running = snap.get("running")
    queued = snap.get("queued", [])
    w = geo.queue_row_w
    rows: list[str] = []
    if running:
        rows.append(render.queue_row(running, show_id=False, width=w))
    for i, job in enumerate(queued):
        rows.append(render.queue_row(job, index=i + 2, width=w))
    return rows


def queue_preview_pages(snap: dict, geo: Geometry = REFERENCE) -> int:
    """Total pages of the focused dashboard queue box (for ←/→ clamping)."""
    n = len(_queue_rows(snap, geo))
    return max(1, -(-n // geo.dash_queue_rows))


def _queue_preview(snap: dict, geo: Geometry, *, focused: bool, cursor: int = 0,
                   page: int = 0) -> tuple[list[str], str]:
    """The dashboard queue box body + its paginator label.

    **Unfocused:** a fixed-height truncated preview (running + first few queued,
    then ``… N more`` — the full backlog lives in the maximized §4). **Focused:** a
    windowed, ``▸``-cursored page of the same rows, so ↑/↓ + ←/→ navigate the whole
    backlog in place (the paginator shows ``page i/N``)."""
    budget = geo.dash_queue_rows
    running = snap.get("running")
    queued = snap.get("queued", [])
    if not running and not queued:
        # Idle: a single-line box (it sizes to content, like the original mockup).
        return ["  idle — no jobs running or queued."], "page 1/1"

    if not focused:
        fitted = fit(_queue_rows(snap, geo), budget, mode="truncate")
        return fitted.rows, "page 1/1"

    # Focused: re-render with the ▸ on the selected row, windowed by page.
    w = geo.queue_row_w
    rows: list[str] = []
    if running:
        rows.append(render.queue_row(running, selected=(cursor == 0), show_id=False, width=w))
    for i, job in enumerate(queued):
        rows.append(render.queue_row(job, selected=(cursor == i + 1), index=i + 2, width=w))
    fitted = fit(rows, budget, mode="scroll", page=page)
    return fitted.rows, f"page {page + 1}/{fitted.total_pages}"
