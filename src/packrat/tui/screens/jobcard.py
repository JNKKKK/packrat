"""Job result / detail card (§5) — pure body builders keyed off status then op.

The renderer keys off ``status`` first, ``op`` second (the §5 "every job show-able"
contract): a **running** job has no ``result_json`` yet → renders live from the SSE
stream (progress bar + counts); a **terminal** job switches on ``result_json.op``
for its outcome tallies, falling back to ``status`` + ``error`` when ``result_json``
is NULL (error/interrupted). All pure (job dict → lines).
"""

from __future__ import annotations

import json

from .. import render
from ..geometry import REFERENCE, Geometry
from ..layout import Cell, row
from ..tokens import CROSS, CW, WARN

RULE = "─" * (CW - 4)


def _result(job: dict) -> dict:
    try:
        return json.loads(job.get("result_json") or "{}")
    except (ValueError, TypeError):
        return {}


def card_title(job: dict) -> str:
    """The card's top-border title: ``Job #418 · scan iPhone · running``."""
    label = job.get("label", job["type"])
    return f"packrat · Job #{job['id']} · {label} · {_status_word(job)}"


def _status_word(job: dict) -> str:
    st = job.get("status")
    if st == "running":
        return "running"
    if job.get("status") == "done" and _result(job).get("review_status") == "pending":
        return "⚠ awaiting review"
    return st or "done"


def card_body(job: dict, *, now: str, problem_files: list[dict] | None = None,
              problems_scroll: int = 0, geo: Geometry = REFERENCE) -> list[str]:
    """Build the card body — dispatch on status first, then op (§5).

    A **scan** card also lists its undecodable/read-error ``problem_files`` (paths +
    reasons) in a fixed-height, ``↑/↓``-scrollable section below the count summary;
    ``problems_scroll`` is the line offset the screen advances with the arrows."""
    status = job.get("status")
    if status == "running":
        return _running_body(job)
    if status in ("error", "interrupted"):
        return _terminal_error_body(job)
    op = _result(job).get("op") or job["type"]
    if op == "scan":
        return _scan_body(job, problem_files or [], problems_scroll, geo)
    builder = {
        "merge": _merge_body,
        "dedup": _dedup_body,
        "cleanup": _cleanup_body,
        "trash-refresh": _oneliner_body,
        "untrash": _oneliner_body,
    }.get(op, _oneliner_body)
    return builder(job)


# --- running (§5.1) — live from SSE ---------------------------------------
def _running_body(job: dict) -> list[str]:
    bar = render.progress_bar(job.get("done"), job.get("total"),
                             width=30, eta_s=job.get("_eta_s"), running=True)
    return [
        f"{job['type']}  {job.get('root_name') or ''}",
        RULE,
        bar,
        "",
        "  ‹live — refreshes as the job runs; auto-shows the result card on completion›",
    ]


# --- terminal bodies -------------------------------------------------------
def _scan_body(job: dict, problem_files: list[dict], scroll: int,
               geo: Geometry) -> list[str]:
    r = _result(job)
    lines = [
        f"scan  {job.get('root_name') or ''}",
        RULE,
        f"{r.get('new', 0):>5,}  new assets       {r.get('exact_dup', 0):>5,}  exact-dup instances",
        f"{r.get('backfilled', 0):>5,}  filled-in fp     {r.get('matches_trashed', 0):>5,}  identified as trash",
        f"{r.get('undecodable', 0):>5,}  undecodable      {r.get('read_errors', 0):>5,}  read errors",
        f"{r.get('skipped_fastpath', 0):>5,}  skipped (fast)   {r.get('deleted_instances', 0):>5,}  instances gone",
        "",
        r.get("summary", ""),
    ]
    if problem_files:
        lines += _problem_section(problem_files, scroll, geo)
    return lines


def problem_budget(job: dict, problem_files: list[dict], geo: Geometry) -> int:
    """Rows the scrollable problem-file window gets (drives the screen's ↑/↓ clamp).

    The card's fixed portion is the count summary (8 lines) + the problem-section
    header (1); the rest of the content region is the scrollable window. Mirrors the
    budget :func:`_problem_section` fits into, so the screen clamps scroll to the
    same number of rows the body actually shows."""
    if not problem_files:
        return 0
    return max(1, geo.content_rows - 9)


