"""Pure content renderers — read-model dict → content line(s).

Each function turns a query-shaped dict (see :mod:`packrat.queries`) into the
plain-text line(s) for one row/panel, built from :mod:`packrat.tui.layout` cells /
:mod:`packrat.tui.tokens` glyphs. Keeping them **pure** (dict → line, colorless)
makes the frame tests cheap string assertions and lets the Textual widgets stay
thin — a widget delegates here for its text, then colors spans by
:class:`~packrat.tui.layout.Cell` role and owns only focus/keys/liveness.

Rows use ``grow`` cells so a flexible column (the path/label) absorbs slack and the
trailing columns (dot/count/status) stay right-aligned as the frame widens.
"""

from __future__ import annotations

from . import tokens
from .data import fmt_eta, reltime
from .layout import Cell, middle_elide, row
from .tokens import BAR_EMPTY, BAR_FILL, CURSOR, RUNNING


# --- Logo (§1) -------------------------------------------------------------
# The gem the packrat clutches, cycled by the dashboard's animation timer. Each is a
# single terminal cell (verified), so swapping between them never shifts the layout —
# the plain frame stays width-stable whatever gem is drawn. `LOGO_GEMS[0]` (◆) is the
# deterministic default the pure builders / golden tests render.
LOGO_GEMS = ("◆", "◇", "◈")


# The "Packrat" ASCII wordmark that sits beside the mascot's upper body (3 lines,
# each ≤ the logo width). Plain ASCII (pipes/parens/underscores, NOT box-drawing
# glyphs) so the colorizer leaves it default and it never reads as a panel border.
LOGO_WORDMARK = (
    "  _",
    " |_) _.  _ |  ._ _. _|_",
    " |  (_| (_ |< | (_|  |_",
)


def logo_lines(assets: int, *, gem: str = LOGO_GEMS[0],
               rows: int | None = None, width: int | None = None) -> list[str]:
    """The packrat mascot + "Packrat" wordmark + tagline + live "· N assets ·" line (§1).

    The 3-line :data:`LOGO_WORDMARK` sits in the top three rows of the text column
    (the first row has no mascot beside it); a blank line then separates it from the
    tagline + live hoard count, which sit beside the mascot's legs/base. ``gem`` is the
    glyph the mascot holds (``(>◆◆<)``) — the dashboard cycles it across
    :data:`LOGO_GEMS` for the hoard animation; it defaults to ◆ so the pure builder is
    deterministic (golden tests). ``rows``/``width`` pad the block to a fixed size so it
    sits beside the Collection box in the dashboard top section (same height). All lines
    are 1-cell-per-char (the gem included), so ``len``-based padding == cell width."""
    w0, w1, w2 = LOGO_WORDMARK
    pad = " " * 11         # indent to the wordmark column (2 + 6-cell mascot + 3-space gap)
    lines = [
        f"{pad}{w0}",                          # wordmark top — no mascot on this row
        f"  (\\__/)   {w1}",
        f"  (o..o)   {w2}",
        f"  (>{gem}{gem}<)",                    # gem row; text column blank → the spacer line
        '  / || \\    "hoards everything, keeps a system"',
        f"  (____)    · {assets:,} assets hoarded ·",
    ]
    if rows is not None:
        lines = (lines + [""] * rows)[:rows]
    if width is not None:
        lines = [ln[:width].ljust(width) for ln in lines]
    return lines


# --- size formatting -------------------------------------------------------
def fmt_size(n: int | None) -> str:
    """Human-readable byte size (``12.4 GB`` / ``840 MB`` / ``0 B``) for the UI.

    Base-1024 units; ≥10 in a unit shows no decimal (``124 GB``), <10 shows one
    (``4.2 GB``) so the field stays compact. ``None``/0 → ``0 B``."""
    f = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            if unit == "B" or f >= 10:
                return f"{f:.0f} {unit}"
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.0f} TB"


# --- CollectionBox (§1) ----------------------------------------------------
# Inner content width of the Collection box (its outer width minus the box's 4-cell
# border+padding), used to right-align each stat's value against the box's right edge.
COLLECTION_INNER_W = tokens.COLLECTION_W - 4       # 25 at the reference size


