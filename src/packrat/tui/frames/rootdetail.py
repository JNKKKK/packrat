"""The RootDetailScreen screen (M6, §12) — see :mod:`packrat.tui.frames.base`."""

from __future__ import annotations

import time

from textual import work
from textual.binding import Binding

from .. import demo
from ..colorize import colorize
from ..framing import screen
from ..modals import ChoiceModal
from ..screens.rootdetail import detail_body
from ..screens.rootdetail import detail_header_right

from .base import FrameScreen, _open_in_explorer, _review_verb
from .jobcard import JobCard
from .mergepicker import MergePickerScreen


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
        self.review_scroll = 0           # ↑/↓ offset when the Review box overflows its cap
        # History is LAZY-loaded a page at a time (§12), like the Queue screen: running +
        # queued come from the detail dict (the live snapshot read), but the terminal-job
        # history is unbounded, so we hold only the current page + its true total count.
        self._history: list[dict] = []
        self._history_total = 0
        # History PAGE SIZE = the jobs-panel height, which changes on resize AND when the
        # review box appears/shrinks (a poll can move it). That invalidates the stored page
        # INDEX, so `_history_win` records the size the loaded page used and `_anchor_history`
        # re-derives the index (keeping the first-visible job) whenever it changes.
        self._history_win = 0
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

    def _history_rows(self) -> int:
        """The History window height this frame — the page size we fetch. Derived from
        the panel split (which depends on the review-box size), so it needs a built geo."""
        from ..screens.rootdetail import panel_section_rows
        try:
            return max(1, panel_section_rows(self._detail or {}, self._geo)["history"])
        except Exception:  # noqa: BLE001 - before the first frame built a geo
            return 4

    def _history_pages(self) -> int:
        rows = self._history_rows()
        return max(1, -(-self._history_total // rows)) if rows else 1

    def _anchor_history(self) -> int:
        """Re-anchor the history page index to the CURRENT window height, returning that
        height. The page size (jobs-panel height) shifts on resize and when the review box
        appears/shrinks, so a stored index is only valid for its own size — re-derive it
        from the absolute offset (keep the first-visible job on-screen) whenever it changes.
        Call on the UI thread before computing a fetch offset."""
        rows = self._history_rows()
        if self._history_win and rows != self._history_win:
            from ..screens.queue import reanchor_page
            self.pages["history"] = reanchor_page(
                self.pages["history"], self._history_win, rows, self._history_total)
            self.cursors["history"] = 0
        self._history_win = rows
        return rows

    def reload(self) -> None:
        """Fetch this root's detail + current history page (mount + first paint).

        ``root_detail`` is ``status <root>`` (stats/review/running/queued); the terminal
        History is a SEPARATE lazy-loaded page (:meth:`_reload_history`), so a long job
        history never fetches wholesale. :meth:`frame` renders from the cached
        ``self._detail``/``self._history``; the POLL path (:meth:`poll_reload`) fetches
        off the UI thread so a slow daemon can't freeze input on the timer."""
        self._detail = self.app.root_detail(self.root_name)
        self._loaded = True
        self._reload_history()

    def on_mount(self) -> None:
        self.reload()
        super().on_mount()

    def on_resize(self, event) -> None:
        # Base refreshes the frame (→ frame() rebuilds self._geo); then re-anchor + re-fetch
        # the history page for the new panel height so a deep page stays valid.
        super().on_resize(event)
        self._reload_history()

    def poll_reload(self) -> None:
        """Poll refresh — fetch off the UI thread so a slow daemon can't freeze input.

        Offline (in-memory demo) or with no running loop (unit tests) applies inline;
        online it hands the blocking fetch to a worker that marshals back via
        :meth:`_apply_reload`."""
        if self.app.offline or not self.app._app_loop_running():
            self.reload()
            return
        # Re-anchor on the UI thread BEFORE the worker reads the page/offset (a poll can
        # move the review box → change the jobs-panel height → invalidate the page index).
        rows = self._anchor_history()
        self._poll_fetch(rows, self.pages["history"] * rows)

    @work(thread=True, exclusive=True, group="rootdetail-poll")
    def _poll_fetch(self, rows: int, offset: int) -> None:
        detail = self.app.root_detail(self.root_name)
        rid = detail.get("id") if detail else None
        hist, total = self.app.root_history(self.root_name, rid, rows, offset)
        try:
            self.app.call_from_thread(self._apply_reload, detail, hist, total)
        except Exception:
            pass   # screen/app tearing down

    def _apply_reload(self, detail, hist, total) -> None:
        self._detail = detail
        self._history, self._history_total = hist, total
        self._loaded = True
        self.refresh_frame()

    def _reload_history(self) -> None:
        """Fetch the current history page (offset = page·window). Inline offline / no loop;
        else off the UI thread. Called on mount, resize, poll, and history page change.
        Re-anchors the page index first so a window-height change can't strand it."""
        rows = self._anchor_history()
        offset = self.pages["history"] * rows
        rid = self._detail.get("id") if self._detail else None
        if self.app.offline or not self.app._app_loop_running():
            self._history, self._history_total = self.app.root_history(
                self.root_name, rid, rows, offset)
            return
        self._fetch_history_async(rid, rows, offset)

    @work(thread=True, exclusive=True, group="rootdetail-history")
    def _fetch_history_async(self, rid, limit: int, offset: int) -> None:
        hist, total = self.app.root_history(self.root_name, rid, limit, offset)
        try:
            self.app.call_from_thread(self._apply_history, hist, total)
        except Exception:
            pass   # screen/app tearing down

    def _apply_history(self, hist: list[dict], total: int) -> None:
        self._history, self._history_total = hist, total
        self.refresh_frame()

    def frame(self) -> str:
        # Render from the cached detail/history (fetched on mount + poll, not per keypress).
        if not self._loaded:
            self.reload()
        d = self._detail
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
        # DISPLAY masking before layout (detail + history); the raw self._detail/_history
        # stay the source for actions (scan/dedup/merge/review, root_name lookups). History
        # is the LAZY-loaded current page; its true total-page count drives the paginator.
        vd, vhist = self.app.view(d), self.app.view(self._history)
        body = detail_body(vd, now=self.now, geo=geo, jobs=vhist,
                          focus=self.focus, job_focus=self.job_focus,
                          cursors=self.cursors, pages=self.pages,
                          review_scroll=self.review_scroll,
                          history_total_pages=self._history_pages())
        return screen(f"packrat · {vd['name']}", body, detail_header_right(vd),
                      footer=footer, width=geo.w, height=geo.h)

    def _colorize(self, frame: str):
        # Base colors + the ▸-selected-row emphasis (inherited), then shade the focused
        # box's title tab (accent tab), matching the dashboard boxes.
        from ..colorize import shade_box_title
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
        from ..screens.rootdetail import split_jobs
        # History is the pre-sliced lazy page → prefiltered (don't re-filter/slice it).
        return split_jobs(self._detail or {}, self._history, prefiltered=True)

    def _section_jobs(self, section: str) -> list[dict]:
        return self._sections().get(section, [])

    def _section_rows(self, section: str) -> int:
        # The queued/history window heights the body used this frame (§3 panel split).
        from ..screens.rootdetail import panel_section_rows
        return panel_section_rows(self._detail or {}, self._geo)[section]

    def action_focus_review(self) -> None:
        # [e] focuses the R[e]view box (always focus-able, even with no pending review —
        # the box is a permanent section, like Jobs).
        self.focus = "review"
        self.review_scroll = 0          # start at the top each time it gains focus
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
        if self.focus == "review":
            # ↑/↓ scroll the Review box when its content exceeds the responsive cap.
            from ..screens.rootdetail import review_scroll_max
            hi = review_scroll_max(self._detail or {}, self._geo)
            self.review_scroll = max(0, min(self.review_scroll + delta, hi))
            self.refresh_frame()
            return
        if self.focus != "jobs":
            return
        sec = self.job_focus
        n = len(self._section_jobs(sec))
        rows = self._section_rows(sec)
        cur = max(0, min(self.cursors[sec] + delta, n - 1)) if n else 0
        self.cursors[sec] = cur
        # History's cursor is WITHIN the loaded page (rows come pre-sliced), so it never
        # auto-advances the page here — page moves are explicit (←/→ → re-fetch).
        if sec != "history":
            self.pages[sec] = cur // rows if rows else 0     # auto-follow within section
        self.refresh_frame()

    def action_page(self, delta: int) -> None:
        if self.focus != "jobs":
            return
        sec = self.job_focus
        rows = self._section_rows(sec)
        if sec == "history":
            # Lazy-loaded: clamp to the TRUE page count, then re-fetch that page (cursor
            # resets to its first row — the rows arrive from the server, not sliced here).
            pages = self._history_pages()
            new = max(0, min(self.pages["history"] + delta, pages - 1))
            if new != self.pages["history"]:
                self.pages["history"] = new
                self.cursors["history"] = 0
                self._reload_history()
            self.refresh_frame()
            return
        n = len(self._section_jobs(sec))
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

    #: [s] scan-type prompt. Cursor 0 = Normal (fast-path skip), so a plain [Enter]
    #: keeps the incremental default; option 1 maps to `scan --full` (§8 A2 step 4).
    #: Labels kept ≤ ~54 cells so the choice modal doesn't clip them (like CLEANUP_MODES).
    SCAN_TYPE_OPTIONS = [
        "Normal  (fast-path skip; relink moves, no re-hash)",
        "Full  (re-hash every file; retry undecodables)",
    ]

    def action_scan(self) -> None:
        if not self.is_active:
            return
        root = self.root_name

        def after(idx):
            if idx is None:                      # Esc → don't submit anything
                return
            full = idx == 1
            cmd = f"packrat scan {root}" + (" --full" if full else "")
            self.app.run_verb(cmd, title="scan",
                              submit=lambda: self.app.client.submit_scan(root, full=full))

        self.app.push_screen(
            ChoiceModal(self.SCAN_TYPE_OPTIONS, title="scan — which type?",
                        prompt="How should packrat scan this root?"),
            after)

    #: [d] dedup master-preference prompt (§8 B --prefer-internal). Cursor 0 = default
    #: (external is the master), so a plain [Enter] keeps today's behavior.
    DEDUP_PREFER_OPTIONS = [
        "Prefer external (keep the other root's copy)",
        "Prefer internal (keep this root's copy)",
    ]

    def action_dedup(self) -> None:
        if not self.is_active:
            return
        root = self.root_name

        def after(idx):
            if idx is None:                      # Esc → don't submit anything
                return
            prefer_internal = idx == 1
            cmd = f"packrat dedup {root}" + (" --prefer-internal" if prefer_internal else "")
            self.app.run_verb(
                cmd, title="dedup",
                submit=lambda: self.app.client.submit_dedup(root, prefer_internal=prefer_internal))

        self.app.push_screen(
            ChoiceModal(self.DEDUP_PREFER_OPTIONS, title="dedup — which copy is the master?",
                        prompt="On exact dups + tie-break suggestions across roots:"),
            after)

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
        from ..screens.rootdetail import is_stage2_dedup
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
