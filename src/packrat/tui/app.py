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
from .geometry import REF_H, REF_W, Geometry
from .layout import wrap_hints
from .modals import ChoiceModal, ConfirmModal, MessageModal
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
                self.app.push_screen(RootDetailScreen(roots[fs.roots_cursor]["name"]))
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
        footer = ("[Tab] next field   [Space] toggle   type to edit   "
                  "[Enter] register   Esc cancel")
        geo = self._geo = self.geo_for(footer)
        body = add_root_body(path=self.path, name=self.root_name, kind=self.kind,
                             scan=self.scan, focus_field=self._field, geo=geo)
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

    def action_register(self) -> None:
        parts = [f"packrat roots register {self.path}"]
        if self.root_name:
            parts.append(f"--name {self.root_name}")
        if self.kind == "trash":
            parts.append("--kind trash")
        elif self.scan:
            parts.append("--scan")
        path, name, kind, scan = self.path, self.root_name, self.kind, self.scan

        def submit():
            # register_root returns {root, job_id}; report the scan job id if any.
            resp = self.app.client.register_root(
                path, name=name or None, kind=kind, scan=(scan and kind == "library"))
            return resp.get("job_id")

        self.app.run_verb(" ".join(parts), title="register root", submit=submit)


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
        Binding("s", "scan", show=False),
        Binding("d", "dedup", show=False),
        Binding("m", "merge", show=False),
        Binding("c", "cleanup", show=False),
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

    FOOTER = ("[s] scan  [d] dedup  [m] merge from…  [c] clean up  [Enter] result  "
              "↑/↓ jobs  ←/→ page  Esc")

    # The three cleanup modes (§6.2) offered by [c]; label → CLI flag. Labels kept
    # short enough to fit the choice modal (≤ ~54 cells) without wrapping.
    CLEANUP_MODES = [
        ("trash-exact  (delete byte-identical trash)", "--trash-exact"),
        ("trash-perceptual  (stage recompressed trash)", "--trash-perceptual"),
        ("undecodable  (delete non-decoding files)", "--undecodable"),
    ]

    def frame(self) -> str:
        # One fetch per re-render; actions reuse `self._detail`/`self._jobs` rather
        # than re-hitting the daemon on every keypress (online → HTTP call).
        geo = self._geo = self.geo_for(self.FOOTER)
        d, jobs = self.app.root_detail(self.root_name)
        self._detail, self._jobs = d, jobs
        if d is None:
            return screen("packrat · ?", ["root not found."], self.app.header_right,
                          footer="Esc back", width=geo.w, height=geo.h)
        body = detail_body(d, now=self.now, geo=geo, jobs=jobs,
                          jobs_cursor=self.cursor, jobs_page=self.page)
        return screen(f"packrat · {d['name']}", body, detail_header_right(d),
                      footer=self.FOOTER, width=geo.w, height=geo.h)

    def action_move(self, delta: int) -> None:
        n = len(self._jobs)
        self.cursor = max(0, min(self.cursor + delta, n - 1)) if n else 0
        self.page = self.cursor // self._geo.jobs_rows
        self.refresh_frame()

    def action_page(self, delta: int) -> None:
        rows = self._geo.jobs_rows
        n = len(self._jobs)
        pages = max(1, -(-n // rows))
        new = max(0, min(self.page + delta, pages - 1))
        if new != self.page:                       # cursor → first item of the new page
            self.page = new
            self.cursor = min(new * rows, max(0, n - 1))
        self.refresh_frame()

    def action_result(self) -> None:
        if self._jobs:
            self.app.push_screen(JobCard(self._jobs[self.cursor]))

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
            self.app.run_verb(f"packrat cleanup {root} {flag}", title="clean up",
                              submit=lambda: self.app.client.submit_cleanup(root, mode=mode))

        self.app.push_screen(ChoiceModal(options, title=f"clean up {root}"), after)

    def _has_review(self) -> bool:
        return bool(self._detail and self._detail.get("pending_review"))

    def action_open_review(self) -> None:
        if self._has_review():
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
        if self._has_review():
            verb = _review_verb(self._detail["pending_review"])
            root = self.root_name
            self.app.confirm_verb(f"Confirm this {verb} stage for {root}?",
                                  f"packrat {verb} {root} --confirm",
                                  submit=lambda: self._submit_review(verb, root, confirm=True))

    def action_cancel_review(self) -> None:
        if self._has_review():
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

    # Full natural wording — wraps to 2 lines on a narrow terminal (wrap_hints),
    # one line on a wide one. No hand-trimming to fit 100 cols.
    FOOTER = ("[r]/[q]/[e] section   ↑/↓ select   ←/→ page   [c] cancel   "
              "[p] prioritize   [x] cancel all   [Enter] detail   Esc back")

    def _section_rows(self, section: str) -> int:
        geo = self._geo
        return {"running": 1, "queued": geo.queued_rows, "recent": geo.recent_rows}[section]

    def frame(self) -> str:
        geo = self._geo = self.geo_for(self.FOOTER)
        snap = self.app.snapshot
        body = queue_body(
            snap.get("running"), snap.get("queued", []), self.app.recent, now=self.now,
            geo=geo, focus=self.focus,
            queued_cursor=self.cursors["queued"], queued_page=self.pages["queued"],
            recent_cursor=self.cursors["recent"], recent_page=self.pages["recent"],
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
        geo = self._geo = self.geo_for(footer)
        return screen(jobcard.card_title(j), jobcard.card_body(j, now=self.now), right,
                      footer=footer, width=geo.w, height=geo.h)

    def action_cancel(self) -> None:
        if self.job.get("status") == "running":
            jid = self.job["id"]
            self.app.confirm_verb(f"Cancel running {self.job['label']} (#{jid})?",
                                  f"packrat jobs cancel {jid}",
                                  submit=lambda: self.app.client.cancel_job(jid))

    def action_open_review(self) -> None:
        if self._pending():
            root = self.job.get("root_name", "")
            path = self.app.root_path(root)
            submit = (lambda: _open_in_explorer(path)) if path else None
            target = f"{path}\\_packrat_review\\" if path else f"<{root} review folder>"
            self.app.run_verb(f"explorer {target}", title="open in Explorer", submit=submit)

    def action_confirm_review(self) -> None:
        if self._pending():
            root = self.job.get("root_name", "")
            self.app.confirm_verb(f"Confirm this dedup stage for {root}?",
                                  f"packrat dedup {root} --confirm",
                                  submit=lambda: self.app.client.submit_dedup(root, confirm=True))

    def action_cancel_review(self) -> None:
        if self._pending():
            root = self.job.get("root_name", "")
            self.app.confirm_verb(f"Cancel the whole dedup run for {root}?",
                                  f"packrat dedup {root} --cancel",
                                  submit=lambda: self.app.client.submit_dedup(root, cancel=True))


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

    def root_path(self, name: str) -> str | None:
        """The on-disk path of a root by name (from the current snapshot), or None."""
        for r in self.snapshot.get("roots", []):
            if r.get("name") == name:
                return r.get("path")
        return None

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

    def run_verb(self, cmd: str, *, title: str = "would run", submit=None) -> None:
        """Run an action that maps to a CLI verb (§1.6).

        ``cmd`` is the human display string (the CLI verb it corresponds to).
        ``submit`` is a zero-arg callable that performs the real daemon call
        (``client.submit_*``) and returns a job id; pass it for actions that
        actually submit work.

        - **offline** demo: no daemon → open a notice modal showing ``cmd`` (the
          walkable "modal describing the flow"); ``submit`` is not called.
        - **online**: call ``submit()`` and show a brief "submitted — job #N" (or the
          error) notice, then refresh so the new job appears. This is what was
          missing — online the action used to be a silent no-op.
        """
        if self._modal_on_top():
            return
        if self.offline or submit is None:
            self.push_screen(MessageModal(cmd, title=title, footer="[Enter] ok  ·  demo (no-op)"))
            return
        try:
            job_id = submit()
        except Exception as exc:
            self.push_screen(MessageModal(f"{cmd}\n\n✗ {exc}", title="error",
                                          footer="[Enter] ok"))
            return
        self.refresh_data()
        note = f"submitted — job #{job_id}" if job_id else "submitted"
        self.push_screen(MessageModal(f"{cmd}\n\n{note} — watch it in the Queue ([q]).",
                                      title="submitted", footer="[Enter] ok"))

    def confirm_verb(self, question: str, cmd: str, *, count: int | None = None,
                     network: int = 0, submit=None) -> None:
        """Confirm (y/n or typed-count), then run the verb (§1.6 — gather then act).

        On confirm, delegates to :meth:`run_verb` (offline → notice; online →
        ``submit()`` + result notice). ``submit`` is the real daemon call.
        """
        if self._modal_on_top():
            return
        # The chained action (after confirm) runs once THIS modal has dismissed —
        # so run_verb there sees no modal on top and is allowed (ask→act sequence).
        def after(ok):
            if ok:
                self.run_verb(cmd, title="confirmed", submit=submit)

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
