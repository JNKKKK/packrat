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

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static

from . import demo, fixtures
from .colorize import colorize
from .data import reltime
from .framing import screen
from .modals import ConfirmModal, MessageModal
from .nav import DashboardFocus
from .screens import jobcard
from .screens.dashboard import (
    QUEUE_PREVIEW_ROWS,
    ROOTS_PREVIEW_ROWS,
    dashboard_body,
    queue_preview_pages,
)
from .screens.queue import QUEUED_ROWS, RECENT_ROWS, queue_body
from .screens.queue import section_jobs as q_section_jobs
from .screens.queue import section_pages as q_section_pages
from .screens.rootdetail import JOBS_ROWS, detail_body, detail_header_right
from .screens.roots import ADD_ROOT_FIELDS, LIST_ROWS, add_root_body, roots_body


def _review_verb(pending: dict) -> str:
    """The CLI verb that confirms/cancels a pending review run (dedup vs cleanup)."""
    return "cleanup" if pending.get("run_type") == "cleanup-perceptual" else "dedup"


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

    def refresh_frame(self) -> None:
        self.current_frame = self.frame()      # PLAIN string (tests / snapshotting)
        # Colorize post-layout (§Theming): the plain frame stays the source of
        # truth; only the live widget gets theme role colors applied by pattern.
        self.query_one("#frame", Static).update(colorize(self.current_frame))

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
            "↑/↓ select  [Enter] detail  ←/→ page  [c] cancel  [p] prioritize  [x] all  Esc"
            if fs.target == "queue"
            else "↑/↓ select root   [Enter] open detail   ←/→ page   [r] maximize   Esc unfocus"
            if fs.target == "roots"
            else "[r] focus Roots   [q] focus Queue (again = maximize)   Ctrl-C quit"
        )
        body = dashboard_body(
            self.app.snapshot, now=self.now, focus=fs.target,
            roots_cursor=fs.roots_cursor, roots_page=self.roots_page,
            queue_cursor=fs.queue_cursor, queue_page=self.queue_page,
        )
        return screen("packrat", body, self.app.header_right, footer=footer)

    def action_page(self, delta: int) -> None:
        # ←/→ pages the focused box and moves the cursor to the FIRST item on the
        # new page (so the ▸ is never left behind on the previous page). Both the
        # Roots and Queue boxes page in place; the full backlog is also in §4.
        self._sync_lens()
        fs = self.focus_state
        if fs.target == "roots":
            pages = max(1, -(-fs.roots_len // ROOTS_PREVIEW_ROWS))
            new = max(0, min(self.roots_page + delta, pages - 1))
            if new != self.roots_page:
                self.roots_page = new
                fs.roots_cursor = min(new * ROOTS_PREVIEW_ROWS, max(0, fs.roots_len - 1))
            self.refresh_frame()
        elif fs.target == "queue":
            pages = queue_preview_pages(self.app.snapshot)
            new = max(0, min(self.queue_page + delta, pages - 1))
            if new != self.queue_page:
                self.queue_page = new
                fs.queue_cursor = min(new * QUEUE_PREVIEW_ROWS, max(0, fs.queue_len - 1))
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
        self.focus_state.move(delta)
        # keep the focused box's page in sync with its cursor (auto-follow)
        if self.focus_state.target == "roots":
            self.roots_page = self.focus_state.roots_cursor // ROOTS_PREVIEW_ROWS
        elif self.focus_state.target == "queue":
            self.queue_page = self.focus_state.queue_cursor // QUEUE_PREVIEW_ROWS
        self.refresh_frame()

    def action_unfocus(self) -> None:
        if self.focus_state.escape():
            self.refresh_frame()

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
                self.app.push_screen(RootDetailScreen(roots[fs.roots_cursor]["name"]))
        elif fs.target == "queue":
            job = self._selected_queue_job()
            if job:
                self.app.push_screen(JobCard(job))

    def action_cancel(self) -> None:
        job = self._selected_queue_job()
        if job and job.get("status") in ("queued", "running"):
            self.app.confirm_verb(f"Cancel {job['label']} (#{job['id']})?",
                                  f"packrat jobs cancel {job['id']}")

    def action_prioritize(self) -> None:
        job = self._selected_queue_job()
        if job and job.get("status") == "queued":   # only a queued job can be prioritized
            self.app.run_verb(f"packrat jobs prioritize {job['id']}")

    def action_cancel_all(self) -> None:
        if self.focus_state.target == "queue" and self.app.snapshot.get("queued"):
            n = len(self.app.snapshot["queued"])
            self.app.confirm_verb(f"Cancel all {n} queued job(s)?",
                                  "packrat jobs cancel --all-queued")


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

    def frame(self) -> str:
        body = roots_body(self.app.snapshot.get("roots", []), now=self.now,
                          sort_mode=self.sort_mode, cursor=self.cursor, page=self.page)
        return screen("packrat · Roots", body, self.app.header_right,
                      footer="↑/↓ select   [Enter] open detail   ←/→ page   "
                             "[s] sort   [a] add root   Esc back")

    def action_sort(self) -> None:
        self.sort_mode = (self.sort_mode + 1) % 4
        self.cursor = 0
        self.page = 0
        self.refresh_frame()

    def action_move(self, delta: int) -> None:
        n = len(self._ordered())
        self.cursor = max(0, min(self.cursor + delta, n - 1)) if n else 0
        self.page = self.cursor // LIST_ROWS       # keep the cursor on-page
        self.refresh_frame()

    def action_page(self, delta: int) -> None:
        n = len(self._ordered())
        pages = max(1, -(-n // LIST_ROWS))
        new = max(0, min(self.page + delta, pages - 1))
        if new != self.page:                       # move cursor to the new page's first item
            self.page = new
            self.cursor = min(new * LIST_ROWS, max(0, n - 1))
        self.refresh_frame()

    def action_add(self) -> None:
        self.app.push_screen(AddRootScreen())

    def action_open(self) -> None:
        roots = self._ordered()
        if roots:
            self.app.push_screen(RootDetailScreen(roots[self.cursor]["name"]))


class AddRootScreen(FrameScreen):
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
        # Pre-filled sample so the demo form isn't blank; the fields are editable.
        self.path = r"E:\import\SDCard_2026"
        self.root_name = "SDCard2"
        self.kind = "library"     # toggled between library/trash on the Kind field
        self.scan = True          # toggled on the scan field
        self.field_idx = 0        # index into ADD_ROOT_FIELDS ([Tab] focus order)

    @property
    def _field(self) -> str:
        return ADD_ROOT_FIELDS[self.field_idx]

    def frame(self) -> str:
        body = add_root_body(path=self.path, name=self.root_name, kind=self.kind,
                             scan=self.scan, focus_field=self._field)
        return screen("packrat · Roots · add", body, self.app.header_right,
                      footer="[Tab] next field   [Space] toggle   type to edit   "
                             "[Enter] register   Esc cancel")

    # -- field navigation (§2.2) -------------------------------------------
    def action_next_field(self) -> None:
        self.field_idx = (self.field_idx + 1) % len(ADD_ROOT_FIELDS)
        self.refresh_frame()

    def action_prev_field(self) -> None:
        self.field_idx = (self.field_idx - 1) % len(ADD_ROOT_FIELDS)
        self.refresh_frame()

    def action_toggle(self) -> None:
        """[Space] toggles the focused choice field (Kind radio / scan checkbox)."""
        if self._field == "kind":
            self.kind = "trash" if self.kind == "library" else "library"
            self.refresh_frame()
        elif self._field == "scan":
            self.scan = not self.scan
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
            if self._field == "path":
                self.path += ch
            else:
                self.root_name += ch
            self.refresh_frame()
            event.stop()
        elif event.key == "space" and self._field in ("path", "name"):
            # space is a literal character in a text field (not the toggle binding)
            if self._field == "path":
                self.path += " "
            else:
                self.root_name += " "
            self.refresh_frame()
            event.stop()

    def action_register(self) -> None:
        parts = [f"packrat roots register {self.path}"]
        if self.root_name:
            parts.append(f"--name {self.root_name}")
        if self.kind == "trash":
            parts.append("--kind trash")
        elif self.scan:
            parts.append("--scan")
        self.app.run_verb(" ".join(parts), title="register root")


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
        Binding("s", "scan", show=False),
        Binding("d", "dedup", show=False),
        Binding("m", "merge", show=False),
        Binding("o", "open_review", show=False),
        Binding("g", "confirm_review", show=False),
        Binding("k", "cancel_review", show=False),
        Binding("escape", "app.pop_screen", show=False),
    ]

    def __init__(self, root_name: str) -> None:
        super().__init__()
        self.root_name = root_name
        self.cursor = 0
        self.page = 0
        self._jobs: list[dict] = []      # last-fetched jobs (refreshed in frame())
        self._detail: dict | None = None

    def frame(self) -> str:
        # One fetch per re-render; actions reuse `self._detail`/`self._jobs` rather
        # than re-hitting the daemon on every keypress (online → HTTP call).
        d, jobs = self.app.root_detail(self.root_name)
        self._detail, self._jobs = d, jobs
        if d is None:
            return screen("packrat · ?", ["root not found."], self.app.header_right,
                          footer="Esc back")
        body = detail_body(d, now=self.now, jobs=jobs, jobs_cursor=self.cursor, jobs_page=self.page)
        return screen(f"packrat · {d['name']}", body, detail_header_right(d),
                      footer="[s] scan  [d] dedup  [m] merge from…  [Enter] result  "
                             "↑/↓ jobs  ←/→ page  Esc")

    def action_move(self, delta: int) -> None:
        n = len(self._jobs)
        self.cursor = max(0, min(self.cursor + delta, n - 1)) if n else 0
        self.page = self.cursor // JOBS_ROWS
        self.refresh_frame()

    def action_page(self, delta: int) -> None:
        n = len(self._jobs)
        pages = max(1, -(-n // JOBS_ROWS))
        new = max(0, min(self.page + delta, pages - 1))
        if new != self.page:                       # cursor → first item of the new page
            self.page = new
            self.cursor = min(new * JOBS_ROWS, max(0, n - 1))
        self.refresh_frame()

    def action_result(self) -> None:
        if self._jobs:
            self.app.push_screen(JobCard(self._jobs[self.cursor]))

    # -- per-root ops (§3): each maps to a CLI verb (§1.6) ------------------
    def action_scan(self) -> None:
        self.app.run_verb(f"packrat scan {self.root_name}")

    def action_dedup(self) -> None:
        self.app.run_verb(f"packrat dedup {self.root_name}")

    def action_merge(self) -> None:
        # The full picker is §3.3; the demo surfaces the verb this root's merge maps to.
        self.app.run_verb(f"packrat merge <source> --into {self.root_name}",
                          title="merge from… (pick a source)")

    def _has_review(self) -> bool:
        return bool(self._detail and self._detail.get("pending_review"))

    def action_open_review(self) -> None:
        if self._has_review():
            path = self._detail["path"]
            self.app.run_verb(f"explorer {path}\\_packrat_review\\", title="open in Explorer")

    def action_confirm_review(self) -> None:
        if self._has_review():
            verb = _review_verb(self._detail["pending_review"])
            self.app.confirm_verb(f"Confirm this {verb} stage for {self.root_name}?",
                                  f"packrat {verb} {self.root_name} --confirm")

    def action_cancel_review(self) -> None:
        if self._has_review():
            verb = _review_verb(self._detail["pending_review"])
            self.app.confirm_verb(f"Cancel the whole {verb} run for {self.root_name}?",
                                  f"packrat {verb} {self.root_name} --cancel")


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
        Binding("e", "focus_section('recent')", show=False),
        Binding("enter", "detail", show=False),
        Binding("c", "cancel", show=False),
        Binding("p", "prioritize", show=False),
        Binding("x", "cancel_all", show=False),
        Binding("escape", "app.pop_screen", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.focus = "queued"       # focused section: running|queued|recent
        self.cursors = {"running": 0, "queued": 0, "recent": 0}
        self.pages = {"running": 0, "queued": 0, "recent": 0}

    # -- section data / sizing --------------------------------------------
    def _section_jobs(self, section: str) -> list[dict]:
        snap = self.app.snapshot
        return q_section_jobs(section, snap.get("running"), snap.get("queued", []),
                              self.app.recent)

    def _section_rows(self, section: str) -> int:
        return {"running": 1, "queued": QUEUED_ROWS, "recent": RECENT_ROWS}[section]

    def frame(self) -> str:
        snap = self.app.snapshot
        body = queue_body(
            snap.get("running"), snap.get("queued", []), self.app.recent, now=self.now,
            focus=self.focus,
            queued_cursor=self.cursors["queued"], queued_page=self.pages["queued"],
            recent_cursor=self.cursors["recent"], recent_page=self.pages["recent"],
            running_cursor=self.cursors["running"],
        )
        return screen("packrat · Queue", body, self.app.header_right,
                      footer="[r]/[q]/[e] section  ↑/↓ select  ←/→ page  [c] cancel  "
                             "[p] prioritize  [x] all  [Enter] detail  Esc")

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
            self.app.confirm_verb(f"Cancel {job['label']} (#{job['id']})?",
                                  f"packrat jobs cancel {job['id']}")

    def action_prioritize(self) -> None:
        job = self._selected()
        if job and job.get("status") == "queued":
            self.app.run_verb(f"packrat jobs prioritize {job['id']}")

    def action_cancel_all(self) -> None:
        queued = self.app.snapshot.get("queued", [])
        if queued:
            self.app.confirm_verb(f"Cancel all {len(queued)} queued job(s)?",
                                  "packrat jobs cancel --all-queued")


# ---------------------------------------------------------------------------
# Job result / detail card (§5)
# ---------------------------------------------------------------------------
class JobCard(FrameScreen):
    BINDINGS = [
        Binding("c", "cancel", show=False),
        Binding("o", "open_review", show=False),
        Binding("g", "confirm_review", show=False),
        Binding("k", "cancel_review", show=False),
        Binding("escape", "app.pop_screen", show=False),
    ]

    def __init__(self, job: dict) -> None:
        super().__init__()
        self.job = job

    def _pending(self) -> bool:
        import json
        try:
            return json.loads(self.job.get("result_json") or "{}").get("review_status") == "pending"
        except (ValueError, TypeError):
            return False

    def frame(self) -> str:
        j = self.job
        right = reltime(j.get("finished_at") or j.get("started_at"), self.now)
        if j.get("status") == "running":
            footer = "[c] cancel job   Esc back"
        elif self._pending():
            footer = "[o] open review   [g] confirm stage   [k] cancel run   Esc back"
        else:
            footer = "Esc back"
        return screen(jobcard.card_title(j), jobcard.card_body(j, now=self.now), right, footer=footer)

    def action_cancel(self) -> None:
        if self.job.get("status") == "running":
            self.app.confirm_verb(f"Cancel running {self.job['label']} (#{self.job['id']})?",
                                  f"packrat jobs cancel {self.job['id']}")

    def action_open_review(self) -> None:
        if self._pending():
            self.app.run_verb(f"explorer <{self.job.get('root_name', 'root')} review folder>",
                              title="open in Explorer")

    def action_confirm_review(self) -> None:
        if self._pending():
            root = self.job.get("root_name", "")
            self.app.confirm_verb(f"Confirm this dedup stage for {root}?",
                                  f"packrat dedup {root} --confirm")

    def action_cancel_review(self) -> None:
        if self._pending():
            root = self.job.get("root_name", "")
            self.app.confirm_verb(f"Cancel the whole dedup run for {root}?",
                                  f"packrat dedup {root} --cancel")


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
    # priority=True so Ctrl-C quits from ANY screen/widget — including a modal or a
    # focused Input (the count-confirm field). Without priority, a non-priority
    # binding on the app is shadowed by the focused widget / modal, so Ctrl-C is
    # swallowed and the app hangs unquittable (the reported blank-terminal bug).
    BINDINGS = [Binding("ctrl+c", "quit", "quit", show=False, priority=True)]

    def __init__(self, *, client=None, offline: bool = False, now: str | None = None):
        # ansi_color=True disables Textual's ANSIToTruecolor filter, which would
        # otherwise rewrite our `background: ansi_default` (the ansi=-1 sentinel)
        # into a concrete opaque RGB fill. With the filter off, the transparent
        # background is emitted as `\x1b[49m` (reset-to-terminal-default), so the
        # terminal's own acrylic/"glass" background shows through (§12 chrome).
        super().__init__(ansi_color=True)
        self.client = client
        self.offline = offline or client is None
        self._now = now or fixtures.REFERENCE_NOW
        self.snapshot: dict = {}
        self.recent: list[dict] = []
        self.header_right = "daemon ● up"

    @property
    def now(self) -> str:
        return self._now

    def on_mount(self) -> None:
        self.refresh_data()
        self.push_screen(Dashboard())
        if not self.offline:
            from .tokens import POLL_INTERVAL_S
            self.set_interval(POLL_INTERVAL_S, self.refresh_data)

    # -- data ---------------------------------------------------------------
    def refresh_data(self) -> None:
        """Re-fetch the snapshot + recent jobs (poll backstop / job-finished trigger)."""
        if self.offline:
            # The offline demo uses the rich `demo` dataset (multi-page lists +
            # every job shape) rather than the mockup-exact `fixtures`, so a person
            # can exercise pagination and every screen without a daemon.
            self.snapshot = demo.status_snapshot(running=True)
            self.recent = demo.recent_jobs()
            self.header_right = "v0.1.0 · daemon ● up"
        else:
            try:
                self.snapshot = self.client.status()
                self.recent = self.client.list_jobs(20)
                self.header_right = "v0.1.0 · daemon ● up"
            except Exception:
                self.header_right = "v0.1.0 · daemon ○ down"
        # Re-render the top screen if it's mounted.
        if self.screen_stack and isinstance(self.screen, FrameScreen):
            self.screen.refresh_frame()

    def sorted_roots(self) -> list[dict]:
        from . import render
        return render.sort_roots(self.snapshot.get("roots", []), 0)

    def root_detail(self, name: str):
        """Return ``(detail_dict, jobs)`` for a root by name (offline → demo)."""
        if self.offline:
            return demo.root_detail(name), demo.root_jobs(name)
        try:
            d = self.client.status(name).get("root_detail")
            jobs = self.client.root_jobs(d["id"]) if d else []
            return d, jobs
        except Exception:
            return None, []

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

    def run_verb(self, cmd: str, *, title: str = "would run") -> None:
        """Surface the CLI verb an action maps to.

        In **offline** demo mode this opens a notice modal ("would run: <cmd>") so
        the full action UX is walkable without a daemon (no state mutates). Online,
        this is where the real ``client.submit_*`` call goes (wired per action).
        """
        if self._modal_on_top():
            return
        if self.offline:
            self.push_screen(MessageModal(cmd, title=title, footer="[Enter] ok  ·  demo (no-op)"))
        # else: online submit is wired per-action on the screens (deferred).

    def confirm_verb(self, question: str, cmd: str, *, count: int | None = None,
                     network: int = 0) -> None:
        """Confirm (y/n or typed-count) then surface the CLI verb it maps to.

        The modal only *gathers input* (§1.6); on confirm the demo shows the verb.
        """
        if self._modal_on_top():
            return
        # The chained notice (after confirm) is pushed from `after`, which runs
        # once THIS modal has dismissed — so `run_verb` there sees no modal on top
        # and is allowed (a legitimate ask→act sequence, not a re-entrant push).
        def after(ok):
            if ok:
                self.run_verb(cmd, title="confirmed")

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