def collection_lines(snap: dict, *, now: str, last_scan_label: str | None = None,
                     width: int = COLLECTION_INNER_W) -> list[str]:
    """Collection stats body (§1): assets/photo-video split, size, trashed, deduped.

    ``last_scan_label`` is accepted for call compatibility but no longer shown. The
    ``Size`` line is the full collection's total on-disk bytes (``snap['size_bytes']``);
    the last line is the lifetime-deduped total (files removed across all dedup runs).
    Each value is **right-aligned to ``width``** (the box's inner width) so every number's
    right edge sits flush against the box's right border.
    """
    def _stat(label: str, value) -> str:
        v = f"{value:,}" if isinstance(value, int) else str(value)
        # label left, value right-justified to fill the rest → value hugs the right edge.
        return f"{label}{v:>{max(1, width - len(label))}}"
    return [
        _stat("Assets", snap["assets"]),
        _stat("  photos", snap["photos"]),
        _stat("  videos", snap["videos"]),
        _stat("Size", fmt_size(snap.get("size_bytes"))),
        _stat("Trashed", snap["trashed"]),
        _stat("Deduped", snap.get("lifetime_deduped", 0)),
    ]


# --- StatusDot / RootRow (§1, §2) -----------------------------------------
# The root NAME column width, shared by both row renderers (dashboard Roots box +
# the maximized Roots interface). The name is a root's primary human handle (§8 A1),
# so it gets the generous fixed space and the path — a grow cell — absorbs whatever
# is left; widening this directly trades path width for name width.
NAME_W = 24


def root_dot(r: dict) -> str:
    """The ◉/◐/○ (or blank for trash) freshness dot for a root row."""
    return tokens.status_dot(r["kind"], r.get("last_scan_at"), r.get("last_dedup_at"))


def root_row_compact(r: dict, *, selected: bool = False, width: int = 62,
                     path_w: int = 20) -> str:
    """A dashboard/focused-box root row (§1): ``▸ Name  path……………  ◐   count``.

    The path is a **grow** cell that absorbs the middle, so the dot + count are
    pushed to the right end (most space goes to the path). ``path_w`` is kept as a
    minimum floor; ``width`` is the full row width. Trash roots show ``(trash)`` in
    the count column and no dot.
    """
    cur = CURSOR if selected else " "
    dot = root_dot(r)
    count = "(trash)" if r["kind"] == "trash" else f"{r['asset_count']:,}"
    size = "—" if r["kind"] == "trash" else fmt_size(r.get("size_bytes"))
    return row(
        width,
        [
            Cell(cur, width=1, style="highlighted" if selected else None),
            Cell(r["name"], width=NAME_W),
            Cell(r["path"], grow=1, elide="middle"),   # absorbs the middle
            Cell(dot, width=1, style=_dot_style(dot)),
            Cell(count, width=7, align="right", style="dim" if r["kind"] == "trash" else None),
            Cell(size, width=9, align="right", style="dim"),   # total size on disk
        ],
    ).rstrip()


def root_row_wide(r: dict, *, now: str, selected: bool = False, path_w: int = 20,
                  width: int = 96) -> str:
    r"""A maximized Roots-interface row (§2.1): dot, count, recency all right-aligned.

    ``▸ Downloads  D:\dump…………………  ◐     241  never deduped`` — the path is a
    **grow** cell absorbing the middle, so the dot / count / recency columns sit at
    the right end. ``width`` is the full row width; ``path_w`` is unused (kept for
    call compatibility). Trash roots show ``(trash)`` for the count and ``—`` recency.
    """
    cur = CURSOR if selected else " "
    dot = root_dot(r)
    size = "—" if r["kind"] == "trash" else fmt_size(r.get("size_bytes"))
    if r["kind"] == "trash":
        count, recency = "(trash)", "—"
        dot_cell = Cell(" ", width=1)
        count_cell = Cell(count, width=8, align="right", style="dim")
    else:
        count = f"{r['asset_count']:,}"
        dd = r.get("last_dedup_at")
        if not dd:
            recency = "never deduped"
        elif _is_today(dd, now):
            recency = "deduped today"
        else:
            recency = f"deduped {reltime(dd, now)}"
        dot_cell = Cell(dot, width=1, style=_dot_style(dot))
        count_cell = Cell(count, width=8, align="right")
    return row(
        width,
        [
            Cell(cur, width=1, style="highlighted" if selected else None),
            Cell(r["name"], width=NAME_W),
            Cell(r["path"], grow=1, elide="middle"),    # absorbs the middle
            dot_cell,
            count_cell,
            Cell(size, width=9, align="right", style="dim"),   # total size on disk
            Cell(recency, width=15, align="right", style="dim"),
        ],
    ).rstrip()


def _is_today(ts: str, now: str) -> bool:
    return (ts or "")[:10] == (now or "")[:10]


def _dot_style(dot: str) -> str | None:
    if dot == tokens.DOT_DEDUPED:
        return "success"
    if dot == tokens.DOT_SCANNED:
        return "warn"
    return None


