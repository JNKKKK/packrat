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
from ..tokens import CROSS, CW

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


def card_body(job: dict, *, now: str) -> list[str]:
    """Build the card body — dispatch on status first, then op (§5)."""
    status = job.get("status")
    if status == "running":
        return _running_body(job)
    if status in ("error", "interrupted"):
        return _terminal_error_body(job)
    op = _result(job).get("op") or job["type"]
    builder = {
        "scan": _scan_body,
        "merge": _merge_body,
        "dedup": _dedup_body,
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
def _scan_body(job: dict) -> list[str]:
    r = _result(job)
    return [
        f"scan  {job.get('root_name') or ''}",
        RULE,
        f"{r.get('new', 0):>5,}  new assets       {r.get('exact_dup', 0):>5,}  exact-dup instances",
        f"{r.get('backfilled', 0):>5,}  filled-in fp     {r.get('matches_trashed', 0):>5,}  identified as trash",
        f"{r.get('undecodable', 0):>5,}  undecodable      {r.get('read_errors', 0):>5,}  read errors",
        f"{r.get('skipped_fastpath', 0):>5,}  skipped (fast)   {r.get('deleted_instances', 0):>5,}  instances gone",
        "",
        r.get("summary", ""),
    ]


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


def _oneliner_body(job: dict) -> list[str]:
    r = _result(job)
    return [r.get("summary", job.get("status", "done"))]


def _terminal_error_body(job: dict) -> list[str]:
    """error / interrupted — result_json may be NULL → render from status + error."""
    if job.get("status") == "interrupted":
        return [f"{job.get('label', job['type'])} — interrupted; progress safe, re-run to resume."]
    err = job.get("error") or "failed"
    return [f"{CROSS} {err}", "", "(result_json NULL on error → shown from status + jobs.error)"]
