"""Root detail interface (§3) — pure body builder over ``root_detail(root)``.

Renders the 3-column stats header (folder icon | counts | scan/dedup dates), a
**bordered Review section** (the pending-review case, §3.1, or the no-pending-review
line, §3.2), and a **bordered Jobs panel** laid out like the Queue interface (§4) —
three independent Running / Queued / History sections, each with its own paginator.

Both boxes are focus-able (like the dashboard boxes): the R[e]view box is focused by
``[e]`` and the Jobs panel by ``[J]``; a focused box gets the heavy accent-colored
border, while an unfocused box dims its inside key hints. Within the focused Jobs
panel, ``[r]``/``[q]``/``[h]`` pick the sub-section (Queued/History paginate). Pure
(dict → lines); the Textual screen routes the action keys to the CLI verbs.
"""

from __future__ import annotations

from .. import render
from ..data import reltime
from ..framing import box
from ..geometry import REFERENCE, Geometry
from ..layout import Cell, row
from ..tokens import CURSOR, WARN
from . import queue as q

# A 5-row packrat mascot (clutching a 📁) for the stats section's first column (§3),
# echoing the dashboard hoard logo. Plain text (no box-drawing) so the colorizer
# leaves it default and it never reads as a focused-panel heavy border; the 📁 is a
# wide (2-cell) glyph but `(>📁<)` still measures 8 cells like the other rows, and the
# CJK-aware Cell pads it to ICON_W. Padded to a fixed width by its Cell.
FOLDER_ICON = [
    "  (\\__/)",
    "  (o..o)",
    "  (>📁<)",
    "  / || \\",
    "  (____)",
]
ICON_H = len(FOLDER_ICON)   # 5 rows (was 4) — the stats block is this tall
ICON_W = 15       # column width the mascot art is padded into
COUNTS_W = 26     # the assets/photos/videos column


def detail_header_right(d: dict) -> str:
    """The top-border right label: ``<path> · <kind>``."""
    return f"{d['path']} · {d['kind']}"


REVIEW_ROWS = 4    # the Review box's interior height WHEN a review is pending (≤4 lines)


def _review_rows(d: dict) -> int:
    """The Review box's interior height: 4 for a pending review, 1 for the calm case.

    An adaptive height means the common no-pending-review root spends only one row on
    the box (leaving the Jobs History more room), while a pending review gets its full
    4-line banner."""
    return REVIEW_ROWS if d.get("pending_review") else 1


def detail_body(d: dict, *, now: str, geo: Geometry = REFERENCE,
                jobs: list[dict] | None = None,
                focus: str | None = None, job_focus: str = "history",
                cursors: dict | None = None, pages: dict | None = None) -> list[str]:
    """Build the §3 root-detail body for root ``d`` (with its ``jobs`` history).

    Top: the 3-column stats header. Then two focus-able bordered boxes — a **Review
    box** and a **Jobs panel** (Running / Queued / History). ``focus`` is the focused
    box (``None`` | ``"review"`` | ``"jobs"``); its border is heavy+accent and its
    inside key hints read normal, while the unfocused box dims its hints. ``job_focus``
    names the focused Jobs sub-section; ``cursors`` / ``pages`` hold each sub-section's
    ▸ cursor + paginator page. The Jobs panel fills the remaining vertical space."""
    jobs = jobs or []
    row_w = geo.content_w
    lines = [""]                           # breathing room above the stats/info section
    lines += _stats_columns(d, now, row_w)
    lines.append("")                       # breathing room between stats and Review
    lines += _review_box(d, geo.roots_w, focused=(focus == "review"))
    lines += _jobs_panel(d, jobs, now, geo.roots_w, _panel_interior(d, geo),
                         focused=(focus == "jobs"), job_focus=job_focus,
                         cursors=cursors or {}, pages=pages or {})
    return lines


def _panel_interior(d: dict, geo: Geometry) -> int:
    """Rows the Jobs panel's interior gets — the space left below the header block.

    Header block = 1 top spacer + stats(``ICON_H``) + 1 spacer + the Review box
    (``_review_rows`` + 2 borders); the Jobs box adds its own 2 border rows. Computed
    here (not inlined) so the screen's ↑/↓ paging budget uses the SAME interior the body
    rendered, keeping cursor math and layout in lockstep."""
    header_rows = 1 + ICON_H + 1 + (_review_rows(d) + 2)
    return max(4, geo.content_rows - header_rows - 2)


def panel_section_rows(d: dict, geo: Geometry) -> dict:
    """The Running/Queued/History window heights this frame (for the screen's paging)."""
    q_rows, h_rows = _section_budgets(_panel_interior(d, geo))
    return {"running": 1, "queued": q_rows, "history": h_rows}


