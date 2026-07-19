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
from ..layout import fit
from ..tokens import COLLECTION_W, CW, ROOTS_W

# The dashboard queue box is a FIXED-HEIGHT preview (§1.2 note): logo+collection
# eat 15 rows, so it fits at most 4 item rows + 2 borders before the pinned
# footer. Overflow lives in the maximized §4. `fit(mode='truncate')` enforces this.
QUEUE_PREVIEW_ROWS = 4

DOTKEY = "  ◉ deduped   ◐ scanned only   ○ never"


# The dashboard roots box shows a fixed 5-row window (+ the dot legend), aligned
# with the Collection box's height (5 stat rows). Long root lists page within it
# (§12) — the paginator lives in the box's title bar when focused.
ROOTS_PREVIEW_ROWS = 5


def dashboard_body(
    snap: dict,
    *,
    now: str,
    focus: str | None = None,       # None | 'roots' | 'queue'
    roots_cursor: int = 0,
    roots_page: int = 0,
    queue_cursor: int = 0,
    queue_page: int = 0,
) -> list[str]:
    """Build the dashboard body lines (§1.1–§1.4).

    ``focus`` heavies the focused box's frame + shows a ``▸`` cursor + a title-bar
    paginator (§focus model). Roots are shown most-recently-registered first (a
    TUI-side id-DESC reorder of the id-ascending snapshot — Open Q#1), windowed to
    :data:`ROOTS_PREVIEW_ROWS` and paged so a long list never overflows the box.
    """
    assets = snap["assets"]
    roots = render.sort_roots(snap.get("roots", []), 0)  # default: most-recent-registered

    # Roots box (right of Collection): a fixed-height window over the root list.
    all_rows = [
        render.root_row_compact(r, selected=(focus == "roots" and i == roots_cursor))
        for i, r in enumerate(roots)
    ]
    fitted = fit(all_rows, ROOTS_PREVIEW_ROWS, mode="scroll", page=roots_page)
    root_lines = [*fitted.rows, DOTKEY]
    if focus == "roots":
        pager = f"page {roots_page + 1}/{fitted.total_pages}"
        roots_box = box("[R]OOTS", root_lines, ROOTS_W, right=pager, heavy=True)
    else:
        roots_box = box("[R]oots", root_lines, ROOTS_W)

    coll_box = box("Collection", render.collection_lines(snap, now=now), COLLECTION_W)

    # Queue box (full width, below). Running bar + queued preview, or idle message.
    if focus == "queue":
        queue_lines, pager = _queue_preview(snap, focused=True,
                                            cursor=queue_cursor, page=queue_page)
        queue_box = box("[Q]UEUE", queue_lines, CW - 2, right=pager, heavy=True)
    else:
        queue_lines, _ = _queue_preview(snap, focused=False)
        queue_box = box("[Q]ueue", queue_lines, CW - 2)

    return render.logo_lines(assets) + hjoin(coll_box, roots_box) + [""] + queue_box


def _queue_rows(snap: dict) -> list[str]:
    """The queued-only rows (positionally numbered), running row prepended if any."""
    running = snap.get("running")
    queued = snap.get("queued", [])
    rows: list[str] = []
    if running:
        rows.append(render.queue_row(running, show_id=False))
    for i, job in enumerate(queued):
        rows.append(render.queue_row(job, index=i + 2))
    return rows


def queue_preview_pages(snap: dict) -> int:
    """Total pages of the focused dashboard queue box (for ←/→ clamping)."""
    n = len(_queue_rows(snap))
    return max(1, -(-n // QUEUE_PREVIEW_ROWS))


def _queue_preview(snap: dict, *, focused: bool, cursor: int = 0,
                   page: int = 0) -> tuple[list[str], str]:
    """The dashboard queue box body + its paginator label.

    **Unfocused:** a fixed truncated preview (running + first few queued, then
    ``… N more`` — the full backlog lives in the maximized §4). **Focused:** a
    windowed, ``▸``-cursored page of the same rows, so ↑/↓ + ←/→ navigate the whole
    backlog in place (the paginator shows ``page i/N``)."""
    running = snap.get("running")
    queued = snap.get("queued", [])
    if not running and not queued:
        return ["  idle — no jobs running or queued."], "page 1/1"

    if not focused:
        fitted = fit(_queue_rows(snap), QUEUE_PREVIEW_ROWS, mode="truncate")
        return fitted.rows, "page 1/1"

    # Focused: re-render with the ▸ on the selected row, windowed by page.
    rows: list[str] = []
    if running:
        rows.append(render.queue_row(running, selected=(cursor == 0), show_id=False))
    for i, job in enumerate(queued):
        rows.append(render.queue_row(job, selected=(cursor == i + 1), index=i + 2))
    fitted = fit(rows, QUEUE_PREVIEW_ROWS, mode="scroll", page=page)
    return fitted.rows, f"page {page + 1}/{fitted.total_pages}"
