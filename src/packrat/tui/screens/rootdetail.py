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
from ..data import reltime, same_day
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


#: Overhead rows in the §3 body OUTSIDE the two box interiors: 1 top spacer + ICON_H
#: stats + 1 spacer + Review box borders (2) + Jobs box borders (2). The Review and Jobs
#: INTERIORS share whatever is left (``content_rows − _DETAIL_OVERHEAD``).
_DETAIL_OVERHEAD = 1 + ICON_H + 1 + 2 + 2   # ref: 11
#: Rows the Jobs panel must keep no matter how tall the Review box wants to be — its 3
#: section headers (Running/Queued/History) + the running line. The Review cap yields to
#: this so Jobs never collapses below a usable height (unreachable given the ≥24-row
#: terminal invariant + the 1:1 cap, but a defensive floor the old code had as `max(4,…)`).
_JOBS_MIN_ROWS = 4


def _review_text_w(geo: Geometry) -> int:
    """Usable text width inside the Review box (box border + 1-cell padding each side)."""
    return geo.roots_w - 4


def review_content_lines(d: dict, geo: Geometry = REFERENCE, *, focused: bool = False) -> list[str]:
    """The FULL (unclamped) Review body lines — the height cap/scroll window over these.

    Pure ``dict → lines``; width-aware so the stage-2 columns+histogram lay out to the box.
    A calm (no-pending-review) root is a single line."""
    if not d.get("pending_review"):
        return ["No pending review."]
    return _review_lines(d, geo, focused=focused)


