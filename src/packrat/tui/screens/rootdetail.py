"""Root detail interface (§3) — pure body builder over ``root_detail(root)``.

Renders the counts + scan/dedup recency header, the pending-review banner (the
actionable case, §3.1) or the no-pending-review line (§3.2), the last-scan summary,
and the per-root jobs history (from ``root_jobs``), paginated. Pure (dict → lines);
the Textual screen displays it and routes ``[s]``/``[d]``/``[m]``/``[o]``/``[g]``/
``[k]`` to the CLI verbs.
"""

from __future__ import annotations

from .. import render
from ..data import reltime
from ..geometry import REFERENCE, Geometry
from ..layout import Cell, fit, row
from ..tokens import CURSOR, WARN

JOBS_ROWS = 4    # reference Jobs-list budget; live budget comes from Geometry


def detail_header_right(d: dict) -> str:
    """The top-border right label: ``<path> · <kind>``."""
    return f"{d['path']} · {d['kind']}"


def detail_body(d: dict, *, now: str, geo: Geometry = REFERENCE,
                jobs: list[dict] | None = None,
                jobs_cursor: int = 0, jobs_page: int = 0) -> list[str]:
    """Build the §3 root-detail body for root ``d`` (with its ``jobs`` history).

    The Jobs list window is ``geo.jobs_rows`` tall (grows on a taller terminal);
    rule lines + job rows span ``geo``'s content width."""
    jobs = jobs or []
    rule = "─" * (geo.content_w - 2)
    row_w = geo.content_w
    photos, videos = d["photos"], d["videos"]
    lines = [
        f"assets  {photos + videos:,}  (photos {photos:,} · videos {videos:,})"
        f"     files {d['instances']:,}",
        f"scanned {reltime(d.get('last_scan_at'), now)}    "
        f"last full scan {reltime(d.get('last_full_scan_at'), now)}    "
        f"deduped {reltime(d.get('last_dedup_at'), now, clock=_is_today(d.get('last_dedup_at'), now))}",
        rule,
    ]
    lines += _review_banner(d, now)
    lines.append(rule)
    job_rows = [
        _job_history_row(j, now, selected=(i == jobs_cursor), width=row_w)
        for i, j in enumerate(jobs)
    ]
    fitted = fit(job_rows, geo.jobs_rows, mode="scroll", page=jobs_page)
    # "Jobs (newest first):" (left) + page i/N (right) share one line, like the
    # queue interface's section headers.
    pager = f"page {jobs_page + 1}/{fitted.total_pages}"
    lines.append(row(row_w, [Cell("Jobs (newest first):", grow=1),
                             Cell(pager, align="right")], gap=2))
    lines += fitted.rows
    return lines


def _is_today(ts, now) -> bool:
    return bool(ts) and (ts or "")[:10] == (now or "")[:10]


def _review_banner(d: dict, now: str) -> list[str]:
    pr = d.get("pending_review")
    if not pr:
        return ["No pending review."]
    c = pr.get("counts") or {}
    run = pr.get("run_type", "dedup")
    stage = pr.get("stage")
    return [
        f"{WARN} {run} — awaiting review (stage {stage} of 3)",
        f"    {c.get('to_delete_exact', 0)} to delete (exact) · "
        f"{c.get('groups', 0)} groups / {c.get('members', 0)} members (default-keep)",
        f"    review: {d['path']}\\_packrat_review\\",
        "    [o] open in Explorer   [g] confirm stage   [k] cancel run",
    ]


def _job_history_row(job: dict, now: str, *, selected: bool = False, width: int = 96) -> str:
    """A per-root jobs row (§3): type, status/outcome one-liner, age.

    In the per-root panel the root is dropped from the label (the header names it,
    §Job labels rule (a)). ``width`` (ref 96) lets the outcome column fill a wider
    terminal; the age stays right-aligned in the last 12 cells.
    """
    cur = CURSOR if selected else " "
    verb = job["type"]
    when = reltime(job.get("finished_at") or job.get("started_at"), now,
                   clock=_is_today(job.get("finished_at") or job.get("started_at"), now))
    if job.get("status") == "interrupted":
        note = "interrupted — re-run to resume"
    elif job.get("status") == "running":
        note = "running"
    else:
        note = _summary(job) or job.get("status", "")
    left = f"{cur}{verb:<6} {note}"
    return f"{left:<{width - 12}}{when:>12}".rstrip()


def _summary(job: dict) -> str:
    import json
    try:
        return json.loads(job.get("result_json") or "{}").get("summary", "")
    except (ValueError, TypeError):
        return ""
