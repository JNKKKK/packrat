"""Queue interface (§4) — three INDEPENDENT fixed-height sections.

**Running** (the single active job), **Queued** (the durable backlog in dequeue
order, each with its blocked reason), and **History** (``recent_jobs`` terminal
history). Each section is a *fixed-height window* at a fixed position, with its
**own** paginator — so paging one section never resizes or clears another (that was
a bug when the three shared one scroll). Pure (dicts → lines); the screen routes
keys.

Navigation model (per-section focus):
- ``[r]`` / ``[q]`` / ``[h]`` focus the Running / Queued / History section; the
  focused section's header is UPPERCASED (→ accent-colored) and shows the ``▸``.
- ``↑/↓`` move the cursor **within the focused section only**; ``←/→`` page **that
  section only**. Each section keeps its own cursor + page.

The per-section renderers (:func:`section_pages`, :func:`header_line`,
:func:`window`, the ``*_line`` row builders) are also reused by the root-detail
jobs panel (:mod:`packrat.tui.screens.rootdetail`), which lays out the same three
sections inside a bordered box.
"""

from __future__ import annotations

from .. import render
from ..data import reltime
from ..geometry import REFERENCE, Geometry
from ..layout import Cell, fit, pager_line, row
from ..tokens import CURSOR, RUNNING

# Reference per-section budgets (fit the 24-row frame: 1+1 running, 1+6+1 queued,
# 1+6+1 history, + blanks = 20 rows, ≤ the 21 content rows). Live budgets come from
# Geometry (split the vertical surplus between Queued and History).
QUEUED_ROWS = 6
HISTORY_ROWS = 6

SECTIONS = ("running", "queued", "history")