def _split_rows(content_len: int, geo: Geometry) -> tuple[int, int]:
    """Split the shared interior into ``(review_interior, jobs_interior)`` from a review
    content LINE COUNT (§3) — pure arithmetic, no dict, so callers compute the content
    once and reuse it (the render path threads the same list into the box).

    Review is capped at review:jobs ≤ 1:1 (``S // 2``, odd row → Jobs so History keeps
    priority) and SHRINKS to its content when shorter; Jobs backfills the freed rows but
    never drops below :data:`_JOBS_MIN_ROWS` (the cap yields to that floor)."""
    s = max(2, geo.content_rows - _DETAIL_OVERHEAD)     # ref: 21 − 11 = 10
    cap = max(1, s // 2)                                 # 1:1 max; ref 5
    review = min(content_len, cap, max(0, s - _JOBS_MIN_ROWS))
    return review, s - review


def _detail_split(d: dict, geo: Geometry, *, focused: bool = False) -> tuple[int, int]:
    """``(review_interior, jobs_interior)`` for root ``d`` — see :func:`_split_rows`.

    ``focused`` must match how the box is rendered so the cap and the rendered content
    are derived from the SAME line list (no desync)."""
    return _split_rows(len(review_content_lines(d, geo, focused=focused)), geo)


def _review_rows(d: dict, geo: Geometry = REFERENCE, *, focused: bool = False) -> int:
    """The Review box's interior height this frame (the responsive cap, §3)."""
    return _detail_split(d, geo, focused=focused)[0]


def detail_body(d: dict, *, now: str, geo: Geometry = REFERENCE,
                jobs: list[dict] | None = None,
                focus: str | None = None, job_focus: str = "history",
                cursors: dict | None = None, pages: dict | None = None,
                review_scroll: int = 0) -> list[str]:
    """Build the §3 root-detail body for root ``d`` (with its ``jobs`` history).

    Top: the 3-column stats header. Then two focus-able bordered boxes — a **Review
    box** and a **Jobs panel** (Running / Queued / History). ``focus`` is the focused
    box (``None`` | ``"review"`` | ``"jobs"``); its border is heavy+accent and its
    inside key hints read normal, while the unfocused box dims its hints. ``job_focus``
    names the focused Jobs sub-section; ``cursors`` / ``pages`` hold each sub-section's
    ▸ cursor + paginator page. The Jobs panel fills the remaining vertical space."""
    jobs = jobs or []
    row_w = geo.content_w
    # Compute the Review content ONCE (with the real focused flag) and derive both box
    # heights from its length — so the cap, the scroll window, and the Jobs height are all
    # in lockstep off a single build (no recompute, no focused-flag desync).
    review_focused = focus == "review"
    content = review_content_lines(d, geo, focused=review_focused)
    review_h, jobs_h = _split_rows(len(content), geo)
    lines = [""]                           # breathing room above the stats/info section
    lines += _stats_columns(d, now, row_w)
    lines.append("")                       # breathing room between stats and Review
    lines += _review_box(content, review_h, geo.roots_w, focused=review_focused,
                         scroll=review_scroll)
    lines += _jobs_panel(d, jobs, now, geo.roots_w, jobs_h,
                         focused=(focus == "jobs"), job_focus=job_focus,
                         cursors=cursors or {}, pages=pages or {})
    return lines


def _panel_interior(d: dict, geo: Geometry) -> int:
    """Rows the Jobs panel's interior gets — the Jobs half of :func:`_detail_split`.

    Kept as its own accessor (not inlined) so the screen's ↑/↓ paging budget uses the SAME
    interior the body rendered, keeping cursor math and layout in lockstep."""
    return _detail_split(d, geo)[1]


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
        f"last dedup  {reltime(dd, now, clock=same_day(dd, now))}",
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


def review_scroll_max(d: dict, geo: Geometry = REFERENCE, *, focused: bool = False) -> int:
    """Max ↑/↓ scroll offset for the Review box this frame (0 when it all fits).

    Derives BOTH the content length and the cap from the same focused build (via
    :func:`_detail_split`), so the scroll bound matches the rendered window exactly."""
    content = review_content_lines(d, geo, focused=focused)
    review_h, _ = _split_rows(len(content), geo)
    return max(0, len(content) - review_h)


def _review_box(content: list[str], cap: int, width: int, *, focused: bool,
                scroll: int = 0) -> list[str]:
    """The bordered, focus-able Review section (§3.1/§3.2).

    ``content`` is the full (unclamped) review lines and ``cap`` its interior height (both
    from :func:`detail_body`'s single build, review:jobs ≤ 1:1). When content exceeds the
    cap, ↑/↓ scroll a window and the title carries a right-aligned ``↑/↓ start–end of n``
    indicator (the scan-card idiom). Heavy accent border while ``focused``."""
    n = len(content)
    start = max(0, min(scroll, max(0, n - cap)))
    window = content[start:start + cap]
    window = (window + [""] * cap)[:cap]
    # No double-press-to-maximize here (unlike the dashboard), so the [e] key hint is
    # only useful while UNFOCUSED; a focused box drops the brackets → plain "Review".
    title = "Review" if focused else "R[e]view"
    right = f"↑/↓ {start + 1}–{start + min(cap, n - start)} of {n}" if n > cap else ""
    return box(title, window, width, heavy=focused, right=right)


def _hints(text: str, focused: bool) -> str:
    """A box's inside key-hint line — normal when focused, dim (‹…›) when not."""
    return text if focused else f"‹{text}›"


def is_stage2_dedup(pr: dict | None) -> bool:
    """True for a dedup review parked at stage 2 (recompression) — the only case where
    ``--confirm --keep-suggested`` applies (§8 B: keep each group's suggested lead).

    Stage 1/3 have no suggested leads, and cleanup-perceptual isn't banded into stages,
    so neither offers the bulk keep-suggested action."""
    return bool(pr and pr.get("run_type") == "dedup" and pr.get("stage") == 2)


def _review_lines(d: dict, geo: Geometry = REFERENCE, *, focused: bool) -> list[str]:
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
        detail = [(f"  {c.get('exact', 0)} exact-trash (will delete) · "
                   f"{c.get('perceptual', 0)} perceptual candidate(s) (delete-default)")]
    elif stage == 2 and c.get("stage2"):
        # Rich stage-2 breakdown (keep-lead columns + PDQ histogram + make-up + suggestion
        # split), built by the SHARED review_stats line-builder the CLI log also uses.
        from ...review_stats import stage2_lines
        header = f"{WARN} {run} — awaiting review (stage 2 of 3)"
        detail = [f"  {ln}" for ln in stage2_lines(c["stage2"], _review_text_w(geo) - 2)]
    elif stage == 1 and c.get("stage1"):
        from ...review_stats import stage1_lines
        header = f"{WARN} {run} — awaiting review (stage 1 of 3)"
        detail = stage1_lines(c["stage1"])
    else:
        header = f"{WARN} {run} — awaiting review (stage {stage} of 3)"
        detail = [(f"  {c.get('to_delete_exact', 0)} to delete (exact) · "
                   f"{c.get('groups', 0)} groups / {c.get('members', 0)} members (default-keep)")]
    return [
        header,
        *detail,
        f"  review: {d['path']}\\_packrat_review\\",
        _hints(hint, focused),
    ]