def split_jobs(d: dict, jobs: list[dict]) -> dict:
    """Group the root's jobs into the three panel sections (§3 / §4 parity).

    Running + queued come from ``root_detail`` (the live view, with blocked reasons);
    History is the terminal jobs from the per-root ``jobs`` list (``root_jobs``),
    newest-first. Keyed the same as the Queue interface's sections so the shared
    :mod:`~packrat.tui.screens.queue` renderers apply unchanged."""
    running = d.get("running_job")
    queued = d.get("queued_jobs") or []
    terminal = {"queued", "running"}
    history = [j for j in jobs if j.get("status") not in terminal]
    return {"running": [running] if running else [],
            "queued": queued, "history": history}


# The Queued sub-section is deliberately SHORTER than History (item 2 — a root rarely
# has more than a couple jobs waiting on it, whereas its history is long). QUEUED_CAP
# bounds the Queued window; History gets the rest of the interior.
QUEUED_CAP = 2


def _jobs_seps(interior: int) -> int:
    """Blank separator rows drawn between the Jobs sub-sections (2 when roomy, else 0).

    The separators are cosmetic; when the panel interior is tight (a full mascot + a
    4-line pending-review box leave it small), we drop them so all THREE section headers
    stay visible rather than clipping History off the bottom (§12 "trim, don't overflow").
    Roomy = the 4 mandatory lines (3 headers + running line) + 2 separators + ≥1 row each
    window ≤ interior, i.e. interior ≥ 8."""
    return 2 if interior >= 8 else 0