def section_pages(n: int, rows: int) -> int:
    """Page count for a section of ``n`` items shown ``rows`` at a time.

    ``rows`` can be 0 when the panel is squeezed so tight a window gets no rows (only
    its header shows) — then there is 1 (empty) page, never a divide-by-zero."""
    if rows <= 0:
        return 1
    return max(1, -(-n // rows))


def section_jobs(section: str, running: dict | None, queued: list[dict],
                 history: list[dict]) -> list[dict]:
    """The selectable jobs of one section (running is a 0-or-1 list)."""
    if section == "running":
        return [running] if running else []
    if section == "queued":
        return queued
    return history


def header(label: str, state: str = "active") -> str:
    """A section header, cased to encode its focus state for :mod:`colorize`.

    Three states (colorize keys off the casing of the ``[K]abel:`` prefix):
    - ``"focused"`` → UPPERCASED (``[Q]UEUED:``) → whole-line accent;
    - ``"dim"``     → lowercased (``[q]ueued:``) → whole-line dim (inactive panel);
    - ``"active"``  → as written (``[Q]ueued:``) → default text, ``[K]`` accented.
    ``bool`` is accepted for back-compat (True → focused, False → active)."""
    if state is True:
        state = "focused"
    elif state is False:
        state = "active"
    if state == "focused":
        return label.upper()
    if state == "dim":
        return label.lower()
    return label


def queue_body(running: dict | None, queued: list[dict], history: list[dict],
               *, now: str, geo: Geometry = REFERENCE, focus: str = "queued",
               queued_cursor: int = 0, queued_page: int = 0,
               history_cursor: int = 0, history_page: int = 0,
               running_cursor: int = 0) -> list[str]:
    """Build the §4 body — three fixed-height sections, each independently paged.

    ``geo`` sizes each section's window (Queued/History grow on a taller terminal)
    and the paginator width. Rows lay out to ``geo``'s content width."""
    q_rows, h_rows = geo.queued_rows, geo.recent_rows
    w = geo.content_w
    lines: list[str] = []

    # -- Running (≤1 job; no paging) --
    run_focused = focus == "running"
    lines.append(header("[R]unning:", run_focused))
    if running:
        cur = CURSOR if run_focused else " "
        lines.append(running_line(running, cur, w))
    else:
        lines.append("  (nothing running)")
    lines.append("")

    # -- Queued (paginator sits on the header line, right-aligned) --
    q_focused = focus == "queued"
    q_pages = section_pages(len(queued), q_rows)
    lines.append(header_line("[Q]ueued (runs top-down):", q_focused, w,
                             min(queued_page, q_pages - 1) + 1, q_pages))
    lines += window(queued, q_rows, queued_page, queued_cursor, q_focused,
                    lambda j, c: queued_line(j, c, w), empty="  (backlog empty)")
    lines.append("")

    # -- History (paginator sits on the header line, right-aligned) --
    h_focused = focus == "history"
    h_pages = section_pages(len(history), h_rows)
    lines.append(header_line("[H]istory:", h_focused, w,
                             min(history_page, h_pages - 1) + 1, h_pages))
    lines += window(history, h_rows, history_page, history_cursor, h_focused,
                    lambda j, c: history_line(j, now, c, w), empty="  (no job history)")
    return lines


def header_line(label: str, state, width: int, cur_page: int, total: int) -> str:
    """A section header with its ``page i/N`` right-aligned on the same line (§4)."""
    return row(width, [Cell(header(label, state), grow=1),
                       Cell(f"page {cur_page}/{total}", align="right")], gap=2)


def window(jobs, budget, page, cursor, focused, line_fn, *, empty):
    """One section's fixed-height row window (▸ on the focused cursor)."""
    if not jobs:
        return fit([empty], budget, mode="clip").rows
    rows = [line_fn(j, CURSOR if (focused and i == cursor) else " ")
            for i, j in enumerate(jobs)]
    return fit(rows, budget, mode="scroll", page=page).rows


def focused_header_text(focus: str) -> str:
    """The UPPERCASED focused-section header (what colorize should accent as a line)."""
    return {
        "running": "[R]UNNING:",
        "queued": "[Q]UEUED (RUNS TOP-DOWN):",
        "history": "[H]ISTORY:",
    }.get(focus, "")


def running_line(job: dict, cur: str = " ", width: int = 96) -> str:
    """The running-job row: ``▸▶ #id label   ███░░░ 39% 17,800/45,000 ETA 26m``.

    Carries the same visual ``███░░░`` progress bar the dashboard queue preview shows
    (:func:`render.queue_row`) so the maximized Queue / root-detail Running section
    match the mock. The ``▶`` marker is on the label side, so the bar renders without
    a second marker (``running=False``)."""
    left = f"{cur}{RUNNING} #{job['id']} {job.get('label', job['type'])}"
    bar = render.progress_bar(job.get("done"), job.get("total"),
                              eta_s=job.get("_eta_s"), running=False)
    return row(width, [Cell(left, grow=1, elide="end"),
                       Cell(bar, align="right", style="running")], gap=2).rstrip()


def queued_line(job: dict, cur: str = " ", width: int = 96) -> str:
    note = render.job_status_note(job)
    left = f"{cur} #{job['id']} {job.get('label', job['type'])}"
    return row(width, [Cell(left, grow=1, elide="end"),
                       Cell(note, align="right", style="dim")], gap=2).rstrip()


def history_line(job: dict, now: str, cur: str = " ", width: int = 96) -> str:
    when = reltime(job.get("finished_at") or job.get("started_at"), now,
                   clock=(job.get("finished_at") or "")[:10] == (now or "")[:10])
    summ = _summary(job)
    status = job.get("status", "")
    left = f"{cur} #{job['id']} {job.get('label', job['type'])}"
    mid = f"{status}   {summ}".strip()
    # label grows; outcome summary + age pinned to the right (age last, 13 cells so
    # "today HH:MM" fits without eliding to "today 11:...").
    return row(width, [Cell(left, grow=2, elide="end"),
                       Cell(mid, grow=3, elide="end"),
                       Cell(when, width=13, align="right", style="dim")], gap=2).rstrip()


def _summary(job: dict) -> str:
    import json
    try:
        return json.loads(job.get("result_json") or "{}").get("summary", "") or ""
    except (ValueError, TypeError):
        return ""