# --- [s] sort cycle (§2 Roots interface) -----------------------------------
# The fixed cycle (§2 notes), wrapping back to the first. All display-side over
# roots_snapshot() (which stays id-ascending for CLI parity — Open Q#1). Each
# entry: (header label, key function, reverse).
SORT_CYCLE = [
    ("most recent registered", lambda r: r["id"], True),      # id DESC (default)
    ("most assets", lambda r: r["asset_count"], True),
    ("most photos", lambda r: r["photos"], True),
    ("most videos", lambda r: r["videos"], True),
]


def sort_roots(roots: list[dict], mode: int) -> list[dict]:
    """Return ``roots`` reordered per the ``[s]`` sort cycle position ``mode`` (mod 4).

    Stable sort so ties keep the snapshot's (registration) order — deterministic
    for golden tests. Trash roots sort with the rest by the chosen key.
    """
    _, key, reverse = SORT_CYCLE[mode % len(SORT_CYCLE)]
    return sorted(roots, key=key, reverse=reverse)


def sort_header(mode: int) -> str:
    """The Roots-interface header line for sort ``mode`` (§2.1)."""
    label = SORT_CYCLE[mode % len(SORT_CYCLE)][0]
    return f"[S]ort: {label}  (→ most assets → photos → videos)"


# --- ProgressBar (§1.4/§4/§5.1) -------------------------------------------
def progress_bar(done: int | None, total: int | None, *, width: int = 14,
                 eta_s: float | None = None, running: bool = True) -> str:
    """An inline ``███░░░ 67% 8,912/13,204 ETA 4m`` bar (§ProgressBar).

    ``width`` is the bar-cell count. ETA is passed in (TUI-derived, §cross-cutting);
    blank until derivable. A non-running bar omits the ▶ marker.
    """
    done = done or 0
    marker = f"{RUNNING} " if running else ""
    if not total:
        return f"{marker}{done:,}"
    frac = min(1.0, done / total)
    filled = int(frac * width)
    bar = BAR_FILL * filled + BAR_EMPTY * (width - filled)
    eta = fmt_eta(eta_s)
    tail = f" {eta}" if eta else ""
    return f"{marker}{bar}  {int(frac * 100):d}% {done:,}/{total:,}{tail}"


# --- JobRow (§1.4/§4) ------------------------------------------------------
def blocked_short(job: dict) -> str | None:
    """A compact ``blocked: <root> pending <run>`` note, or None if runnable/not queued.

    The daemon's holder ``what`` is verbose (``"dedup pending since <ts>"``); the
    compact list rows (§1.2/§4) show the short form ``blocked: <root> pending
    <run_type>``. ``run_type`` is pulled from the holder when present.
    """
    holder = job.get("blocked")
    if not holder:
        return None
    root = job.get("root_name") or "root"
    run = holder.get("run_type")
    if run:
        run = run.replace("cleanup-perceptual", "cleanup")
        return f"blocked: {root} pending {run}"
    return f"blocked: {holder.get('what', 'held')}"


def job_status_note(job: dict) -> str:
    """The right-hand status/blocked note for a queued/terminal job row.

    ``queued · waiting for worker`` / ``blocked: <root> pending <run>`` / a terminal
    status.
    """
    status = job.get("status")
    if status == "queued":
        return blocked_short(job) or "queued · waiting for worker"
    return status or ""


def queue_row(job: dict, *, selected: bool = False, show_id: bool = True,
              index: int | None = None, width: int = 94) -> str:
    """A queue-panel row (§1.4/§4): running → live bar; queued → label + reason.

    The running row carries the ▶ marker itself, so its bar renders ``running=False``
    (no second marker). Queued rows show a leading identifier — the positional
    ``index`` in the dashboard preview (``2 merge …``), or the job id in the
    maximized queue (``#419 …``) when ``show_id``. ``width`` (ref 94) lets the
    label/note row fill a wider terminal.
    """
    cur = CURSOR if selected else " "
    if job.get("status") == "running":
        bar = progress_bar(job.get("done"), job.get("total"),
                           eta_s=job.get("_eta_s"), running=False)
        # label grows to absorb the middle; the ███░░░ bar sits at the right end, so it
        # stays right-aligned as the terminal widens (matches queue.running_line — the
        # ▶ marker is on the label side, so the bar renders without a second marker).
        left = f"{cur}{RUNNING} {job.get('label', job['type'])}"
        return row(
            width,
            [Cell(left, grow=1, elide="end"),
             Cell(bar, align="right", style="running")],
            gap=2,
        ).rstrip()
    if index is not None:
        ident = f"{index} "
    elif show_id:
        ident = f"#{job['id']} "
    else:
        ident = ""
    label = job.get("label", job["type"])
    note = job_status_note(job)
    left = f"{cur}{ident}{label}"
    # label grows to absorb the middle; the status/blocked note sits at the right.
    return row(
        width,
        [Cell(left, grow=1, elide="end"), Cell(note, align="right", style="dim")],
        gap=2,
    ).rstrip()
