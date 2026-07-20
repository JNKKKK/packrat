"""Queue interface (§4) — three INDEPENDENT fixed-height sections.

**Running** (the single active job), **Queued** (the durable backlog in dequeue
order, each with its blocked reason), and **Recent** (``recent_jobs`` history).
Each section is a *fixed-height window* at a fixed position, with its **own**
paginator — so paging one section never resizes or clears another (that was a bug
when the three shared one scroll). Pure (dicts → lines); the screen routes keys.

Navigation model (per-section focus):
- ``[r]`` / ``[q]`` / ``[e]`` focus the Running / Queued / Recent section; the
  focused section's header is UPPERCASED (→ accent-colored) and shows the ``▸``.
- ``↑/↓`` move the cursor **within the focused section only**; ``←/→`` page **that
  section only**. Each section keeps its own cursor + page.
"""

from __future__ import annotations

from .. import render
from ..data import reltime
from ..geometry import REFERENCE, Geometry
from ..layout import Cell, fit, pager_line, row
from ..tokens import CURSOR, RUNNING

# Reference per-section budgets (fit the 24-row frame: 1+1 running, 1+6+1 queued,
# 1+6+1 recent, + blanks = 20 rows, ≤ the 21 content rows). Live budgets come from
# Geometry (split the vertical surplus between Queued and Recent).
QUEUED_ROWS = 6
RECENT_ROWS = 6

SECTIONS = ("running", "queued", "recent")


def section_pages(n: int, rows: int) -> int:
    """Page count for a section of ``n`` items shown ``rows`` at a time."""
    return max(1, -(-n // rows))


def section_jobs(section: str, running: dict | None, queued: list[dict],
                 recent: list[dict]) -> list[dict]:
    """The selectable jobs of one section (running is a 0-or-1 list)."""
    if section == "running":
        return [running] if running else []
    if section == "queued":
        return queued
    return recent


def _header(label: str, key: str, focused: bool) -> str:
    """A section header — UPPERCASED when focused, so colorize accents the line."""
    return label.upper() if focused else label


def queue_body(running: dict | None, queued: list[dict], recent: list[dict],
               *, now: str, geo: Geometry = REFERENCE, focus: str = "queued",
               queued_cursor: int = 0, queued_page: int = 0,
               recent_cursor: int = 0, recent_page: int = 0,
               running_cursor: int = 0) -> list[str]:
    """Build the §4 body — three fixed-height sections, each independently paged.

    ``geo`` sizes each section's window (Queued/Recent grow on a taller terminal)
    and the paginator width. Rows lay out to ``geo``'s content width."""
    q_rows, r_rows = geo.queued_rows, geo.recent_rows
    w = geo.content_w
    lines: list[str] = []

    # -- Running (≤1 job; no paging) --
    run_focused = focus == "running"
    lines.append(_header("[R]unning:", "r", run_focused))
    if running:
        cur = CURSOR if run_focused else " "
        lines.append(_running_line(running, cur, w))
    else:
        lines.append("  (nothing running)")
    lines.append("")

    # -- Queued (paginator sits on the header line, right-aligned) --
    q_focused = focus == "queued"
    q_pages = section_pages(len(queued), q_rows)
    lines.append(_header_line("[Q]ueued (runs top-down):", q_focused, w,
                              min(queued_page, q_pages - 1) + 1, q_pages))
    lines += _window(queued, q_rows, queued_page, queued_cursor, q_focused,
                     lambda j, c: _queued_line(j, c, w), empty="  (backlog empty)")
    lines.append("")

    # -- Recent (paginator sits on the header line, right-aligned) --
    r_focused = focus == "recent"
    r_pages = section_pages(len(recent), r_rows)
    lines.append(_header_line("Rec[e]nt:", r_focused, w,
                              min(recent_page, r_pages - 1) + 1, r_pages))
    lines += _window(recent, r_rows, recent_page, recent_cursor, r_focused,
                     lambda j, c: _recent_line(j, now, c, w), empty="  (no recent jobs)")
    return lines


def _header_line(label: str, focused: bool, width: int, cur_page: int, total: int) -> str:
    """A section header with its ``page i/N`` right-aligned on the same line (§4)."""
    header = _header(label, "", focused)
    return row(width, [Cell(header, grow=1), Cell(f"page {cur_page}/{total}", align="right")],
               gap=2)


def _window(jobs, budget, page, cursor, focused, line_fn, *, empty):
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
        "recent": "REC[E]NT:",
    }.get(focus, "")


def _running_line(job: dict, cur: str = " ", width: int = 96) -> str:
    # Label grows; percent/counts/ETA + "running" sit at the right end.
    pct = ""
    if job.get("total"):
        pct = f"{int(100 * (job.get('done') or 0) / job['total'])}% {job['done']:,}/{job['total']:,}"
        eta = render.fmt_eta(job.get("_eta_s"))
        pct = f"{pct} {eta}".strip()
    left = f"{cur}{RUNNING} #{job['id']} {job.get('label', job['type'])}"
    right = f"{pct}  running".strip()
    return row(width, [Cell(left, grow=1, elide="end"),
                       Cell(right, align="right", style="running")], gap=2).rstrip()


def _queued_line(job: dict, cur: str = " ", width: int = 96) -> str:
    note = render.job_status_note(job)
    left = f"{cur} #{job['id']} {job.get('label', job['type'])}"
    return row(width, [Cell(left, grow=1, elide="end"),
                       Cell(note, align="right", style="dim")], gap=2).rstrip()


def _recent_line(job: dict, now: str, cur: str = " ", width: int = 96) -> str:
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
