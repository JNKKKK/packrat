"""The Textual app + screens (M6, §12) — thin display over the pure builders.

Each interface is a Textual :class:`~textual.screen.Screen` holding a single
:class:`~textual.widgets.Static` that shows the composed 100×24 frame produced by
a **pure builder** (``screens/*.py`` + ``framing.screen``). The screens own only
what Textual is for — key routing, focus, the screen stack, and liveness (poll +
SSE via :class:`~packrat.tui.data.DataSource`); all geometry/text lives in the
pure layer, so the frames stay golden-testable and §12's fixed layout is enforced
in one place.

State lives on the :class:`PackratApp`: the read-model snapshots (refreshed on a
light poll timer + on job-finished) and the daemon client. Actions map to CLI
verbs / daemon endpoints (§1.6) — the TUI issues no privileged op of its own.
"""

from __future__ import annotations

import time

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static

from . import demo, fixtures
from .colorize import colorize
from .data import EtaEstimator, reltime
from .framing import screen
from .geometry import REF_H, REF_W, Geometry
from .layout import wrap_hints
from .modals import ChoiceModal, ConfirmModal, TrashRefreshModal
from .nav import DashboardFocus
from .screens import jobcard
from .screens.dashboard import dashboard_body, queue_preview_pages
from .screens.queue import queue_body
from .screens.queue import section_jobs as q_section_jobs
from .screens.queue import section_pages as q_section_pages
from .screens.merge import merge_body, merge_sources, source_list_rows
from .screens.rootdetail import detail_body, detail_header_right
from .screens.roots import ADD_ROOT_FIELDS, add_root_body, roots_body


def _review_verb(pending: dict) -> str:
    """The CLI verb that confirms/cancels a pending review run (dedup vs cleanup)."""
    return "cleanup" if pending.get("run_type") == "cleanup-perceptual" else "dedup"


def _empty_snapshot() -> dict:
    """A complete, zeroed ``status_snapshot()``-shaped dict for the daemon-down state.

    Every pure builder indexes ``snap["assets"]`` etc. directly (not ``.get``), so an
    empty ``{}`` crashes the dashboard with ``KeyError``. This is the safe default the
    app renders before the first successful fetch / when the daemon is unreachable —
    the frame draws (all zeros, ``daemon ○ down`` in the header), never crashes."""
    return {
        "assets": 0, "photos": 0, "videos": 0, "trashed": 0,
        "size_bytes": 0, "lifetime_deduped": 0,
        "running": None, "queued": [], "interrupted": [],
        "pending_reviews": [], "roots": [],
    }


def _open_in_explorer(path: str) -> None:
    """Open ``path`` in the OS file manager (the [o] review action, a local op).

    Not a daemon call — the TUI observes/controls jobs but reviewing happens in
    Explorer (§12 "observe-and-control, not a file manager"). Returns None (no job
    id), so the notice just says "submitted"."""
    review = f"{path}\\_packrat_review\\"
    import os
    import subprocess
    if os.name == "nt":
        os.startfile(review)  # type: ignore[attr-defined]
    else:  # dev fallback (non-Windows)
        subprocess.Popen(["xdg-open", review])