def _section_budgets(interior: int) -> tuple[int, int]:
    """Split the panel interior into (queued_rows, history_rows).

    Fixed lines = running-header(1) + running-line(1) + queued-header(1) +
    history-header(1) + ``_jobs_seps`` blank separators. The two windows share whatever
    is left. History (the long list) gets priority: it takes at least half the shared
    space, and Queued is capped small (``QUEUED_CAP``). Both can shrink to 0 rows under
    extreme pressure (the headers always win, so all three sections stay visible)."""
    fixed = 4 + _jobs_seps(interior)
    share = max(0, interior - fixed)
    # History keeps ≥ half so a tight panel doesn't spend all its rows on Queued.
    queued_rows = min(QUEUED_CAP, share // 2)
    return queued_rows, share - queued_rows


def _section_state(focused: bool, job_focus: str, section: str) -> str:
    """The header casing state for a Jobs sub-section (see ``queue.header``).

    Panel unfocused → ``dim`` (grey the whole panel's sub-headers, incl. their `[k]`);
    panel focused + this is the active sub-section → ``focused`` (accent whole line);
    panel focused + another sub-section active → ``active`` (default + `[k]` accent)."""
    if not focused:
        return "dim"
    return "focused" if job_focus == section else "active"


def _jobs_panel(d: dict, jobs: list[dict], now: str, width: int, interior: int, *,
                focused: bool, job_focus: str, cursors: dict, pages: dict) -> list[str]:
    """The bordered Jobs panel — three Queue-style sections in one box (§3)."""
    sec = split_jobs(d, jobs)
    q_rows, h_rows = _section_budgets(interior)
    sep = [""] * _jobs_seps(interior)   # blank separators, dropped when the panel is tight
    iw = width - 4                      # inner text width (box borders + 1-cell pad)
    body: list[str] = []

    # -- Running (≤1; no paging) --
    run_state = _section_state(focused, job_focus, "running")
    run_focused = focused and job_focus == "running"
    body.append(q.header("[R]unning:", run_state))
    if sec["running"]:
        body.append(q.running_line(sec["running"][0], CURSOR if run_focused else " ", iw))
    else:
        body.append("  (nothing running)")
    body += sep[:1]

    # -- Queued (its pager rides the header line) --
    q_focused = focused and job_focus == "queued"
    qp = pages.get("queued", 0)
    q_pages = q.section_pages(len(sec["queued"]), q_rows)
    body.append(q.header_line("[Q]ueued:", _section_state(focused, job_focus, "queued"),
                              iw, min(qp, q_pages - 1) + 1, q_pages))
    body += q.window(sec["queued"], q_rows, qp, cursors.get("queued", 0), q_focused,
                     lambda j, c: q.queued_line(j, c, iw), empty="  (none queued)")
    body += sep[1:2]

    # -- History (newest-first terminal jobs) --
    h_focused = focused and job_focus == "history"
    hp = pages.get("history", 0)
    h_pages = q.section_pages(len(sec["history"]), h_rows)
    body.append(q.header_line("[H]istory:", _section_state(focused, job_focus, "history"),
                              iw, min(hp, h_pages - 1) + 1, h_pages))
    body += q.window(sec["history"], h_rows, hp, cursors.get("history", 0), h_focused,
                     lambda j, c: q.history_line(j, now, c, iw), empty="  (no job history)")

    # Fixed-height interior, then wrap in a heavy (accent) box when focused (§focus).
    # No maximize here, so drop the [J] key hint once focused → plain "Jobs".
    body = (body + [""] * interior)[:interior]
    title = "Jobs" if focused else "[J]obs"
    return box(title, body, width, heavy=focused)


def _is_today(ts, now) -> bool:
    return bool(ts) and (ts or "")[:10] == (now or "")[:10]


def _stats_columns(d: dict, now: str, width: int) -> list[str]:
    """The 3-column stats header (§3): mascot | counts | scan/dedup dates.

    Each of the ``ICON_H`` rows is a :func:`row` of three cells — the mascot art
    (fixed ``ICON_W``), the assets/photos/videos/size counts (fixed ``COUNTS_W``), and
    the recency dates (the grow cell). All three columns share the rows so the mascot,
    counts, and dates read as one aligned block; the counts/dates (4 entries) pad out
    to the mascot's 5 rows with blanks."""
    photos, videos = d["photos"], d["videos"]
    # Row 4 is the on-disk size (the raw file count was dropped — item 2).
    counts = [
        f"assets  {photos + videos:>9,}",
        f"  photos{photos:>9,}",
        f"  videos{videos:>9,}",
        f"size    {render.fmt_size(d.get('size_bytes')):>9}",
    ]
    dd = d.get("last_dedup_at")
    dates = [
        f"last scan   {reltime(d.get('last_scan_at'), now)}",
        f"full scan   {reltime(d.get('last_full_scan_at'), now)}",
        f"last dedup  {reltime(dd, now, clock=_is_today(dd, now))}",
        "",
    ]
    # Pad counts/dates to the mascot's height so every row has all three cells.
    counts = (counts + [""] * ICON_H)[:ICON_H]
    dates = (dates + [""] * ICON_H)[:ICON_H]
    return [
        row(width, [
            Cell(FOLDER_ICON[i], width=ICON_W),
            Cell(counts[i], width=COUNTS_W),
            Cell(dates[i], grow=1, elide="end", style="dim"),
        ], gap=2)
        for i in range(ICON_H)
    ]


def _review_box(d: dict, width: int, *, focused: bool) -> list[str]:
    """The bordered, focus-able Review section (§3.1/§3.2).

    Heavy accent border while ``focused``; its ``[o]/[g]/[k]`` action hints read
    normal when focused and **dim** (guillemet-wrapped) when not — so an out-of-focus
    Review box doesn't advertise live-looking shortcuts. With no pending review it is
    a single calm "No pending review." line."""
    rows = _review_rows(d)
    body = _review_lines(d, focused=focused)
    body = (body + [""] * rows)[:rows]
    # No double-press-to-maximize here (unlike the dashboard), so the [e] key hint is
    # only useful while UNFOCUSED; a focused box drops the brackets → plain "Review".
    title = "Review" if focused else "R[e]view"
    return box(title, body, width, heavy=focused)


def _hints(text: str, focused: bool) -> str:
    """A box's inside key-hint line — normal when focused, dim (‹…›) when not."""
    return text if focused else f"‹{text}›"


def is_stage2_dedup(pr: dict | None) -> bool:
    """True for a dedup review parked at stage 2 (recompression) — the only case where
    ``--confirm --keep-suggested`` applies (§8 B: keep each group's suggested lead).

    Stage 1/3 have no suggested leads, and cleanup-perceptual isn't banded into stages,
    so neither offers the bulk keep-suggested action."""
    return bool(pr and pr.get("run_type") == "dedup" and pr.get("stage") == 2)


def _review_lines(d: dict, *, focused: bool) -> list[str]:
    pr = d.get("pending_review")
    if not pr:
        return ["No pending review."]
    c = pr.get("counts") or {}
    run = pr.get("run_type", "dedup")
    stage = pr.get("stage")
    # Stage-2 dedup adds the bulk "[b] keep suggested" action (§8 B --keep-suggested):
    # keep each group's suggested lead, ignore shortcut edits. Only shown when it applies.
    if is_stage2_dedup(pr):
        hint = ("[o] open in Explorer   [g] confirm stage   "
                "[b] confirm · keep suggested   [k] cancel run")
    else:
        hint = "[o] open in Explorer   [g] confirm stage   [k] cancel run"
    # The counts dict is SHAPED BY run_type (queries._review_counts): dedup carries
    # {to_delete_exact, groups, members}; cleanup-perceptual carries {exact, perceptual}.
    # Branch like the CLI's _review_count_summary — reading dedup keys for a cleanup run
    # rendered a false "0 to delete · 0 groups / 0 members" (the whole staged set as zero).
    if run == "cleanup-perceptual":
        header = f"{WARN} cleanup — awaiting review (perceptual)"
        counts = (f"  {c.get('exact', 0)} exact-trash (will delete) · "
                  f"{c.get('perceptual', 0)} perceptual candidate(s) (delete-default)")
    else:
        header = f"{WARN} {run} — awaiting review (stage {stage} of 3)"
        counts = (f"  {c.get('to_delete_exact', 0)} to delete (exact) · "
                  f"{c.get('groups', 0)} groups / {c.get('members', 0)} members (default-keep)")
    return [
        header,
        counts,
        f"  review: {d['path']}\\_packrat_review\\",
        _hints(hint, focused),
    ]
