"""The QueueMax screen (M6, §12) — see :mod:`packrat.tui.frames.base`."""

from __future__ import annotations

from textual import work
from textual.binding import Binding

from .. import demo
from ..framing import screen
from ..layout import wrap_hints
from ..screens.queue import queue_body
from ..screens.queue import section_jobs as q_section_jobs
from ..screens.queue import section_pages as q_section_pages

from .base import FrameScreen
from .jobcard import JobCard


# ---------------------------------------------------------------------------
# Queue interface (§4)
# ---------------------------------------------------------------------------
class QueueMax(FrameScreen):
    """§4 with per-section focus: [r]unning / [q]ueued / rec[e]nt.

    ↑/↓ and ←/→ act on the FOCUSED section only; each section keeps its own cursor
    and page, so paging one never touches another (the three are independent
    fixed-height windows). A section-letter key focuses that section.
    """

    BINDINGS = [
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("left", "page(-1)", show=False),
        Binding("right", "page(1)", show=False),
        Binding("r", "focus_section('running')", show=False),
        Binding("q", "focus_section('queued')", show=False),
        Binding("h", "focus_section('history')", show=False),
        Binding("enter", "detail", show=False),
        Binding("c", "cancel", show=False),
        Binding("p", "prioritize", show=False),
        Binding("x", "cancel_all", show=False),
        Binding("escape", "app.pop_screen", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.focus = "running"      # focused section: running|queued|history (§4 default)
        self.cursors = {"running": 0, "queued": 0, "history": 0}
        self.pages = {"running": 0, "queued": 0, "history": 0}
        # History is LAZY-LOADED one page at a time (§12): running/queued come from the
        # snapshot, but the terminal-job history is unbounded, so we hold only the current
        # page's rows + the true total-page count (for the paginator). Fetched on mount +
        # poll + page change, off the UI thread.
        self._history: list[dict] = []
        self._history_total = 0     # total terminal jobs (denominator for page K/N)

    def on_mount(self) -> None:
        self._reload_history()
        super().on_mount()

    # -- history page size / fetch ----------------------------------------
    def _history_rows(self) -> int:
        """The History window height this frame — the page size we fetch."""
        return max(1, self._geo.recent_rows)

    def _history_pages(self) -> int:
        rows = self._history_rows()
        return max(1, -(-self._history_total // rows)) if rows else 1

    def poll_reload(self) -> None:
        """Poll refresh — re-fetch the CURRENT history page so a just-finished job appears
        (the live running/queued sections come from the snapshot, refreshed centrally)."""
        self._reload_history()

    def _reload_history(self) -> None:
        """Fetch the current history page (limit=window, offset=page·window) off the UI
        thread. Offline / no app loop applies inline (demo data / unit tests)."""
        rows = self._history_rows()
        offset = self.pages["history"] * rows
        if self.app.offline:
            full = demo.recent_jobs()
            terminal = {"queued", "running"}
            hist = [j for j in full if j.get("status") not in terminal]
            self._history_total = len(hist)
            self._history = hist[offset:offset + rows]
            return
        if not self.app._app_loop_running():
            self._history, self._history_total = self._fetch_history_page(rows, offset)
            return
        self._fetch_history_async(rows, offset)

    def _fetch_history_page(self, limit: int, offset: int) -> tuple[list[dict], int]:
        try:
            return self.app.client.history_page(limit=limit, offset=offset)
        except Exception:  # noqa: BLE001 - degrade to empty, header already shows down
            return [], 0

    @work(thread=True, exclusive=True, group="queue-history")
    def _fetch_history_async(self, limit: int, offset: int) -> None:
        hist, total = self._fetch_history_page(limit, offset)
        try:
            self.app.call_from_thread(self._apply_history, hist, total)
        except Exception:
            pass   # screen/app tearing down

    def _apply_history(self, hist: list[dict], total: int) -> None:
        self._history, self._history_total = hist, total
        self.refresh_frame()

    # -- section data / sizing --------------------------------------------
    def _section_jobs(self, section: str) -> list[dict]:
        snap = self.app.snapshot
        return q_section_jobs(section, snap.get("running"), snap.get("queued", []),
                              self._history)

    # Full natural wording — wraps to 2 lines on a narrow terminal (wrap_hints),
    # one line on a wide one. No hand-trimming to fit 100 cols.
    FOOTER = ("[r]/[q]/[h] section   ↑/↓ select   ←/→ page   [c] cancel   "
              "[p] prioritize   [x] cancel all   [Enter] detail   Esc back")

    def _section_rows(self, section: str) -> int:
        geo = self._geo
        return {"running": 1, "queued": geo.queued_rows, "history": geo.recent_rows}[section]

    def frame(self) -> str:
        geo = self._geo = self.geo_for(self.FOOTER)
        snap = self.app.snapshot
        # DISPLAY masking before layout (job labels embed the root name); raw snapshot +
        # history are still used for selection + actions (_section_jobs, cancel/prioritize).
        # History is the LAZY-loaded current page; pass its TRUE total-page count so the
        # paginator reads page K/N over all terminal jobs (only one page is in memory).
        body = queue_body(
            self.app.view(snap.get("running")), self.app.view(snap.get("queued", [])),
            self.app.view(self._history), now=self.now,
            geo=geo, focus=self.focus,
            queued_cursor=self.cursors["queued"], queued_page=self.pages["queued"],
            history_cursor=self.cursors["history"], history_page=self.pages["history"],
            history_total_pages=self._history_pages(),
            running_cursor=self.cursors["running"],
        )
        return screen("packrat · Queue", body, self.app.header_right,
                      footer=self.FOOTER, width=geo.w, height=geo.h)

    # -- navigation (focused section only) --------------------------------
    def action_focus_section(self, section: str) -> None:
        self.focus = section
        self.refresh_frame()

    def action_move(self, delta: int) -> None:
        sec = self.focus
        n = len(self._section_jobs(sec))
        rows = self._section_rows(sec)
        cur = max(0, min(self.cursors[sec] + delta, n - 1)) if n else 0
        self.cursors[sec] = cur
        # History's cursor is WITHIN the loaded page (rows come pre-sliced), so it never
        # auto-advances the page here — page changes are explicit (←/→ → re-fetch). The
        # snapshot-backed sections still auto-follow their client-side page.
        if sec != "history":
            self.pages[sec] = cur // rows if rows else 0
        self.refresh_frame()

    def action_page(self, delta: int) -> None:
        sec = self.focus
        rows = self._section_rows(sec)
        if sec == "history":
            # Lazy-loaded: clamp to the TRUE page count, then re-fetch that page. Cursor
            # resets to the page's first row (rows arrive from the server, not sliced here).
            pages = self._history_pages()
            new = max(0, min(self.pages["history"] + delta, pages - 1))
            if new != self.pages["history"]:
                self.pages["history"] = new
                self.cursors["history"] = 0
                self._reload_history()
            self.refresh_frame()
            return
        n = len(self._section_jobs(sec))
        pages = q_section_pages(n, rows)
        new = max(0, min(self.pages[sec] + delta, pages - 1))
        if new != self.pages[sec]:
            self.pages[sec] = new
            self.cursors[sec] = min(new * rows, max(0, n - 1))   # → first item on page
        self.refresh_frame()

    def _selected(self) -> dict | None:
        jobs = self._section_jobs(self.focus)
        i = self.cursors[self.focus]
        return jobs[i] if jobs and 0 <= i < len(jobs) else None

    def action_detail(self) -> None:
        job = self._selected()
        if job:
            self.app.push_screen(JobCard(job))

    def action_cancel(self) -> None:
        job = self._selected()
        if job and job.get("status") in ("queued", "running"):
            jid = job["id"]
            self.app.confirm_verb(f"Cancel {job['label']} (#{jid})?",
                                  f"packrat jobs cancel {jid}",
                                  submit=lambda: self.app.client.cancel_job(jid))

    def action_prioritize(self) -> None:
        job = self._selected()
        if job and job.get("status") == "queued":
            jid = job["id"]
            self.app.run_verb(f"packrat jobs prioritize {jid}",
                              submit=lambda: self.app.client.prioritize_job(jid))

    def action_cancel_all(self) -> None:
        queued = self.app.snapshot.get("queued", [])
        if queued:
            self.app.confirm_verb(f"Cancel all {len(queued)} queued job(s)?",
                                  "packrat jobs cancel --all-queued",
                                  submit=lambda: self.app.client.cancel_queued())