# ---------------------------------------------------------------------------
# base screen — one Static showing a fixed W×H frame
# ---------------------------------------------------------------------------
class FrameScreen(Screen):
    """A screen that renders exactly one 100×24 frame from a pure builder.

    Subclasses implement :meth:`frame` (→ the composed string) and declare
    ``BINDINGS``; :meth:`refresh_frame` re-renders. The single ``Static`` is sized
    to the fixed frame by ``packrat.tcss`` (§12 — a fixed root container, not
    auto-sizing widgets).
    """

    def compose(self) -> ComposeResult:
        # markup=False: our frames are pre-composed PLAIN text. Textual's default
        # markup parsing would treat the `[R]`/`[Q]`/`[c]` hint brackets as style
        # tags — consuming them (dropping `[R]` from a title) and bleeding a bad
        # span's background into the footer/border. Color is applied by ROLE, not
        # inline markup (§Theming), so markup must be off here.
        yield Static(id="frame", markup=False)

    def on_mount(self) -> None:
        self.refresh_frame()

    def on_resize(self, event) -> None:
        # Responsive (Level B): the frame is laid out to the live terminal size, so
        # re-render on every resize. `self.geo` reads the current size at build time.
        self.refresh_frame()

    # the Geometry the last frame() built (footer-aware); action handlers reuse it
    # so their pagination budgets match what was rendered. Set in every frame().
    _geo: Geometry = Geometry(REF_W, REF_H)

    def _term_size(self) -> tuple[int, int]:
        """Live terminal size, clamped to the ≥100×24 reference minimum."""
        size = self.size
        w = max(REF_W, size.width) if size.width else REF_W
        h = max(REF_H, size.height) if size.height else REF_H
        return w, h

    @property
    def geo(self) -> Geometry:
        """Layout budgets for the live terminal size (1-row footer default).

        The Static fills the whole screen (packrat.tcss ``width/height: 100%``), so
        ``self.size`` is the terminal size, clamped to the reference minimum."""
        w, h = self._term_size()
        return Geometry(w, h)

    def geo_for(self, footer: str) -> Geometry:
        """Geometry whose ``content_rows`` accounts for a (possibly wrapping) footer.

        A long hint bar wraps to 2+ lines on a narrow terminal (:func:`wrap_hints`),
        which eats content rows — so pagination budgets must subtract them. Screens
        call this with their footer *before* building the body."""
        w, h = self._term_size()
        rows = len(wrap_hints(footer, (w - 2) - 2))   # content width = (w-2)-2
        return Geometry(w, h, footer_rows=rows)

    def refresh_frame(self) -> None:
        self.current_frame = self.frame()      # PLAIN string (tests / snapshotting)
        # Colorize post-layout (§Theming): the plain frame stays the source of
        # truth; only the live widget gets theme role colors applied by pattern.
        self.query_one("#frame", Static).update(self._colorize(self.current_frame))

    #: Whether this screen's ``▸`` marks a selectable LIST ROW (→ bold + brighter-white
    #: emphasis). True for every list screen; a screen whose ``▸`` is a *form field*
    #: marker (:class:`AddRootScreen`) sets this False, since a focused field-cursor at
    #: the row start is indistinguishable from a list cursor by text alone.
    EMPHASIZE_SELECTED_ROW = True

    def _colorize(self, frame: str):
        """Plain frame → colorized Rich ``Text`` (overridable for per-frame effects,
        e.g. the dashboard's animated logo-gem gradient).

        Applies the base theme colors, then (when :attr:`EMPHASIZE_SELECTED_ROW`)
        emphasizes the ``▸``-selected list row (bold + brighter white) — a single place
        so EVERY list screen (roots, queue, merge sources, root-detail jobs) gets the
        same focus emphasis. Overrides call ``super()._colorize(frame)`` to inherit it,
        then layer their own effects."""
        from .colorize import emphasize_selected_row
        text = colorize(frame)
        if self.EMPHASIZE_SELECTED_ROW:
            emphasize_selected_row(text, frame)
        return text

    def poll_reload(self) -> None:
        """Re-fetch this screen's OWN read-model, if any (called on the poll timer).

        The dashboard/queue read straight off ``app.snapshot`` (refreshed centrally),
        so the base is a no-op. Screens with a per-screen fetch (root detail's
        ``status <root>`` + ``root_jobs``) override this to refresh on the poll instead
        of inside :meth:`frame` — so a keypress re-render doesn't re-hit the daemon."""

    @property
    def is_active(self) -> bool:
        """True only when this screen is the top of the stack.

        Key bindings on a lower screen still fire if a modal on top doesn't handle
        the key (Textual bubbles unhandled keys down the stack). Actions that push
        a screen must guard on this, or a background screen can push a modal while
        another modal is already up — which corrupts the screen stack. Guarding the
        *action* is the driver-safe fix (vs. swallowing keys in the overlay)."""
        try:
            return self.app.screen is self
        except Exception:
            return False

    def frame(self) -> str:  # pragma: no cover - overridden
        return ""

    @property
    def now(self) -> str:
        return self.app.now


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
        from .tokens import LOGO_ANIM_INTERVAL_S
        self.set_interval(LOGO_ANIM_INTERVAL_S, self._tick_logo)

    def _tick_logo(self) -> None:
        from .tokens import LOGO_GRADIENT_STEP
        self._anim_tick += 1
        self._gem_phase = (self._gem_phase + LOGO_GRADIENT_STEP) % 1.0
        # Only repaint when the dashboard is the top screen (a pushed detail/modal
        # screen owns the display) — cheap guard so the timer idles in the background.
        if self.is_active:
            self.refresh_frame()

    @property
    def _gem(self) -> str:
        from .tokens import LOGO_GEM_SWAP_TICKS
        from . import render
        idx = (self._anim_tick // LOGO_GEM_SWAP_TICKS) % len(render.LOGO_GEMS)
        return render.LOGO_GEMS[idx]

    def _colorize(self, frame: str):
        # Apply the base theme colors, then sweep the gem's gradient on top so the held
        # stone glints — and tint the "· N assets hoarded ·" count the SAME color so the
        # number glints with the gem (post-layout, live widget only — §Theming).
        from .colorize import (gem_gradient_color, recolor_gem, recolor_hoard_count,
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
        body = dashboard_body(
            self.app.snapshot, now=self.now, geo=geo, focus=fs.target,
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


# ---------------------------------------------------------------------------
# Roots interface (§2)
# ---------------------------------------------------------------------------
class RootsMax(FrameScreen):
    BINDINGS = [
        Binding("s", "sort", "sort", show=False),
        Binding("a", "add", "add root", show=False),
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("left", "page(-1)", show=False),
        Binding("right", "page(1)", show=False),
        Binding("enter", "open", show=False),
        Binding("escape", "app.pop_screen", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sort_mode = 0
        self.cursor = 0
        self.page = 0

    def _ordered(self) -> list[dict]:
        from . import render
        return render.sort_roots(self.app.snapshot.get("roots", []), self.sort_mode)

    FOOTER = ("↑/↓ select   [Enter] open detail   ←/→ page   "
              "[s] sort   [a] add root   Esc back")

    def frame(self) -> str:
        geo = self._geo = self.geo_for(self.FOOTER)
        body = roots_body(self.app.snapshot.get("roots", []), now=self.now, geo=geo,
                          sort_mode=self.sort_mode, cursor=self.cursor, page=self.page)
        return screen("packrat · Roots", body, self.app.header_right,
                      footer=self.FOOTER, width=geo.w, height=geo.h)

    def action_sort(self) -> None:
        self.sort_mode = (self.sort_mode + 1) % 4
        self.cursor = 0
        self.page = 0
        self.refresh_frame()

    def action_move(self, delta: int) -> None:
        n = len(self._ordered())
        self.cursor = max(0, min(self.cursor + delta, n - 1)) if n else 0
        self.page = self.cursor // self._geo.roots_list_rows    # keep the cursor on-page
        self.refresh_frame()

    def action_page(self, delta: int) -> None:
        rows = self._geo.roots_list_rows
        n = len(self._ordered())
        pages = max(1, -(-n // rows))
        new = max(0, min(self.page + delta, pages - 1))
        if new != self.page:                       # move cursor to the new page's first item
            self.page = new
            self.cursor = min(new * rows, max(0, n - 1))
        self.refresh_frame()

    def action_add(self) -> None:
        self.app.push_screen(AddRootScreen())

    def action_open(self) -> None:
        roots = self._ordered()
        if roots:
            self.app.open_root(roots[self.cursor]["name"])


class AddRootScreen(FrameScreen):
    # A form, not a list: its ▸ marks the focused FIELD (and the scan field's marker sits
    # at the row start, where it would otherwise read as a list cursor). Opt out of the
    # selected-row emphasis so a focused field is never bold-highlighted like a list row.
    EMPHASIZE_SELECTED_ROW = False

    BINDINGS = [
        Binding("tab", "next_field", show=False),
        Binding("shift+tab", "prev_field", show=False),
        Binding("space", "toggle", show=False),
        Binding("backspace", "backspace", show=False),
        Binding("enter", "register", show=False),
        Binding("escape", "app.pop_screen", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        # Start blank — the user types the path/name (no pre-filled sample).
        self.path = ""
        self.root_name = ""
        self.kind = "library"     # toggled between library/trash on the Kind field
        self.scan = True          # toggled on the scan field
        self.full = False         # --full re-hash, toggled on the full field
        self.field_idx = 0        # index into ADD_ROOT_FIELDS ([Tab] focus order)

    @property
    def _field(self) -> str:
        return ADD_ROOT_FIELDS[self.field_idx]

    def frame(self) -> str:
        footer = ("[Tab] next field   [Space] toggle   type to edit   "
                  "[Enter] register   Esc cancel")
        geo = self._geo = self.geo_for(footer)
        body = add_root_body(path=self.path, name=self.root_name, kind=self.kind,
                             scan=self.scan, full=self.full, focus_field=self._field, geo=geo)
        return screen("packrat · Roots · add", body, self.app.header_right,
                      footer=footer, width=geo.w, height=geo.h)

    # -- field navigation (§2.2) -------------------------------------------
    def action_next_field(self) -> None:
        self.field_idx = (self.field_idx + 1) % len(ADD_ROOT_FIELDS)
        self.refresh_frame()

    def action_prev_field(self) -> None:
        self.field_idx = (self.field_idx - 1) % len(ADD_ROOT_FIELDS)
        self.refresh_frame()

    def action_toggle(self) -> None:
        """[Space] toggles the focused choice field (Kind radio / scan|full checkbox)."""
        if self._field == "kind":
            self.kind = "trash" if self.kind == "library" else "library"
            self.refresh_frame()
        elif self._field == "scan":
            self.scan = not self.scan
            self.refresh_frame()
        elif self._field == "full":
            self.full = not self.full
            self.refresh_frame()
        # a text field's space is handled by on_key (below), not a toggle.

    def action_backspace(self) -> None:
        if self._field == "path" and self.path:
            self.path = self.path[:-1]
            self.refresh_frame()
        elif self._field == "name" and self.root_name:
            self.root_name = self.root_name[:-1]
            self.refresh_frame()

    def on_key(self, event) -> None:
        """Type into the focused text field (path/name). Bound keys pass through."""
        if self._field not in ("path", "name"):
            return
        ch = event.character
        # only printable single chars; let bindings (tab/enter/esc/backspace) run
        if ch and ch.isprintable() and len(ch) == 1 and event.key not in ("space",):
            self._append(ch)
            event.stop()
        elif event.key == "space" and self._field in ("path", "name"):
            # space is a literal character in a text field (not the toggle binding)
            self._append(" ")
            event.stop()

    def on_paste(self, event) -> None:
        """Paste (Ctrl+V / Ctrl+Shift+V) into the focused text field.

        Textual delivers a clipboard paste as a single ``Paste`` event with the
        whole text (bracketed-paste mode, enabled automatically) — so paste isn't a
        burst of key events and must be handled here, not in ``on_key``."""
        if self._field not in ("path", "name"):
            return
        text = event.text.replace("\r", "").replace("\n", "")   # paths are single-line
        if text:
            self._append(text)
        event.stop()

    def _append(self, text: str) -> None:
        if self._field == "path":
            self.path += text
        else:
            self.root_name += text
        self.refresh_frame()

    def _back(self) -> None:
        """Pop the form back to the Roots interface that opened it.

        Fired via ``run_verb(then=…)`` right after the register toast is posted, so
        pressing [Enter] returns the user to the previous page instead of leaving them
        on a now-submitted form (matching JobCard's back-after-action behavior, §5).
        Guarded on ``is_active`` + a non-empty stack so a bubbled key can't pop the
        wrong screen."""
        if self.is_active and self.app.screen_stack:
            self.app.pop_screen()

    def action_register(self) -> None:
        parts = [f"packrat roots register {self.path}"]
        if self.root_name:
            parts.append(f"--name {self.root_name}")
        # --full only makes sense with a scan of a library root; a trash root is never
        # scanned, so scan/full drop out for it (mirrors the CLI + the form's own note).
        do_scan = self.scan and self.kind == "library"
        if self.kind == "trash":
            parts.append("--kind trash")
        elif self.scan:
            parts.append("--scan")
            if self.full:
                parts.append("--full")
        path, name, kind, full = self.path, self.root_name, self.kind, self.full

        def submit():
            # register_root returns {root, job_id}; report the scan job id if any.
            resp = self.app.client.register_root(
                path, name=name or None, kind=kind,
                scan=do_scan, full=(full and do_scan))
            return resp.get("job_id")

        self.app.run_verb(" ".join(parts), title="register root", submit=submit,
                          then=self._back)


# ---------------------------------------------------------------------------
# Merge-from picker (§3.3)
# ---------------------------------------------------------------------------
class MergePickerScreen(FrameScreen):
    """Pick a merge SOURCE for a fixed destination root (§3.3).

    ``[Tab]`` toggles the source between a paginated **registered-root** list
    (library roots, dest excluded) and a typed **external folder** path; ``↑/↓``
    picks a root, ``←/→`` pages it, ``[Space]`` toggles ``--dry-run``, typing edits
    the external path, ``[Enter]`` submits ``merge <source> --into <dest>``.
    """

    BINDINGS = [
        Binding("tab", "toggle_source", show=False),
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("left", "page(-1)", show=False),
        Binding("right", "page(1)", show=False),
        Binding("ctrl+d", "toggle_dry_run", show=False),   # both modes; Space types in ext
        Binding("backspace", "backspace", show=False),
        Binding("enter", "merge", show=False),
        Binding("escape", "app.pop_screen", show=False),
    ]

    def __init__(self, dest: dict) -> None:
        super().__init__()
        self.dest = dest
        self.source_mode = "root"     # 'root' | 'ext'
        self.cursor = 0
        self.page = 0
        self.ext_path = ""
        self.dry_run = False

    def _sources(self) -> list[dict]:
        return merge_sources(self.app.snapshot.get("roots", []), self.dest["name"])

    FOOTER_ROOT = ("↑/↓ pick   ←/→ page   [Tab] switch source   "
                   "[Ctrl-D] --dry-run   [Enter] merge   Esc")
    FOOTER_EXT = ("type to edit path   [Tab] switch source   "
                  "[Ctrl-D] --dry-run   [Enter] merge   Esc")

    def frame(self) -> str:
        footer = self.FOOTER_ROOT if self.source_mode == "root" else self.FOOTER_EXT
        geo = self._geo = self.geo_for(footer)
        body = merge_body(self.dest, self._sources(), geo=geo,
                          source_mode=self.source_mode, cursor=self.cursor,
                          page=self.page, ext_path=self.ext_path, dry_run=self.dry_run)
        right = f"{self.dest['path']} · {self.dest['kind']}"
        return screen(f"packrat · {self.dest['name']} · merge from", body, right,
                      footer=footer, width=geo.w, height=geo.h)

    # -- navigation --------------------------------------------------------
    def action_toggle_source(self) -> None:
        self.source_mode = "ext" if self.source_mode == "root" else "root"
        self.refresh_frame()

    def action_move(self, delta: int) -> None:
        if self.source_mode != "root":
            return
        n = len(self._sources())
        self.cursor = max(0, min(self.cursor + delta, n - 1)) if n else 0
        self.page = self.cursor // source_list_rows(self._geo)
        self.refresh_frame()

    def action_page(self, delta: int) -> None:
        if self.source_mode != "root":
            return
        rows = source_list_rows(self._geo)
        n = len(self._sources())
        pages = max(1, -(-n // rows))
        new = max(0, min(self.page + delta, pages - 1))
        if new != self.page:                       # cursor → first item of new page
            self.page = new
            self.cursor = min(new * rows, max(0, n - 1))
        self.refresh_frame()

    def action_toggle_dry_run(self) -> None:
        self.dry_run = not self.dry_run
        self.refresh_frame()

    def action_backspace(self) -> None:
        if self.source_mode == "ext" and self.ext_path:
            self.ext_path = self.ext_path[:-1]
            self.refresh_frame()

    def on_key(self, event) -> None:
        """Type into the external-path field (path mode only). Bound keys pass through."""
        if self.source_mode != "ext":
            return
        ch = event.character
        if ch and ch.isprintable() and len(ch) == 1 and event.key != "space":
            self.ext_path += ch
            self.refresh_frame()
            event.stop()
        elif event.key == "space":
            # In the ext field, Space is a literal char, NOT the dry-run toggle.
            self.ext_path += " "
            self.refresh_frame()
            event.stop()

    def on_paste(self, event) -> None:
        """Paste (Ctrl+V / Ctrl+Shift+V) a path into the external-folder field.

        A clipboard paste is one ``Paste`` event with the whole text (not key
        bursts) — the common way to enter a long path. Path mode only."""
        if self.source_mode != "ext":
            return
        text = event.text.replace("\r", "").replace("\n", "")
        if text:
            self.ext_path += text
            self.refresh_frame()
        event.stop()

    def action_merge(self) -> None:
        dest = self.dest["name"]
        if self.source_mode == "root":
            sources = self._sources()
            if not sources:
                return
            src = sources[self.cursor]
            src_disp, src_arg = src["name"], src["path"]
        else:
            if not self.ext_path.strip():
                return
            src_disp = src_arg = self.ext_path.strip()
        dry = " --dry-run" if self.dry_run else ""
        cmd = f"packrat merge {src_disp} --into {dest}{dry}"
        self.app.run_verb(
            cmd, title="merge",
            submit=lambda: self.app.client.submit_merge(src_arg, dest, dry_run=self.dry_run))


# ---------------------------------------------------------------------------
# Root detail (§3)
# ---------------------------------------------------------------------------
class RootDetailScreen(FrameScreen):
    BINDINGS = [
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("left", "page(-1)", show=False),
        Binding("right", "page(1)", show=False),
        Binding("enter", "result", show=False),
        Binding("e", "focus_review", show=False),   # R[e]view box
        Binding("j", "focus_jobs", show=False),
        Binding("r", "focus_section('running')", show=False),
        Binding("q", "focus_section('queued')", show=False),
        Binding("h", "focus_section('history')", show=False),
        Binding("s", "scan", show=False),
        Binding("d", "dedup", show=False),
        Binding("m", "merge", show=False),
        Binding("c", "cleanup", show=False),
        Binding("o", "open_review", show=False),
        Binding("g", "confirm_review", show=False),
        Binding("b", "confirm_keep_suggested", show=False),
        Binding("k", "cancel_review", show=False),
        Binding("escape", "back", show=False),
    ]

    def __init__(self, root_name: str) -> None:
        super().__init__()
        self.root_name = root_name
        # Two focus-able bordered boxes (like the dashboard): the R[e]view box ([e]) and
        # the Jobs panel ([J]). `focus` is which box is focused (None | "review" |
        # "jobs"); within the Jobs panel, [r]/[q]/[h] pick the sub-section (each with
        # its own cursor + page). Unfocused by default.
        self.focus: str | None = None
        self.job_focus = "running"       # default Jobs sub-section (§3, matches Queue)
        self.cursors = {"running": 0, "queued": 0, "history": 0}
        self.pages = {"running": 0, "queued": 0, "history": 0}
        self._jobs: list[dict] = []      # last-fetched jobs (refreshed on mount + poll)
        self._detail: dict | None = None
        self._loaded = False             # False until the first reload() populates data

    FOOTER_BASE = ("[s] scan  [d] dedup  [m] merge from…  [c] clean up  "
                   "[e] review  [J] jobs  Esc")
    FOOTER_REVIEW = ("[o] open in Explorer   [g] confirm stage   [k] cancel run   "
                     "Esc unfocus")
    # Stage-2 dedup also offers the bulk keep-suggested confirm (§8 B --keep-suggested).
    FOOTER_REVIEW_STAGE2 = ("[o] open in Explorer   [g] confirm stage   "
                            "[b] keep suggested   [k] cancel run   Esc unfocus")
    FOOTER_REVIEW_EMPTY = "no pending review — nothing to act on   Esc unfocus"
    FOOTER_JOBS = ("[r]/[q]/[h] section   ↑/↓ select   ←/→ page   [Enter] result   "
                   "Esc unfocus")

    # The three cleanup modes (§6.2) offered by [c]; label → CLI flag. Labels kept
    # short enough to fit the choice modal (≤ ~54 cells) without wrapping.
    CLEANUP_MODES = [
        ("trash-exact  (delete byte-identical trash)", "--trash-exact"),
        ("trash-perceptual  (stage recompressed trash)", "--trash-perceptual"),
        ("undecodable  (delete non-decoding files)", "--undecodable"),
    ]

    def reload(self) -> None:
        """Fetch this root's detail + jobs from the daemon (mount + first paint).

        ``root_detail`` online is two blocking HTTP calls (``status <root>`` +
        ``root_jobs``); doing it inside :meth:`frame` re-hit the daemon on every
        keypress and blocked the UI. We fetch here — once on mount — and :meth:`frame`
        renders from the cached ``self._detail``/``self._jobs``. The POLL path uses
        :meth:`poll_reload`, which fetches off the UI thread (a slow daemon must not
        freeze input on the timer)."""
        self._detail, self._jobs = self.app.root_detail(self.root_name)
        self._loaded = True

    def on_mount(self) -> None:
        self.reload()
        super().on_mount()

    def poll_reload(self) -> None:
        """Poll refresh — fetch off the UI thread so a slow daemon can't freeze input.

        Offline (in-memory demo) or with no running loop (unit tests) applies inline;
        online it hands the blocking fetch to a worker that marshals back via
        :meth:`_apply_reload`."""
        if self.app.offline or not self.app._app_loop_running():
            self.reload()
            return
        self._poll_fetch()

    @work(thread=True, exclusive=True, group="rootdetail-poll")
    def _poll_fetch(self) -> None:
        detail, jobs = self.app.root_detail(self.root_name)
        try:
            self.app.call_from_thread(self._apply_reload, detail, jobs)
        except Exception:
            pass   # screen/app tearing down

    def _apply_reload(self, detail, jobs) -> None:
        self._detail, self._jobs = detail, jobs
        self._loaded = True
        self.refresh_frame()

    def frame(self) -> str:
        # Render from the cached detail/jobs (fetched on mount + poll, not per keypress).
        if not self._loaded:
            self.reload()
        d, jobs = self._detail, self._jobs
        if self.focus == "review":
            if not self._has_review():
                footer = self.FOOTER_REVIEW_EMPTY
            elif self._is_stage2_dedup():
                footer = self.FOOTER_REVIEW_STAGE2
            else:
                footer = self.FOOTER_REVIEW
        elif self.focus == "jobs":
            footer = self.FOOTER_JOBS
        else:
            footer = self.FOOTER_BASE
        geo = self._geo = self.geo_for(footer)
        if d is None:
            return screen("packrat · ?", ["root not found."], self.app.header_right,
                          footer="Esc back", width=geo.w, height=geo.h)
        body = detail_body(d, now=self.now, geo=geo, jobs=jobs,
                          focus=self.focus, job_focus=self.job_focus,
                          cursors=self.cursors, pages=self.pages)
        return screen(f"packrat · {d['name']}", body, detail_header_right(d),
                      footer=footer, width=geo.w, height=geo.h)

    def _colorize(self, frame: str):
        # Base colors + the ▸-selected-row emphasis (inherited), then shade the focused
        # box's title tab (accent tab), matching the dashboard boxes.
        from .colorize import shade_box_title
        text = super()._colorize(frame)
        # A focused root-detail box drops its key-hint brackets (no maximize), so shade
        # the PLAIN title that _review_box/_jobs_panel render when focused.
        if self.focus == "review":
            shade_box_title(text, frame, "Review")
        elif self.focus == "jobs":
            shade_box_title(text, frame, "Jobs")
        return text

    # -- box focus + per-section navigation (mirrors QueueMax) ------------
    def _sections(self) -> dict:
        from .screens.rootdetail import split_jobs
        return split_jobs(self._detail or {}, self._jobs)

    def _section_jobs(self, section: str) -> list[dict]:
        return self._sections().get(section, [])

    def _section_rows(self, section: str) -> int:
        # The queued/history window heights the body used this frame (§3 panel split).
        from .screens.rootdetail import panel_section_rows
        return panel_section_rows(self._detail or {}, self._geo)[section]

    def action_focus_review(self) -> None:
        # [e] focuses the R[e]view box (always focus-able, even with no pending review —
        # the box is a permanent section, like Jobs).
        self.focus = "review"
        self.refresh_frame()

    def action_focus_jobs(self) -> None:
        self.focus = "jobs"
        self.refresh_frame()

    def action_focus_section(self, section: str) -> None:
        if self.focus == "jobs":
            self.job_focus = section
            self.refresh_frame()

    def action_back(self) -> None:
        # Esc un-focuses a focused box first; a second Esc backs out to Roots.
        if self.focus is not None:
            self.focus = None
            self.refresh_frame()
        else:
            self.app.pop_screen()

    def action_move(self, delta: int) -> None:
        if self.focus != "jobs":
            return
        sec = self.job_focus
        n = len(self._section_jobs(sec))
        rows = self._section_rows(sec)
        cur = max(0, min(self.cursors[sec] + delta, n - 1)) if n else 0
        self.cursors[sec] = cur
        self.pages[sec] = cur // rows if rows else 0     # auto-follow within section
        self.refresh_frame()

    def action_page(self, delta: int) -> None:
        if self.focus != "jobs":
            return
        sec = self.job_focus
        n = len(self._section_jobs(sec))
        rows = self._section_rows(sec)
        pages = max(1, -(-n // rows)) if rows else 1
        new = max(0, min(self.pages[sec] + delta, pages - 1))
        if new != self.pages[sec]:
            self.pages[sec] = new
            self.cursors[sec] = min(new * rows, max(0, n - 1))   # → first item on page
        self.refresh_frame()

    def _selected_job(self) -> dict | None:
        # The focused sub-section's ▸ row (jobs unfocused → newest history job).
        sec = self.job_focus if self.focus == "jobs" else "history"
        jobs = self._section_jobs(sec)
        i = self.cursors[sec] if self.focus == "jobs" else 0
        return jobs[i] if jobs and 0 <= i < len(jobs) else None

    def action_result(self) -> None:
        # [Enter] opens the selected job's result card — a Jobs-panel action (and the
        # unfocused default: newest history job). It is NOT a Review-box shortcut: the
        # Review footers don't advertise [Enter], and the box has its own [o]/[g]/[k]
        # actions with no job list to drill into. So Enter is inert while Review is
        # focused (otherwise it wrongly opened the newest history job's card).
        if self.focus == "review":
            return
        job = self._selected_job()
        if job:
            self.app.push_screen(JobCard(job))

    # -- per-root ops (§3): each maps to a CLI verb (§1.6), submitted for real
    #    online via the daemon client; offline shows the "would run" notice.
    def action_scan(self) -> None:
        root = self.root_name
        self.app.run_verb(f"packrat scan {root}",
                          submit=lambda: self.app.client.submit_scan(root))

    def action_dedup(self) -> None:
        root = self.root_name
        self.app.run_verb(f"packrat dedup {root}",
                          submit=lambda: self.app.client.submit_dedup(root))

    def action_merge(self) -> None:
        # [m] → the §3.3 merge-from picker (this root is the destination).
        if self.is_active and self._detail is not None:
            self.app.push_screen(MergePickerScreen(self._detail))

    def action_cleanup(self) -> None:
        """[c] → pick one of the 3 cleanup modes (§6.2), then run it."""
        if not self.is_active:
            return
        options = [label for label, _ in self.CLEANUP_MODES]
        root = self.root_name

        def after(idx):
            if idx is None:
                return
            flag = self.CLEANUP_MODES[idx][1]
            mode = {"--trash-exact": "exact", "--trash-perceptual": "perceptual",
                    "--undecodable": "undecodable"}[flag]
            cmd = f"packrat cleanup {root} {flag}"
            if mode == "perceptual":
                # Stateful analyze → pause: the bare submit IS the real step; the user
                # reviews staging and then confirms via the Review box's [g] (§6.2). No
                # count-confirm here — perceptual matches stage for review, not a tally.
                self.app.run_verb(cmd, title="clean up",
                                  submit=lambda: self.app.client.submit_cleanup(root, mode=mode))
            else:
                # One-shot modes (exact / undecodable): a bare submit only PREVIEWS
                # (counts + logs, deletes nothing — cleanup.py `_preview`). The delete
                # happens on a SECOND job with `apply=True`, gated by a typed count
                # confirmation — mirroring the CLI's preview → confirm → apply flow.
                self._cleanup_one_shot(root, mode, cmd)

        self.app.push_screen(ChoiceModal(options, title=f"clean up {root}"), after)

    @staticmethod
    def _cleanup_what(mode: str) -> str:
        return "undecodable file(s)" if mode == "undecodable" else "file(s) matching trashed content"

    def _cleanup_one_shot(self, root: str, mode: str, cmd: str) -> None:
        """Count-confirm → apply for a one-shot cleanup mode (exact / undecodable, §6.2).

        A bare ``submit_cleanup`` for these modes runs only the read-only preview leaf
        (it logs "would delete … Nothing deleted"), so the TUI must — like the CLI —
        fetch the count, require a typed count-confirmation (with the §10 network
        permanent-delete warning), and then submit the real ``apply=True`` job.

        **Exact mode refreshes the trash collection first** (§6.1), exactly like the CLI:
        a freshly-dropped trash-folder file must be absorbed into the trashed set *before*
        we count/delete its library re-appearances, or the TUI would silently delete fewer
        files than the same CLI command. That refresh runs inside a daemon PREVIEW job, so
        we stream it to completion off the UI thread, then count over the now-current set
        (:meth:`_cleanup_exact_refresh_then_confirm`). **Undecodable mode never refreshes**
        (it targets the folder's own undecodables, independent of the trashed set — §9.1),
        so it counts directly. **Offline** (demo, no daemon) degrades to a plain y/n confirm.
        """
        if self.app.offline:
            note = " (their assets are marked trashed)" if mode == "undecodable" else ""
            self.app.confirm_verb(
                f"Delete the matching {self._cleanup_what(mode)} in {root}?{note} "
                f"They move to the Recycle Bin.",
                cmd, count=None, network=0,
                submit=lambda: self.app.client.submit_cleanup(root, mode=mode, apply=True))
            return
        if mode == "exact":
            self.app.notify(f"{cmd}\nrefreshing the trash collection, then counting…",
                            title="clean up", severity="information")
            self._cleanup_exact_refresh_then_confirm(root, cmd)
            return
        # undecodable: no trash refresh → a read-only count is already current.
        prev = self._fetch_cleanup_preview(root, "undecodable", cmd)
        if prev is not None:
            self._offer_cleanup_delete(root, "undecodable", cmd, prev)

    def _fetch_cleanup_preview(self, root: str, mode: str, cmd: str) -> dict | None:
        """Read the daemon's read-only ``/cleanup/preview`` count; toast + None on failure."""
        try:
            return self.app.client.cleanup_preview(root, mode=mode)
        except Exception as exc:  # noqa: BLE001 - surfaced as a toast, never crash
            self.app.notify(f"{cmd}\ncouldn't count files to delete: {exc}",
                            title="clean up", severity="error")
            return None

    def _offer_cleanup_delete(self, root: str, mode: str, cmd: str, prev: dict) -> None:
        """Open the typed count-confirm for a one-shot cleanup (or a 'nothing to delete' toast).

        Called on the UI thread — directly for undecodable, via ``call_from_thread`` from
        the exact-mode refresh worker. ``prev`` is the ``/cleanup/preview`` dict (count +
        network) read AFTER any refresh, so the count matches what ``apply`` will delete."""
        count = int(prev.get("count", 0))
        network = int(prev.get("network", 0))
        what = self._cleanup_what(mode)
        if count == 0:
            self.app.notify(f"{cmd}\nno {what} — nothing to delete.",
                            title="clean up", severity="information")
            return
        note = " (their assets are marked trashed)" if mode == "undecodable" else ""
        self.app.confirm_verb(
            f"Delete {count} {what} in {root}?{note} They move to the Recycle Bin.",
            cmd, count=count, network=network,
            submit=lambda: self.app.client.submit_cleanup(root, mode=mode, apply=True))

    @work(thread=True, exclusive=True, group="cleanup-preview")
    def _cleanup_exact_refresh_then_confirm(self, root: str, cmd: str) -> None:
        """Run the exact-cleanup PREVIEW job (which refreshes trash, §6.1), wait for it to
        finish, then read the now-current count + open the confirm — all off the UI thread.

        The preview job's ``_preview`` leaf refreshes-and-empties the trash roots and
        commits before it reports, so by the time its SSE stream reaches a terminal event
        the refreshed trashed set is durable; the follow-up ``cleanup_preview`` GET then
        counts over it. A dropped/absent stream just falls through to the count (safe —
        the worst case is a slightly stale count, same as before this fix)."""
        try:
            job_id = self.app.client.submit_cleanup(root, mode="exact")   # preview → refresh
            try:
                for ev in self.app.client.stream_job(job_id):
                    if ev.get("type") in ("done", "error") or ev.get("status") in (
                            "done", "error", "cancelled", "interrupted"):
                        break
            except Exception:  # noqa: BLE001 - dropped stream → just count what we have
                pass
            prev = self.app.client.cleanup_preview(root, mode="exact")
        except Exception as exc:  # noqa: BLE001 - report, never crash the worker
            try:
                self.app.call_from_thread(
                    self.app.notify, f"{cmd}\ncouldn't refresh/count: {exc}",
                    title="clean up", severity="error")
            except Exception:
                pass
            return
        try:
            self.app.call_from_thread(self._offer_cleanup_delete, root, "exact", cmd, prev)
        except Exception:
            pass   # app tearing down

    def _has_review(self) -> bool:
        return bool(self._detail and self._detail.get("pending_review"))

    def _review_actionable(self) -> bool:
        """The review actions ([o]/[g]/[k]) fire only when the Review box is FOCUSED
        AND a review is pending — so pressing those keys elsewhere is inert (they're
        the box's inside shortcuts, dimmed while it's out of focus)."""
        return self.focus == "review" and self._has_review()

    def _is_stage2_dedup(self) -> bool:
        """True when the pending review is a dedup parked at stage 2 — the only case
        that offers ``--confirm --keep-suggested`` (§8 B)."""
        from .screens.rootdetail import is_stage2_dedup
        return bool(self._detail and is_stage2_dedup(self._detail.get("pending_review")))

    def action_open_review(self) -> None:
        if self._review_actionable():
            path = self._detail["path"]
            self.app.run_verb(f"explorer {path}\\_packrat_review\\", title="open in Explorer",
                              submit=lambda: _open_in_explorer(path))

    def _submit_review(self, verb: str, root: str, **kw):
        """Deferred daemon call for a review confirm/cancel (dedup or cleanup).

        Built as a thunk so ``self.app.client`` is only touched when actually run
        (online) — offline the client is None and this is never called."""
        if verb == "cleanup":
            return self.app.client.submit_cleanup(root, mode="perceptual", **kw)
        return self.app.client.submit_dedup(root, **kw)

    def action_confirm_review(self) -> None:
        if self._review_actionable():
            pr = self._detail["pending_review"]
            verb = _review_verb(pr)
            root = self.root_name
            # Warn when the stage's delete set includes files on a non-recyclable
            # network share (permanent, no Recycle Bin — §10). `network` is the §10 gate.
            network = (pr.get("counts") or {}).get("network", 0)
            self.app.confirm_verb(f"Confirm this {verb} stage for {root}?",
                                  f"packrat {verb} {root} --confirm",
                                  network=network,
                                  submit=lambda: self._submit_review(verb, root, confirm=True))

    def action_confirm_keep_suggested(self) -> None:
        """[b] on a stage-2 dedup review → `--confirm --keep-suggested` (§8 B): keep
        each group's suggested lead, delete the rest, ignoring shortcut edits. Inert
        unless the Review box is focused AND the run is a stage-2 dedup."""
        if self._review_actionable() and self._is_stage2_dedup():
            root = self.root_name
            network = (self._detail["pending_review"].get("counts") or {}).get("network", 0)
            self.app.confirm_verb(
                f"Confirm stage 2 for {root}, keeping packrat's suggested lead in each group?",
                f"packrat dedup {root} --confirm --keep-suggested",
                network=network,
                submit=lambda: self.app.client.submit_dedup(
                    root, confirm=True, keep_suggested=True))

    def action_cancel_review(self) -> None:
        if self._review_actionable():
            verb = _review_verb(self._detail["pending_review"])
            root = self.root_name
            self.app.confirm_verb(f"Cancel the whole {verb} run for {root}?",
                                  f"packrat {verb} {root} --cancel",
                                  submit=lambda: self._submit_review(verb, root, cancel=True))


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

    # -- section data / sizing --------------------------------------------
    def _section_jobs(self, section: str) -> list[dict]:
        snap = self.app.snapshot
        return q_section_jobs(section, snap.get("running"), snap.get("queued", []),
                              self.app.recent)

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
        body = queue_body(
            snap.get("running"), snap.get("queued", []), self.app.recent, now=self.now,
            geo=geo, focus=self.focus,
            queued_cursor=self.cursors["queued"], queued_page=self.pages["queued"],
            history_cursor=self.cursors["history"], history_page=self.pages["history"],
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
        self.pages[sec] = cur // rows if rows else 0     # auto-follow within section
        self.refresh_frame()

    def action_page(self, delta: int) -> None:
        sec = self.focus
        n = len(self._section_jobs(sec))
        rows = self._section_rows(sec)
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


# ---------------------------------------------------------------------------
# Job result / detail card (§5)
# ---------------------------------------------------------------------------
class JobCard(FrameScreen):
    BINDINGS = [
        Binding("up", "scroll(-1)", show=False),
        Binding("down", "scroll(1)", show=False),
        Binding("c", "cancel", show=False),
        Binding("o", "open_review", show=False),
        Binding("g", "confirm_review", show=False),
        Binding("k", "cancel_review", show=False),
        Binding("escape", "app.pop_screen", show=False),
    ]

    def __init__(self, job: dict) -> None:
        super().__init__()
        self.job = job
        self._problems: list[dict] = []   # a scan card's undecodable/read-error files
        self.problems_scroll = 0

    def on_mount(self) -> None:
        # A terminal scan card lists its problem files (paths + reasons, §12); fetch
        # them once (they don't change for a finished job) BEFORE the first frame.
        self._load_problems()
        super().on_mount()

    def _op(self) -> str:
        import json
        try:
            return json.loads(self.job.get("result_json") or "{}").get("op") or self.job["type"]
        except (ValueError, TypeError):
            return self.job["type"]

    def _load_problems(self) -> None:
        if self.job.get("status") in ("running", "error", "interrupted"):
            return
        if self._op() == "scan":
            self._problems = self.app.job_problem_files(self.job)

    def _review_ui(self) -> str | None:
        """This card's live review-action mode — ``'current'`` (owns the pending stage:
        open/confirm/cancel), ``'advanced'`` (a later stage is pending: open/cancel
        only), or ``None`` (no review actions). Reconciled by the data layer against the
        live ``review_runs`` row, so a stale analyze/confirm card never offers actions
        for a stage that was already confirmed or a run that finished (§8 B)."""
        return jobcard.review_ui(self.job)

    def _pending(self) -> bool:
        """True while this card owns a live pending stage (open + confirm + cancel)."""
        return self._review_ui() == "current"

    def _verb(self) -> str:
        """The review CLI verb this card's op maps to — ``cleanup`` or ``dedup``.

        A paused ``cleanup --trash-perceptual`` also lands here (its analyze now emits
        ``review_status='pending'``), so confirm/cancel must NOT be hardcoded to dedup."""
        return "cleanup" if self._op() == "cleanup" else "dedup"

    def _submit_review(self, root: str, **kw):
        """Deferred confirm/cancel call for this card's review (dedup or cleanup)."""
        if self._verb() == "cleanup":
            return self.app.client.submit_cleanup(root, mode="perceptual", **kw)
        return self.app.client.submit_dedup(root, **kw)

    def frame(self) -> str:
        j = self.job
        right = reltime(j.get("finished_at") or j.get("started_at"), self.now)
        if j.get("status") == "running":
            footer = "[c] cancel job   Esc back"
        elif self._pending():
            footer = "[o] open review   [g] confirm stage   [k] cancel run   Esc back"
        elif self._review_ui() == "advanced":
            # This stage was already confirmed; the run advanced to a later stage. Offer
            # opening that stage's folder + cancelling the run — but NOT confirm (it would
            # apply a different stage than this card shows).
            footer = "[o] open review   [k] cancel run   Esc back"
        elif self._problems:
            footer = "↑/↓ scroll problem files   Esc back"
        else:
            footer = "Esc back"
        geo = self._geo = self.geo_for(footer)
        body = jobcard.card_body(j, now=self.now, problem_files=self._problems,
                                 problems_scroll=self.problems_scroll, geo=geo)
        return screen(jobcard.card_title(j), body, right,
                      footer=footer, width=geo.w, height=geo.h)

    def action_scroll(self, delta: int) -> None:
        # ↑/↓ scroll the problem-file window (no-op when the card has none).
        budget = jobcard.problem_budget(self.job, self._problems, self._geo)
        max_scroll = max(0, len(self._problems) - budget)
        new = max(0, min(self.problems_scroll + delta, max_scroll))
        if new != self.problems_scroll:
            self.problems_scroll = new
            self.refresh_frame()

    def _back(self) -> None:
        """Pop this card back to the interface that opened it (§5).

        Every JobCard action reports via a toast (``run_verb``/``confirm_verb``) and
        then returns here so the user lands back on the screen they came from — the
        root detail's Jobs panel or the Queue — rather than staring at a now-stale
        card. Guarded on ``is_active`` so a stray key on a lower screen can't pop the
        wrong screen, and re-checked because the card may already be gone."""
        if self.is_active and self.app.screen_stack:
            self.app.pop_screen()

    def action_cancel(self) -> None:
        if self.job.get("status") == "running":
            jid = self.job["id"]
            self.app.confirm_verb(f"Cancel running {self.job['label']} (#{jid})?",
                                  f"packrat jobs cancel {jid}",
                                  submit=lambda: self.app.client.cancel_job(jid),
                                  then=self._back)

    def _reviewable(self) -> bool:
        """Open/cancel apply while the run is still open — this card's stage (``current``)
        or a later one it advanced to (``advanced``). Confirm is ``current``-only."""
        return self._review_ui() in ("current", "advanced")

    def action_open_review(self) -> None:
        if self._reviewable():
            root = self.job.get("root_name", "")
            path = self.app.root_path(root)
            submit = (lambda: _open_in_explorer(path)) if path else None
            target = f"{path}\\_packrat_review\\" if path else f"<{root} review folder>"
            self.app.run_verb(f"explorer {target}", title="open in Explorer",
                              submit=submit, then=self._back)

    def _review_network_count(self, root: str) -> int:
        """How many of the pending review's current-stage delete candidates sit on a
        network share (permanent delete — §10). Read from the live root detail; any
        failure → 0 (no warning, never blocks the confirm)."""
        try:
            detail, _ = self.app.root_detail(root)
            return int(((detail or {}).get("pending_review") or {}).get("counts", {}).get("network", 0))
        except Exception:  # noqa: BLE001 - the warning is best-effort
            return 0

    def action_confirm_review(self) -> None:
        if self._pending():
            root = self.job.get("root_name", "")
            verb = self._verb()
            self.app.confirm_verb(f"Confirm this {verb} stage for {root}?",
                                  f"packrat {verb} {root} --confirm",
                                  network=self._review_network_count(root),
                                  submit=lambda: self._submit_review(root, confirm=True),
                                  then=self._back)

    def action_cancel_review(self) -> None:
        if self._reviewable():
            root = self.job.get("root_name", "")
            verb = self._verb()
            self.app.confirm_verb(f"Cancel the whole {verb} run for {root}?",
                                  f"packrat {verb} {root} --cancel",
                                  submit=lambda: self._submit_review(root, cancel=True),
                                  then=self._back)


# ---------------------------------------------------------------------------
# the app
# ---------------------------------------------------------------------------
class PackratApp(App):
    """The packrat TUI application (§12).

    Holds the live read-model state (refreshed on a poll timer + job-finished SSE)
    and the daemon client. ``offline`` mode renders from :mod:`packrat.tui.fixtures`
    so the UI is runnable/demoable without a daemon (and drives the golden tests).
    """

    CSS_PATH = "packrat.tcss"
    # Ctrl-Q is the hard quit (priority=True → fires from ANY screen/widget, incl. a
    # modal or a focused Input). We deliberately do NOT bind Ctrl-C: Windows Terminal
    # sends Ctrl+Shift+C (copy) as the same byte as Ctrl+C, so binding Ctrl+C would
    # hijack the copy shortcut. Esc backs out / quits from the top screen (per-screen
    # `escape` bindings); Ctrl-Q is the anywhere-quit.
    BINDINGS = [Binding("ctrl+q", "quit", "quit", show=False, priority=True)]

    def __init__(self, *, client=None, offline: bool = False, now: str | None = None):
        # ansi_color=True disables Textual's ANSIToTruecolor filter, which would
        # otherwise rewrite our `background: ansi_default` (the ansi=-1 sentinel)
        # into a concrete opaque RGB fill. With the filter off, the transparent
        # background is emitted as `\x1b[49m` (reset-to-terminal-default), so the
        # terminal's own acrylic/"glass" background shows through (§12 chrome).
        super().__init__(ansi_color=True)
        self.client = client
        self.offline = offline or client is None
        # `now` drives every relative time (reltime): last-scan, job ages, card headers.
        # ONLINE it must track the wall clock (`_now=None` → the property returns live
        # now_iso()); a FIXED value is used only when explicitly pinned (tests) or in the
        # offline demo, whose sample timestamps are relative to fixtures.REFERENCE_NOW.
        # (Regression: defaulting to REFERENCE_NOW online froze the clock at a fixture
        # date, so a just-finished job rendered as a future calendar date.)
        if now is not None:
            self._now = now
        elif self.offline:
            self._now = fixtures.REFERENCE_NOW
        else:
            self._now = None   # live wall clock (see the `now` property)
        # A COMPLETE zeroed snapshot, not `{}` — the pure builders index required keys
        # directly, so an empty dict would KeyError before the first fetch / when the
        # daemon is down (the fallback client at run() exists precisely for that case).
        self.snapshot: dict = _empty_snapshot()
        self.recent: list[dict] = []
        self.header_right = "daemon ● up"
        # Live-progress plumbing (§3 SSE + §cross-cutting TUI-side ETA). The poll timer
        # is only the backstop; the running job's bar/ETA are driven by an SSE stream
        # (`_stream_running`) whose samples feed `_eta`. `_streamed_job_id`/`_stream_alive`
        # guard against double-subscribing and let a dropped stream reconnect (§3).
        self._eta = EtaEstimator()
        self._streamed_job_id: int | None = None
        self._stream_alive = False
        self._last_stream_render = 0.0     # coalesces per-file SSE repaints (see below)

    @property
    def now(self) -> str:
        """The reference 'now' for relative-time rendering (§12).

        A pinned value (tests) or the offline demo's fixed reference when set;
        otherwise the LIVE wall clock, so online timestamps age against real time.
        """
        if self._now is not None:
            return self._now
        from ..util import now_iso
        return now_iso()

    def on_mount(self) -> None:
        self.refresh_data()
        self.push_screen(Dashboard())
        if not self.offline:
            from .tokens import POLL_INTERVAL_S
            self.set_interval(POLL_INTERVAL_S, self.refresh_data)

    # -- data ---------------------------------------------------------------
    def refresh_data(self) -> None:
        """Re-fetch the snapshot + recent jobs (poll backstop / job-finished trigger).

        ONLINE the fetch is two blocking httpx calls, so it runs in a WORKER THREAD
        (:meth:`_fetch_online`) and marshals the result back to the UI thread — a slow
        or hung daemon must never freeze keyboard input / rendering on the poll timer.
        OFFLINE (in-memory demo data, no I/O) applies inline. Tests that call this
        directly on a running app get the async worker; the synchronous apply path
        (:meth:`_apply_data`) is what actually mutates state + re-renders."""
        if self.offline:
            self._apply_data(demo.status_snapshot(running=True), demo.recent_jobs(),
                             "v0.1.0 · daemon ● up")
            return
        # Online: fetch off the UI thread. If no app loop is running (a bare unit test
        # driving refresh_data on an un-mounted app), fall back to a synchronous fetch.
        if self._app_loop_running():
            self._fetch_online()
        else:
            self._apply_data(*self._blocking_fetch())

    def _app_loop_running(self) -> bool:
        """True when the Textual event loop is up (so worker threads can marshal back)."""
        try:
            return bool(self.is_running)   # Textual App: True while run()/run_test() is active
        except Exception:  # noqa: BLE001 - be conservative: no loop → synchronous path
            return False

    def _blocking_fetch(self) -> tuple[dict, list, str]:
        """The blocking daemon fetch (status + recent jobs). Returns (snapshot, recent,
        header_right); degrades to a zeroed snapshot + 'down' header if unreachable."""
        try:
            return self.client.status(), self.client.list_jobs(20), "v0.1.0 · daemon ● up"
        except Exception:
            # Daemon unreachable/erroring: a zeroed snapshot (so the frame still draws)
            # + a 'down' header — never crash / leave a partial dict a builder KeyErrors on.
            return _empty_snapshot(), [], "v0.1.0 · daemon ○ down"

    @work(thread=True, exclusive=True, group="poll-fetch")
    def _fetch_online(self) -> None:
        """Worker-thread poll fetch (blocking httpx), applied back on the UI thread."""
        snap, recent, header = self._blocking_fetch()
        try:
            self.call_from_thread(self._apply_data, snap, recent, header)
        except Exception:
            pass   # app tearing down

    def _apply_data(self, snapshot: dict, recent: list, header_right: str) -> None:
        """Install a freshly-fetched read-model + re-render (UI thread only)."""
        self.snapshot = snapshot
        self.recent = recent
        self.header_right = header_right
        # Feed the SSE-less poll path into the ETA estimator + keep the live stream
        # subscribed to whatever job is now running (fix: the "live" bar was poll-only).
        self._track_running()
        # Re-render the top screen if it's mounted (detail screens reload their own
        # per-root data on the poll, NOT on every keypress — see FrameScreen.poll_reload).
        if self.screen_stack and isinstance(self.screen, FrameScreen):
            self.screen.poll_reload()
            self.screen.refresh_frame()

    # -- live progress: SSE stream + TUI-side ETA (§3 / §cross-cutting) ------
    def _track_running(self) -> None:
        """Keep the live SSE stream attached to the current running job + inject ETA.

        Called on every poll refresh. **Online only** — offline demo/fixture jobs carry
        a fixed ``_eta_s`` in the sample data and need no stream/estimator. When the
        running job changes (or none runs) the estimator resets; otherwise the poll
        sample folds in and the derived ETA is written onto the running row so the pure
        builders (:func:`render.progress_bar`) render ``ETA …``. Starting the stream is
        idempotent (guarded by ``_stream_alive``); a dropped stream reconnects here."""
        if self.offline:
            return
        running = self.snapshot.get("running")
        if not running:
            self._streamed_job_id = None
            self._eta.reset()
            return
        jid = running.get("id")
        if jid != self._streamed_job_id:      # a new running job → fresh estimate + stream
            self._streamed_job_id = jid
            self._eta.reset()
            self._stream_alive = False
        self._observe(running)
        # Attach the live stream if the client supports SSE and none is attached. A
        # dropped/failed stream clears `_stream_alive`, so the NEXT poll re-attaches
        # (§3 reconnect) — the poll cadence is the backoff, no tight retry loop.
        if not self._stream_alive and jid is not None and hasattr(self.client, "stream_job"):
            self._stream_running(jid)

    def _observe(self, running: dict) -> None:
        """Fold one ``done`` sample into the estimator + stamp the derived ETA on the row."""
        done = running.get("done")
        if done is not None:
            self._eta.observe(time.monotonic(), done)
        running["_eta_s"] = self._eta.eta_s(running.get("total"))

    @work(thread=True, exclusive=True, group="job-stream")
    def _stream_running(self, job_id: int) -> None:
        """Subscribe to the running job's SSE stream, pushing live progress + ETA.

        Runs in a worker thread — ``client.stream_job`` is a blocking httpx generator.
        Each progress/state event updates the running row's ``done``/``total`` + the
        TUI-side ETA and re-renders on the UI thread (via :meth:`call_from_thread`); a
        terminal event triggers a full refetch so history/result cards appear. A dropped
        or unreachable stream just ends the worker — the poll backstop re-attaches (§3,
        job state is durable). ``exclusive`` cancels any prior stream in the group."""
        self._stream_alive = True
        finished = False                      # True only on a clean terminal event
        try:
            for ev in self.client.stream_job(job_id):
                if self._streamed_job_id != job_id:
                    break                     # a newer job took over — let this stream die
                etype = ev.get("type")
                if etype in ("progress", "state") and ev.get("done") is not None:
                    self.call_from_thread(self._apply_stream_progress, job_id, ev)
                if etype in ("done", "error") or ev.get("status") in (
                        "done", "error", "cancelled", "interrupted"):
                    finished = True
                    break
        except Exception:
            pass                              # dropped / daemon gone → poll reconnects
        finally:
            self._stream_alive = False
            # Refetch immediately ONLY on a clean job-finished event (history/result
            # card appear at once). A mid-stream DROP does NOT refetch here — that would
            # re-attach instantly and tight-loop if the daemon keeps erroring; the poll
            # timer reconnects on its own cadence (§3 durable state).
            if finished:
                try:
                    self.call_from_thread(self.refresh_data)
                except Exception:
                    pass                      # app tearing down

    def _apply_stream_progress(self, job_id: int, ev: dict) -> None:
        """Fold one SSE progress event into the running row + re-render (UI thread).

        The in-memory counters + ETA update on EVERY event (no data lost), but the
        repaint is COALESCED to ``STREAM_RENDER_INTERVAL_S`` — a scan fires one event
        per file (hundreds/sec), and re-laying-out the whole frame that often is what
        made the TUI laggy (issue #1). Between repaints the next poll tick still shows
        the latest value, so nothing stalls visually."""
        running = self.snapshot.get("running")
        if not running or running.get("id") != job_id:
            return
        if ev.get("done") is not None:
            running["done"] = ev["done"]
        if ev.get("total") is not None:
            running["total"] = ev["total"]
        self._observe(running)
        from .tokens import STREAM_RENDER_INTERVAL_S
        now = time.monotonic()
        if now - self._last_stream_render < STREAM_RENDER_INTERVAL_S:
            return                            # coalesce: skip this repaint, data already updated
        self._last_stream_render = now
        if self.screen_stack and isinstance(self.screen, FrameScreen):
            self.screen.refresh_frame()

    def sorted_roots(self) -> list[dict]:
        from . import render
        return render.sort_roots(self.snapshot.get("roots", []), 0)

    def root_path(self, name: str) -> str | None:
        """The on-disk path of a root by name (from the current snapshot), or None."""
        for r in self.snapshot.get("roots", []):
            if r.get("name") == name:
                return r.get("path")
        return None

    def _root_kind(self, name: str) -> str | None:
        """The ``kind`` (library|trash) of a root by name (current snapshot), or None."""
        for r in self.snapshot.get("roots", []):
            if r.get("name") == name:
                return r.get("kind")
        return None

    def open_root(self, name: str) -> None:
        """Open a root the right way for its kind (§6.1 — trash has no detail screen).

        A **library** root opens its RootDetailScreen (scan/dedup/merge/cleanup). A
        **trash** root has no detail — its only meaningful action is *refresh the
        collection* — so it opens the packrat-with-a-trash-can confirm modal instead;
        confirming maps to ``packrat trash refresh <root>``. Centralized so every
        entry point (Dashboard roots box, RootsMax list) treats trash roots alike."""
        if self._modal_on_top():
            return
        if self._root_kind(name) == "trash":
            self._confirm_trash_refresh(name)
        else:
            self.push_screen(RootDetailScreen(name))

    def _confirm_trash_refresh(self, name: str) -> None:
        """Push the trash mascot modal; on [y] submit ``trash refresh <name>`` (§6.1)."""
        def after(ok):
            if ok:
                self.run_verb(f"packrat trash refresh {name}", title="trash refresh",
                              submit=lambda: self.client.submit_trash_refresh(name))

        self.push_screen(TrashRefreshModal(name), after)

    def root_detail(self, name: str):
        """Return ``(detail_dict, jobs)`` for a root by name (offline → demo)."""
        if self.offline:
            return demo.root_detail(name), demo.root_jobs(name)
        try:
            d = self.client.status(name).get("root_detail")
            jobs = self.client.root_jobs(d["id"]) if d else []
            if d is not None:
                self._inject_live_progress(d.get("running_job"))
            return d, jobs
        except Exception:
            return None, []

    def _inject_live_progress(self, job: dict | None) -> None:
        """Copy the live ``done``/``total``/``_eta_s`` onto a per-view running-job dict.

        The SSE stream + TUI-side ETA are tracked against ``snapshot["running"]`` only;
        the root-detail view fetches its ``running_job`` from a *separate* daemon call
        that never carries the estimate. When it's the SAME job we're streaming, mirror
        the freshest counters + ETA onto it so root detail shows the same live bar/ETA
        as the dashboard/Queue (not a stale, ETA-less snapshot)."""
        if not job:
            return
        live = self.snapshot.get("running")
        if live and live.get("id") == job.get("id"):
            for k in ("done", "total", "_eta_s"):
                if live.get(k) is not None:
                    job[k] = live[k]

    def job_problem_files(self, job: dict) -> list[dict]:
        """A scan job's undecodable/read-error files (paths + reasons, §12 card)."""
        if self.offline:
            return demo.job_problem_files(job["id"])
        try:
            return self.client.job_problem_files(job["id"])
        except Exception:
            return []

    # -- actions (§1.6: every action maps to a CLI verb) --------------------
    def _modal_on_top(self) -> bool:
        """True if a Modal is already the active screen — the re-entrancy guard.

        A background screen's key binding can still fire while a modal is open (a
        modal only *focuses* its own widgets; Textual bubbles keys it doesn't bind
        down the stack). If that background action pushed another modal, rapid
        dismissal underflows the stack (the ``No screens on stack`` crash / hang).
        Refusing to push a second modal here is the single, driver-safe choke point.
        """
        from .modals import Modal
        try:
            return isinstance(self.screen, Modal)
        except Exception:
            return False

    def run_verb(self, cmd: str, *, title: str = "would run", submit=None,
                 then=None) -> None:
        """Run an action that maps to a CLI verb (§1.6), reporting via a **toast**.

        ``cmd`` is the human display string (the CLI verb it corresponds to).
        ``submit`` is a zero-arg callable that performs the real daemon call
        (``client.submit_*``) and returns a job id; pass it for actions that
        actually submit work. ``then`` is an optional zero-arg callable fired right
        after the toast is posted — e.g. the JobCard passes ``self._back`` so an
        action pops the card back to the interface that opened it (§5).

        These are the actions that **do not need a confirmation** (a confirm-gated
        action goes through :meth:`confirm_verb` → the ConfirmModal first). So the
        result is a non-blocking Textual toast, never a modal popup:
        - **offline** demo: no daemon → an info toast showing ``cmd`` (the walkable
          "what this would run"); ``submit`` is not called.
        - **online**: call ``submit()`` and show an info toast "submitted — job #N",
          then refresh so the new job appears; on an exception, a **red error toast**
          (the command failed to even submit) instead of crashing.

        **Modal guard:** a background screen's action key can bubble down the stack and
        reach here while a modal is open (Textual only *focuses* the modal's widgets),
        which would fire a real daemon submit underneath the modal. Refuse when a modal
        is on top. (A confirm-gated action is safe: :meth:`confirm_verb` calls this from
        its post-dismiss callback, when no modal is on top.)
        """
        if self._modal_on_top():
            return
        if self.offline or submit is None:
            self.notify(cmd, title=title, severity="information")
            if then:
                then()
            return
        try:
            job_id = submit()
        except Exception as exc:
            self.notify(f"{cmd}\n{exc}", title="couldn't run", severity="error")
            if then:
                then()
            return
        self.refresh_data()
        note = f"submitted — job #{job_id}" if job_id else "submitted"
        self.notify(f"{cmd}\n{note} — watch it in the Queue.",
                    title="submitted", severity="information")
        if then:
            then()

    def confirm_verb(self, question: str, cmd: str, *, count: int | None = None,
                     network: int = 0, submit=None, then=None) -> None:
        """Confirm (y/n or typed-count), then run the verb (§1.6 — gather then act).

        On confirm, delegates to :meth:`run_verb` (offline → notice; online →
        ``submit()`` + result notice). ``submit`` is the real daemon call; ``then``
        is forwarded to :meth:`run_verb` and so fires only after a confirmed action's
        toast — declining (``n``) posts no toast and runs no ``then``.
        """
        if self._modal_on_top():
            return
        # The chained action (after confirm) runs once THIS modal has dismissed —
        # so run_verb there sees no modal on top and is allowed (ask→act sequence).
        def after(ok):
            if ok:
                self.run_verb(cmd, title="confirmed", submit=submit, then=then)

        self.push_screen(ConfirmModal(question, count=count, network=network), after)


def run(*, offline: bool = False) -> None:
    """Launch the TUI (the ``packrat`` no-args entrypoint, §12)."""
    client = None
    if not offline:
        from ..daemon.client import DaemonClient
        from ..daemon.spawn import ensure_daemon
        try:
            client = ensure_daemon()
        except Exception:
            client = DaemonClient()  # render daemon-down state rather than crash
    PackratApp(client=client, offline=offline).run()
