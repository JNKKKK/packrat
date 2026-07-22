"""The Dashboard screen (M6, §12) — see :mod:`packrat.tui.frames.base`."""

from __future__ import annotations

from textual.binding import Binding

from ..colorize import colorize
from ..framing import screen
from ..nav import DashboardFocus
from ..screens.dashboard import dashboard_body
from ..screens.dashboard import queue_preview_pages

from .base import FrameScreen
from .rootsmax import RootsMax
from .queuemax import QueueMax
from .jobcard_screen import JobCard


# ---------------------------------------------------------------------------
# Dashboard (§1) — the default screen + focus→maximize state machine
# ---------------------------------------------------------------------------
class Dashboard(FrameScreen):
    BINDINGS = [
        Binding("r", "focus('r')", "focus Roots", show=False),
        Binding("q", "focus('q')", "focus Queue", show=False),
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("left", "page(-1)", show=False),
        Binding("right", "page(1)", show=False),
        Binding("enter", "drill", show=False),
        Binding("escape", "unfocus", show=False),
        # Queue-focus actions (§1.4 footer) — only meaningful when Queue is focused.
        Binding("c", "cancel", show=False),
        Binding("p", "prioritize", show=False),
        Binding("x", "cancel_all", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.focus_state = DashboardFocus()
        self.roots_page = 0
        self.queue_page = 0
        # Logo hoard animation: the held gem glyph swaps across render.LOGO_GEMS every
        # LOGO_GEM_SWAP_TICKS, and its color shimmers along the GEM_GRADIENT each tick.
        self._anim_tick = 0
        self._gem_phase = 0.0

    def on_mount(self) -> None:
        super().on_mount()
        # Drive the logo animation on its own light timer (offline + online). Only the
        # top section changes; the tick just advances state + re-renders the frame.
        from ..tokens import LOGO_ANIM_INTERVAL_S
        self.set_interval(LOGO_ANIM_INTERVAL_S, self._tick_logo)

    def _tick_logo(self) -> None:
        from ..tokens import LOGO_GRADIENT_STEP
        self._anim_tick += 1
        self._gem_phase = (self._gem_phase + LOGO_GRADIENT_STEP) % 1.0
        # Only repaint when the dashboard is the top screen (a pushed detail/modal
        # screen owns the display) — cheap guard so the timer idles in the background.
        if self.is_active:
            self.refresh_frame()

    @property
    def _gem(self) -> str:
        from ..tokens import LOGO_GEM_SWAP_TICKS
        from .. import render
        idx = (self._anim_tick // LOGO_GEM_SWAP_TICKS) % len(render.LOGO_GEMS)
        return render.LOGO_GEMS[idx]

    def _colorize(self, frame: str):
        # Apply the base theme colors, then sweep the gem's gradient on top so the held
        # stone glints — and tint the "· N assets hoarded ·" count the SAME color so the
        # number glints with the gem (post-layout, live widget only — §Theming).
        from ..colorize import (gem_gradient_color, recolor_gem, recolor_hoard_count,
                               shade_box_title)
        # Base colors + the ▸-selected-row emphasis (inherited from FrameScreen), then
        # the dashboard's own effects on top: the gem gradient sweep, the hoard-count
        # tint, and the focused box's shaded title tab.
        text = super()._colorize(frame)
        color = gem_gradient_color(self._gem_phase)
        recolor_gem(text, frame, self._gem, color)
        recolor_hoard_count(text, frame, color)
        # Focused box: shade its title tab + pager (a highlighted-tab look).
        if self.focus_state.target == "roots":
            shade_box_title(text, frame, "[R]oots")
        elif self.focus_state.target == "queue":
            shade_box_title(text, frame, "[Q]ueue")
        return text

    def _sync_lens(self) -> None:
        snap = self.app.snapshot
        self.focus_state.roots_len = len(snap.get("roots", []))
        # The queue cursor navigates running + queued (running is row 0), matching
        # the dashboard preview's selectable set.
        self.focus_state.queue_len = (1 if snap.get("running") else 0) + len(snap.get("queued", []))

    def frame(self) -> str:
        fs = self.focus_state
        self._sync_lens()
        footer = (
            "↑/↓ select  [Enter] detail  [c] cancel  [p] prioritize  [x] cancel all  "
            "[q] maximize  Esc unfocus"
            if fs.target == "queue"
            else "↑/↓ select root   [Enter] open detail   ←/→ page   [r] maximize   Esc unfocus"
            if fs.target == "roots"
            else "[r] focus Roots   [q] focus Queue (again = maximize)   Esc / Ctrl-Q quit"
        )
        geo = self._geo = self.geo_for(footer)
        # view(): NSFW-masked copy for DISPLAY (masks name/path before layout, so
        # elision can't split a keyword across a `…` — the leak fix). Raw snapshot is
        # still used for navigation/counts (_sync_lens above, _queue_jobs below).
        body = dashboard_body(
            self.app.view(self.app.snapshot), now=self.now, geo=geo, focus=fs.target,
            roots_cursor=fs.roots_cursor, roots_page=self.roots_page,
            queue_cursor=fs.queue_cursor, queue_page=self.queue_page,
            gem=self._gem,
        )
        return screen("packrat", body, self.app.header_right, footer=footer,
                      width=geo.w, height=geo.h)

    def action_page(self, delta: int) -> None:
        # ←/→ pages the focused box and moves the cursor to the FIRST item on the
        # new page (so the ▸ is never left behind on the previous page). Both the
        # Roots and Queue boxes page in place; the full backlog is also in §4.
        self._sync_lens()
        geo = self._geo
        fs = self.focus_state
        if fs.target == "roots":
            rows = geo.dash_roots_rows
            pages = max(1, -(-fs.roots_len // rows))
            new = max(0, min(self.roots_page + delta, pages - 1))
            if new != self.roots_page:
                self.roots_page = new
                fs.roots_cursor = min(new * rows, max(0, fs.roots_len - 1))
            self.refresh_frame()
        elif fs.target == "queue":
            rows = geo.dash_queue_rows
            pages = queue_preview_pages(self.app.snapshot, geo)
            new = max(0, min(self.queue_page + delta, pages - 1))
            if new != self.queue_page:
                self.queue_page = new
                fs.queue_cursor = min(new * rows, max(0, fs.queue_len - 1))
            self.refresh_frame()

    def action_focus(self, key: str) -> None:
        if not self.is_active:      # a modal is on top — don't act on bubbled keys
            return
        result = self.focus_state.press(key)
        if result == "maximize:roots":
            self.app.push_screen(RootsMax())
        elif result == "maximize:queue":
            self.app.push_screen(QueueMax())
        else:
            self.refresh_frame()

    def action_move(self, delta: int) -> None:
        self._sync_lens()
        geo = self._geo
        self.focus_state.move(delta)
        # keep the focused box's page in sync with its cursor (auto-follow)
        if self.focus_state.target == "roots":
            self.roots_page = self.focus_state.roots_cursor // geo.dash_roots_rows
        elif self.focus_state.target == "queue":
            self.queue_page = self.focus_state.queue_cursor // geo.dash_queue_rows
        self.refresh_frame()

    def action_unfocus(self) -> None:
        # Esc un-focuses a focused box; at the top level (nothing focused) it quits
        # the app — the dashboard is the root screen, so there's nothing to back out
        # to. (Ctrl-Q is the anywhere hard-quit; Ctrl-C is left for terminal copy.)
        if self.focus_state.escape():
            self.refresh_frame()
        else:
            self.app.exit()

    def _queue_jobs(self) -> list[dict]:
        # The dashboard queue box selects over running(row 0) + queued (rows 1+).
        snap = self.app.snapshot
        jobs = []
        if snap.get("running"):
            jobs.append(snap["running"])
        jobs.extend(snap.get("queued", []))
        return jobs

    def _selected_queue_job(self) -> dict | None:
        if self.focus_state.target != "queue":
            return None
        jobs = self._queue_jobs()
        i = self.focus_state.queue_cursor
        return jobs[i] if jobs and 0 <= i < len(jobs) else None

    def action_drill(self) -> None:
        if not self.is_active:
            return
        fs = self.focus_state
        if fs.target == "roots":
            roots = self.app.sorted_roots()
            if roots:
                self.app.open_root(roots[fs.roots_cursor]["name"])
        elif fs.target == "queue":
            job = self._selected_queue_job()
            if job:
                self.app.push_screen(JobCard(job))

    def action_cancel(self) -> None:
        job = self._selected_queue_job()
        if job and job.get("status") in ("queued", "running"):
            jid = job["id"]
            self.app.confirm_verb(f"Cancel {job['label']} (#{jid})?",
                                  f"packrat jobs cancel {jid}",
                                  submit=lambda: self.app.client.cancel_job(jid))

    def action_prioritize(self) -> None:
        job = self._selected_queue_job()
        if job and job.get("status") == "queued":   # only a queued job can be prioritized
            jid = job["id"]
            self.app.run_verb(f"packrat jobs prioritize {jid}",
                              submit=lambda: self.app.client.prioritize_job(jid))

    def action_cancel_all(self) -> None:
        if self.focus_state.target == "queue" and self.app.snapshot.get("queued"):
            n = len(self.app.snapshot["queued"])
            self.app.confirm_verb(f"Cancel all {n} queued job(s)?",
                                  "packrat jobs cancel --all-queued",
                                  submit=lambda: self.app.client.cancel_queued())
