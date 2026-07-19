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
from ..layout import fit, pager_line
from ..tokens import CURSOR, CW, RUNNING

# Per-section content-row budgets (fit the fixed frame: 1+1 running, 1+6+1 queued,
# 1+6+1 recent, + blanks = 20 rows, ≤ the 21 content rows screen() allows).
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
               *, now: str, focus: str = "queued",
               queued_cursor: int = 0, queued_page: int = 0,
               recent_cursor: int = 0, recent_page: int = 0,
               running_cursor: int = 0) -> list[str]:
    """Build the §4 body — three fixed-height sections, each independently paged."""
    lines: list[str] = []

    # -- Running (≤1 job; no paging) --
    run_focused = focus == "running"
    lines.append(_header("[R]unning:", "r", run_focused))
    if running:
        cur = CURSOR if run_focused else " "
        lines.append(_running_line(running, cur))
    else:
        lines.append("  (nothing running)")
    lines.append("")

    # -- Queued (own window + paginator) --
    q_focused = focus == "queued"
    lines.append(_header("[Q]ueued (runs top-down):", "q", q_focused))
    lines += _window(queued, QUEUED_ROWS, queued_page, queued_cursor, q_focused,
                     lambda j, c: _queued_line(j, c), empty="  (backlog empty)")
    lines.append(pager_line(CW - 2, min(queued_page, section_pages(len(queued), QUEUED_ROWS) - 1) + 1,
                            section_pages(len(queued), QUEUED_ROWS)))
    lines.append("")

    # -- Recent (own window + paginator) --
    r_focused = focus == "recent"
    lines.append(_header("Rec[e]nt:", "e", r_focused))
    lines += _window(recent, RECENT_ROWS, recent_page, recent_cursor, r_focused,
                     lambda j, c: _recent_line(j, now, c), empty="  (no recent jobs)")
    lines.append(pager_line(CW - 2, min(recent_page, section_pages(len(recent), RECENT_ROWS) - 1) + 1,
                            section_pages(len(recent), RECENT_ROWS)))
    return lines


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


def _running_line(job: dict, cur: str = " ") -> str:
    # Percent/counts/ETA inline (no bar glyphs — the bar is the dashboard/card).
    pct = ""
    if job.get("total"):
        pct = f"{int(100 * (job.get('done') or 0) / job['total'])}% {job['done']:,}/{job['total']:,}"
        eta = render.fmt_eta(job.get("_eta_s"))
        pct = f"{pct} {eta}".strip()
    return f"{cur}{RUNNING} #{job['id']} {job.get('label', job['type'])}   {pct}  running"


def _queued_line(job: dict, cur: str = " ") -> str:
    note = render.job_status_note(job)
    return f"{cur} #{job['id']} {job.get('label', job['type']):<28} {note}"


def _recent_line(job: dict, now: str, cur: str = " ") -> str:
    when = reltime(job.get("finished_at") or job.get("started_at"), now,
                   clock=(job.get("finished_at") or "")[:10] == (now or "")[:10])
    summ = _summary(job)
    status = job.get("status", "")
    tail = f"{status}   {summ}".strip()
    return f"{cur} #{job['id']} {job.get('label', job['type']):<26} {tail}  {when}"


def _summary(job: dict) -> str:
    import json
    try:
        return json.loads(job.get("result_json") or "{}").get("summary", "") or ""
    except (ValueError, TypeError):
        return ""