def _problem_section(problem_files: list[dict], scroll: int,
                     geo: Geometry) -> list[str]:
    """The scrollable ``[undecodable]/[read-error] <path> <reason>`` list (§12 card).

    A fixed-height window into ``problem_files`` starting at line ``scroll`` (the
    ↑/↓ offset), with a header showing the count + the visible range so scrolling is
    legible. Each row middle-elides its path and dims its reason to the right."""
    w = geo.content_w
    n = len(problem_files)
    budget = max(1, geo.content_rows - 9)
    start = max(0, min(scroll, max(0, n - budget)))
    window = problem_files[start:start + budget]
    rows = [_problem_row(pf, w) for pf in window]
    rows += [""] * (budget - len(rows))
    end = start + len(window)
    if n > budget:
        posn = f"↑/↓  {start + 1}–{end} of {n}"
    else:
        posn = f"{n} file{'s' if n != 1 else ''}"
    header = row(w, [Cell(f"problem files ({n}):", grow=1),
                     Cell(posn, align="right")], gap=2)
    return [header] + rows


def _problem_row(pf: dict, width: int) -> str:
    """One problem-file line: a dim ``[tag]``, the middle-elided path, then the reason."""
    problem = pf.get("problem", "undecodable")
    tag = "‹undecodable›" if problem == "undecodable" else "‹read-error›"
    reason = pf.get("detail") or (
        "decoder rejected the pixels" if problem == "undecodable" else "file unreadable"
    )
    return row(width, [
        Cell(WARN, width=1, style="warn"),
        Cell(tag, width=13, style="dim"),
        Cell(pf.get("path", ""), grow=2, elide="middle"),
        Cell(reason, grow=3, elide="end", style="dim"),
    ], gap=1).rstrip()


def _merge_body(job: dict) -> list[str]:
    r = _result(job)
    return [
        f"merge  {r.get('source', '')}  →  {r.get('dest_root', '')}",
        RULE,
        f"{r.get('new', 0):>5,}  copied (new)",
        f"{r.get('exact_known', 0):>5,}  skipped — exact-known (already in collection)",
        f"{r.get('trashed', 0):>5,}  skipped — trashed (matched trash memory)",
        f"{r.get('dup_in_source', 0):>5,}  skipped — dup-in-source (byte-identical siblings)",
        f"{r.get('collisions', 0):>5,}  collisions renamed    {r.get('errors', 0):>3,}  errors",
        "",
        r.get("summary", ""),
    ]


def _dedup_body(job: dict) -> list[str]:
    r = _result(job)
    if r.get("review_status") == "pending":
        return [
            f"dedup  {job.get('root_name') or ''}   stage {r.get('stage', '?')}",
            RULE,
            f"{render.RUNNING} staged · {r.get('groups', 0)} groups / {r.get('members', 0)} members (KEEP)",
            f"  {r.get('to_delete_exact', 0)} exact to delete",
            "",
            "[o] open review folder   [g] confirm this stage   [k] cancel run",
        ]
    return [
        r.get("summary", "done"),
        RULE,
        f"Audit: %APPDATA%\\packrat\\audit\\dedup\\{job.get('root_name', '')}\\{job['id']}\\",
        "       (proposed.json / applied.json)",
    ]


def _cleanup_body(job: dict) -> list[str]:
    r = _result(job)
    # A paused `cleanup --trash-perceptual` analyze emits review_status='pending' (like
    # dedup) — render the awaiting-review card with the [o]/[g]/[k] actions the footer
    # advertises, not a bare one-liner.
    if r.get("review_status") == "pending":
        return [
            f"cleanup  {job.get('root_name') or ''}   perceptual",
            RULE,
            f"{render.RUNNING} staged · {r.get('members', 0)} perceptual candidate(s) (delete-default)",
            f"  {r.get('to_delete_exact', 0)} exact-trash to delete",
            "",
            "[o] open review folder   [g] confirm & delete   [k] cancel run",
        ]
    return _oneliner_body(job)


def _oneliner_body(job: dict) -> list[str]:
    r = _result(job)
    return [r.get("summary", job.get("status", "done"))]


def _terminal_error_body(job: dict) -> list[str]:
    """error / interrupted — result_json may be NULL → render from status + error."""
    if job.get("status") == "interrupted":
        return [f"{job.get('label', job['type'])} — interrupted; progress safe, re-run to resume."]
    err = job.get("error") or "failed"
    return [f"{CROSS} {err}"]
