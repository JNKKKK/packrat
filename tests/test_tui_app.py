"""Live app tests — drive the Textual pilot to assert navigation + rendering.

State-transition tests for the focus→maximize table and the drill-in/back-out
screen stack (component-plan §Testing), plus the modal result plumbing. Run the
async pilot from a sync test via ``asyncio.run`` so no pytest-asyncio plugin is
needed. The app runs in ``offline`` mode (fixtures), so these need no daemon.
"""

from __future__ import annotations

import asyncio

from packrat.tui.app import PackratApp
from packrat.tui.modals import ChoiceModal, ConfirmModal, MessageModal


def _drive(coro_fn):
    """Run an async pilot scenario to completion in a fresh offline app."""
    async def runner():
        app = PackratApp(offline=True)
        async with app.run_test(size=(100, 24)) as pilot:
            await coro_fn(app, pilot)
    asyncio.run(runner())


def test_online_now_tracks_wall_clock_not_fixture_date():
    # Regression: online mode froze `now` at fixtures.REFERENCE_NOW, corrupting every
    # relative time. Online (no pinned now) it must return the LIVE wall clock; offline
    # keeps the fixed reference its sample timestamps are relative to.
    from packrat.tui import fixtures
    from packrat.util import now_iso

    online = PackratApp(client=object(), offline=False)
    assert online._now is None                       # not pinned online
    assert online.now[:4] == now_iso()[:4]           # live wall-clock year, not the fixture's
    assert online.now != fixtures.REFERENCE_NOW

    offline = PackratApp(offline=True)
    assert offline.now == fixtures.REFERENCE_NOW      # demo data is relative to this

    pinned = PackratApp(offline=True, now="2020-01-02T03:04:05")
    assert pinned.now == "2020-01-02T03:04:05"        # an explicit pin always wins


def _screen(app) -> str:
    return type(app.screen).__name__


def _toasts(app) -> list:
    """The app's posted notifications (toasts). `_on_notify` records them even in
    headless test mode, so we can assert message + severity without rendering."""
    return list(app._notifications)


def _last_toast(app):
    ts = _toasts(app)
    return ts[-1] if ts else None


# --- rendering -------------------------------------------------------------
def test_dashboard_renders_logo_and_fixed_frame():
    # The offline demo uses the rich `demo` dataset (a job runs + a backlog), so
    # the dashboard shows the running/queue preview, not the idle message. Assert
    # the logo + the fixed 100×24 frame invariant.
    async def scenario(app, pilot):
        from packrat.tui.layout import cell_width
        f = app.screen.current_frame
        assert "|_) _.  _ |  ._ _. _|_" in f   # the "Packrat" ASCII wordmark
        assert "scan Archive" in f          # the demo's running job is visible
        rows = f.split("\n")
        assert len(rows) == 24
        # DISPLAY width (demo now includes a CJK root, so len() != cells on that row)
        assert all(cell_width(line) == 100 for line in rows)
    _drive(scenario)


def test_dashboard_logo_animation_cycles_gem_and_stays_fixed():
    """The logo animation swaps the held gem (◆→◇→◈) and keeps the frame 100×24."""
    async def scenario(app, pilot):
        from packrat.tui import render
        from packrat.tui.layout import cell_width
        dash = app.screen
        # A gem is always present in the mascot's hands.
        assert any(g * 2 in dash.current_frame for g in render.LOGO_GEMS)
        seen = set()
        for _ in range(len(render.LOGO_GEMS) * 22):     # cover >1 full gem cycle
            dash._tick_logo()
            await pilot.pause()
            gem = dash._gem
            seen.add(gem)
            assert f"(>{gem}{gem}<)" in dash.current_frame   # frame tracks the state
            rows = dash.current_frame.split("\n")
            assert len(rows) == 24 and all(cell_width(r) == 100 for r in rows)
        assert seen == set(render.LOGO_GEMS)             # every gem was shown
    _drive(scenario)


def test_dashboard_hoard_count_matches_gem_color():
    """The "· N assets hoarded ·" number is tinted the SAME color as the mascot's gem."""
    async def scenario(app, pilot):
        from packrat.tui.colorize import gem_gradient_color
        dash = app.screen
        frame = dash.current_frame
        text = dash._colorize(frame)                      # what the live widget paints
        color = gem_gradient_color(dash._gem_phase)
        gem_idx = frame.find(dash._gem)                   # first gem cell
        count_idx = frame.find("assets hoarded") - 2      # a digit of the count
        def color_at(i):
            c = text.style
            for s in text.spans:
                if s.start <= i < s.end:
                    c = s.style
            return str(c)
        assert color_at(gem_idx) == color
        assert color_at(count_idx) == color               # count glints with the gem
    _drive(scenario)


# --- focus → maximize table (§focus model) --------------------------------
def test_focus_then_maximize_roots():
    async def scenario(app, pilot):
        await pilot.press("r")
        f = app.screen.current_frame
        assert "┏" in f                                    # focused → heavy frame
        # Focused Roots title stays mixed-case "[R]oots" (shaded, not upper-cased).
        assert "┏━ [R]oots" in f and "[R]OOTS" not in f
        await pilot.press("r")
        assert _screen(app) == "RootsMax"                  # maximized
    _drive(scenario)


def _shade_style():
    from packrat.tui.tokens import DEFAULT_THEME as T
    return f"{T.color('accent-fg')} on {T.color('accent')}"


def _shaded_spans(screen):
    """Substrings of the screen's colorized frame carrying the shade (accent-tab) style."""
    frame = screen.current_frame
    text = screen._colorize(frame)
    want = _shade_style()
    return [frame[s.start:s.end] for s in text.spans if str(s.style) == want]


def test_dashboard_queue_box_title_shaded_when_focused():
    async def scenario(app, pilot):
        await pilot.press("q")                             # focus the Queue box
        f = app.screen.current_frame
        assert "┏━ [Q]ueue" in f and "[Q]UEUE" not in f    # mixed-case, shaded (not upper)
        shaded = _shaded_spans(app.screen)
        assert " [Q]ueue " in shaded                       # title tab shaded
        assert any(s.strip().startswith("page ") for s in shaded)   # pager shaded too
    _drive(scenario)


def test_root_detail_boxes_title_shaded_when_focused():
    async def scenario(app, pilot):
        await pilot.press("r"); await pilot.press("r"); await pilot.press("enter")
        assert _screen(app) == "RootDetailScreen"
        await pilot.press("e")                             # [e] → Review box
        assert app.screen.focus == "review"
        # focused box drops the key-hint brackets → plain title, shaded.
        assert " Review " in _shaded_spans(app.screen)
        await pilot.press("escape")
        await pilot.press("j")                             # [J] → Jobs box
        assert app.screen.focus == "jobs"
        assert " Jobs " in _shaded_spans(app.screen)       # Jobs tab shaded
    _drive(scenario)


def test_focus_peer_switch():
    async def scenario(app, pilot):
        await pilot.press("r")
        await pilot.press("q")                             # switch focus to queue
        assert _screen(app) == "Dashboard"                 # still dashboard, just refocused
        assert app.screen.focus_state.target == "queue"
    _drive(scenario)


def test_maximize_queue():
    async def scenario(app, pilot):
        await pilot.press("q")
        await pilot.press("q")
        assert _screen(app) == "QueueMax"
    _drive(scenario)


def test_queue_default_focus_is_running():
    """The maximized Queue opens with the Running section focused (issue #4)."""
    async def scenario(app, pilot):
        await pilot.press("q"); await pilot.press("q")
        assert _screen(app) == "QueueMax"
        assert app.screen.focus == "running"
        assert "[R]UNNING:" in app.screen.current_frame       # focused → uppercased
    _drive(scenario)


def test_root_detail_jobs_default_subsection_is_running():
    """Focusing the root-detail Jobs panel starts on the Running sub-section (issue #4)."""
    async def scenario(app, pilot):
        await pilot.press("r"); await pilot.press("r"); await pilot.press("enter")
        assert _screen(app) == "RootDetailScreen"
        assert app.screen.job_focus == "running"              # default sub-section
        await pilot.press("j")                                # focus the Jobs panel
        assert app.screen.focus == "jobs"
        assert "[R]UNNING:" in app.screen.current_frame
    _drive(scenario)


# --- drill-in / back-out stack --------------------------------------------
def test_drill_to_detail_then_card_then_back():
    async def scenario(app, pilot):
        await pilot.press("r")           # focus roots
        await pilot.press("r")           # maximize
        await pilot.press("enter")       # open detail
        assert _screen(app) == "RootDetailScreen"
        await pilot.press("enter")       # open a job card
        assert _screen(app) == "JobCard"
        await pilot.press("escape")      # back to detail
        assert _screen(app) == "RootDetailScreen"
        await pilot.press("escape")      # back to roots
        assert _screen(app) == "RootsMax"
        await pilot.press("escape")      # back to dashboard
        assert _screen(app) == "Dashboard"
    _drive(scenario)


def test_sort_cycle_changes_header():
    async def scenario(app, pilot):
        await pilot.press("r")
        await pilot.press("r")           # RootsMax
        assert "most recent registered" in app.screen.current_frame
        await pilot.press("s")
        assert "most assets" in app.screen.current_frame
        await pilot.press("s")
        assert "most photos" in app.screen.current_frame
        await pilot.press("s"); await pilot.press("s")   # wraps back
        assert "most recent registered" in app.screen.current_frame
    _drive(scenario)


def test_add_root_form_opens():
    async def scenario(app, pilot):
        await pilot.press("r")
        await pilot.press("r")
        await pilot.press("a")
        assert _screen(app) == "AddRootScreen"
        assert "Register a new root" in app.screen.current_frame
    _drive(scenario)


def _selected_emphasis_style():
    from packrat.tui.tokens import DEFAULT_THEME as T
    return f"bold {T.color('selected')}"


def _emphasized_spans(screen):
    frame = screen.current_frame
    text = screen._colorize(frame)
    want = _selected_emphasis_style()
    return [frame[s.start:s.end] for s in text.spans if str(s.style) == want]


def test_add_root_form_never_emphasizes_a_field():
    # A form field's ▸ marker (esp. the scan field, whose marker sits at the row start
    # with no label before it) must NOT get the list-row bold+bright emphasis.
    async def scenario(app, pilot):
        await pilot.press("r"); await pilot.press("r"); await pilot.press("a")
        assert _screen(app) == "AddRootScreen"
        for _ in range(4):                                 # tab through all four fields
            assert not _emphasized_spans(app.screen), f"field {app.screen._field} emphasized"
            await pilot.press("tab")
    _drive(scenario)


def test_maximized_roots_list_emphasizes_selected_row():
    # The positive case: a real list screen DOES bold+brighten its ▸-selected row.
    async def scenario(app, pilot):
        await pilot.press("r"); await pilot.press("r")     # RootsMax
        assert _screen(app) == "RootsMax"
        assert _emphasized_spans(app.screen)               # the selected row is emphasized
    _drive(scenario)


def test_escape_from_dashboard_unfocuses_not_quits():
    async def scenario(app, pilot):
        await pilot.press("r")
        await pilot.press("escape")
        assert _screen(app) == "Dashboard"
        assert app.screen.focus_state.target is None
    _drive(scenario)


# --- modals (result plumbing) ---------------------------------------------
def test_message_modal_dismisses():
    async def scenario(app, pilot):
        result = {}
        app.push_screen(MessageModal("name already in use"),
                        lambda r: result.__setitem__("r", r))
        await pilot.pause()
        assert _screen(app) == "MessageModal"
        await pilot.press("enter")
        await pilot.pause()
        assert _screen(app) == "Dashboard"
    _drive(scenario)


def test_confirm_modal_yes_no():
    async def scenario(app, pilot):
        result = {}
        app.push_screen(ConfirmModal("Delete?"), lambda r: result.__setitem__("r", r))
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert result["r"] is True
    _drive(scenario)


def test_confirm_modal_typed_count_match():
    async def scenario(app, pilot):
        result = {}
        app.push_screen(ConfirmModal("Delete?", count=240, network=12),
                        lambda r: result.__setitem__("r", r))
        await pilot.pause()
        for ch in "240":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert result["r"] is True
    _drive(scenario)


def test_confirm_modal_typed_count_mismatch():
    async def scenario(app, pilot):
        result = {}
        app.push_screen(ConfirmModal("Delete?", count=240),
                        lambda r: result.__setitem__("r", r))
        await pilot.pause()
        for ch in "12":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert result["r"] is False
    _drive(scenario)


def test_choice_modal_returns_index():
    async def scenario(app, pilot):
        result = {}
        app.push_screen(ChoiceModal(["a", "b", "c"]), lambda r: result.__setitem__("r", r))
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert result["r"] == 1
    _drive(scenario)


# --- online actions actually submit to the daemon (the reported bug) -------
class _FakeClient:
    """Minimal daemon client stand-in — records submits, returns fake job ids."""

    def __init__(self):
        self.calls = []

    def status(self, root=None):
        from packrat.tui import demo
        if root:
            return {"root_detail": demo.root_detail(root)}
        return demo.status_snapshot(running=True)

    def list_jobs(self, limit=20):
        from packrat.tui import demo
        return demo.recent_jobs()

    def root_jobs(self, rid, limit=50):
        return []

    def submit_scan(self, root, **kw):
        self.calls.append(("scan", root)); return 901

    def submit_dedup(self, root, **kw):
        self.calls.append(("dedup", root, kw)); return 902

    def submit_cleanup(self, root, **kw):
        self.calls.append(("cleanup", root, kw)); return 903

    def submit_merge(self, source, into, dry_run=False):
        self.calls.append(("merge", source, into, dry_run)); return 904

    def cancel_job(self, jid):
        self.calls.append(("cancel", jid)); return True

    def prioritize_job(self, jid):
        self.calls.append(("prioritize", jid)); return True

    def cancel_queued(self):
        self.calls.append(("cancel_queued",)); return 5


def _drive_online(coro_fn):
    fc = _FakeClient()

    async def runner():
        app = PackratApp(client=fc, offline=False)
        async with app.run_test(size=(120, 34)) as pilot:
            await coro_fn(app, pilot)
    asyncio.run(runner())
    return fc


def test_root_detail_scan_submits_online():
    def scenario_factory():
        async def scenario(app, pilot):
            await pilot.press("r"); await pilot.press("r"); await pilot.press("enter")
            assert _screen(app) == "RootDetailScreen"
            await pilot.press("s")                 # [s] scan → real submit
            await pilot.pause()
            # no-confirm action → a toast, NOT a modal popup (still on the detail)
            assert _screen(app) == "RootDetailScreen"
            t = _last_toast(app)
            assert t and t.severity == "information" and "job #901" in t.message
        return scenario
    fc = _drive_online(scenario_factory())
    assert fc.calls and fc.calls[0][0] == "scan", fc.calls


def test_root_detail_dedup_submits_online():
    fc = _drive_online(_press_seq(["r", "r", "enter", "d"]))
    assert any(c[0] == "dedup" for c in fc.calls), fc.calls


def test_merge_picker_submits_online():
    # [m] opens the picker; [Enter] merges the first source into the dest root.
    async def scenario(app, pilot):
        await pilot.press("r"); await pilot.press("r"); await pilot.press("enter")
        await pilot.press("m")
        assert _screen(app) == "MergePickerScreen"
        await pilot.press("enter")               # merge first registered-root source
        await pilot.pause()
    fc = _drive_online(scenario)
    merges = [c for c in fc.calls if c[0] == "merge"]
    assert merges, fc.calls
    # (kind, source, into, dry_run) — into is a real root name, source a path
    assert merges[0][2] and merges[0][3] is False


def test_queue_cancel_submits_online():
    async def scenario(app, pilot):
        await pilot.press("q"); await pilot.press("q")   # QueueMax
        await pilot.press("down")                        # select a queued job
        await pilot.press("c")                           # [c] cancel
        await pilot.pause()
        await pilot.press("y")                           # confirm
        await pilot.pause()
    fc = _drive_online(scenario)
    assert any(c[0] == "cancel" for c in fc.calls), fc.calls


def test_online_submit_error_shows_red_toast_not_crash():
    class FailClient(_FakeClient):
        def submit_scan(self, root, **kw):
            raise RuntimeError("boom")

    fc = FailClient()

    async def scenario(app, pilot):
        await pilot.press("r"); await pilot.press("r"); await pilot.press("enter")
        await pilot.press("s")
        await pilot.pause()
        # a submit exception surfaces as a RED (error) toast, not a crash / modal
        assert _screen(app) == "RootDetailScreen"        # app alive, no popup
        t = _last_toast(app)
        assert t and t.severity == "error" and "boom" in t.message

    app = PackratApp(client=fc, offline=False)

    async def runner():
        async with app.run_test(size=(120, 34)) as pilot:
            await scenario(app, pilot)
    asyncio.run(runner())


def _press_seq(keys):
    async def scenario(app, pilot):
        for k in keys:
            await pilot.press(k)
            await pilot.pause()
    return scenario


# --- fix #1: daemon-down renders (zeroed snapshot), never KeyError-crashes ---
class _DownClient(_FakeClient):
    """A client whose every call fails — stands in for an unreachable daemon."""

    def status(self, root=None):
        raise RuntimeError("daemon unreachable")

    def list_jobs(self, limit=20):
        raise RuntimeError("daemon unreachable")

    def root_jobs(self, rid, limit=50):
        raise RuntimeError("daemon unreachable")


def test_daemon_down_renders_dashboard_not_crash():
    fc = _DownClient()

    async def scenario(app, pilot):
        # The dashboard must draw (zeroed) and flag the daemon down — the pre-fix
        # empty `{}` snapshot KeyError'd in dashboard_body before any frame appeared.
        f = app.screen.current_frame
        rows = f.split("\n")
        assert len(rows) == 34                      # full frame drew (size below)
        assert "daemon ○ down" in f                 # header reflects the failure
        assert "|_) _.  _ |  ._ _. _|_" in f         # the "Packrat" ASCII wordmark
        # Navigating still works (no snapshot key blows up).
        await pilot.press("r")
        await pilot.pause()
        assert _screen(app) == "Dashboard"

    app = PackratApp(client=fc, offline=False)

    async def runner():
        async with app.run_test(size=(100, 34)) as pilot:
            await scenario(app, pilot)
    asyncio.run(runner())


# --- issue #3: root-detail running job inherits the live ETA/counters ---------
def test_root_detail_running_job_gets_live_eta():
    """The SSE ETA is tracked on snapshot['running']; root detail's running_job comes
    from a separate fetch. When it's the same job, the live done/total/_eta_s must be
    mirrored onto it so root detail shows the same bar + ETA (issue #3)."""
    app = PackratApp(offline=True)                 # no daemon needed for the helper
    app.snapshot = {"running": {"id": 42, "done": 900, "total": 1000, "_eta_s": 17.0}}
    # A per-root running_job for the SAME job but without the live estimate.
    job = {"id": 42, "done": 400, "total": 1000}
    app._inject_live_progress(job)
    assert job["_eta_s"] == 17.0 and job["done"] == 900 and job["total"] == 1000
    # A DIFFERENT job id is left untouched (not the one being streamed).
    other = {"id": 99, "done": 5, "total": 10}
    app._inject_live_progress(other)
    assert "_eta_s" not in other and other["done"] == 5


# --- fix #3: a cleanup-perceptual review card confirms via `cleanup`, not dedup ---
def test_cleanup_pending_card_confirms_via_cleanup_verb():
    from packrat.tui import fixtures as fx
    from packrat.tui.app import JobCard

    fc = _FakeClient()

    async def scenario(app, pilot):
        app.push_screen(JobCard(dict(fx.CLEANUP_PENDING)))
        await pilot.pause()
        assert _screen(app) == "JobCard"
        assert "awaiting review" in app.screen.current_frame
        await pilot.press("g")                 # confirm stage → confirm modal
        await pilot.pause()
        assert _screen(app) == "ConfirmModal"
        await pilot.press("y")                 # confirm → real submit
        await pilot.pause()
        t = _last_toast(app)
        assert t and "cleanup Photos --confirm" in t.message

    app = PackratApp(client=fc, offline=False)

    async def runner():
        async with app.run_test(size=(100, 34)) as pilot:
            await scenario(app, pilot)
    asyncio.run(runner())
    # The submit went to cleanup (mode=perceptual, confirm), NOT dedup.
    assert any(c[0] == "cleanup" and c[2].get("confirm") for c in fc.calls), fc.calls
    assert not any(c[0] == "dedup" for c in fc.calls), fc.calls


# --- a confirmed JobCard action returns to the previous interface (after toast) ---
def test_jobcard_action_pops_back_after_toast():
    """Confirming an action on the job-details card reports via a toast and then pops
    the card back to the interface that opened it — the user shouldn't be left on a
    now-stale card. Declining (n) posts no toast and stays put."""
    from packrat.tui import fixtures as fx
    from packrat.tui.app import JobCard

    fc = _FakeClient()

    async def scenario(app, pilot):
        # Dashboard is the interface the card was opened from.
        assert _screen(app) == "Dashboard"

        # Decline first: 'g' → ConfirmModal, 'n' → no toast, still on the card.
        app.push_screen(JobCard(dict(fx.CLEANUP_PENDING)))
        await pilot.pause()
        assert _screen(app) == "JobCard"
        before = len(_toasts(app))
        await pilot.press("g")
        await pilot.pause()
        assert _screen(app) == "ConfirmModal"
        await pilot.press("n")
        await pilot.pause()
        assert _screen(app) == "JobCard"                 # declined → stayed put
        assert len(_toasts(app)) == before               # and posted no toast

        # Confirm: 'g' → ConfirmModal, 'y' → toast fires AND the card pops back.
        await pilot.press("g")
        await pilot.pause()
        assert _screen(app) == "ConfirmModal"
        await pilot.press("y")
        await pilot.pause()
        assert _screen(app) == "Dashboard"               # popped back to the opener
        assert len(_toasts(app)) == before + 1           # exactly one action toast

    app = PackratApp(client=fc, offline=False)

    async def runner():
        async with app.run_test(size=(100, 34)) as pilot:
            await scenario(app, pilot)
    asyncio.run(runner())


# --- fix #2: the running bar is driven by the SSE stream + TUI-side ETA ------
def test_running_job_streams_progress_and_eta():
    """The app attaches an SSE stream to the running job; its events advance the bar
    and the TUI-side ETA (not just the 3s poll). We feed a canned event sequence."""
    class StreamClient(_FakeClient):
        def stream_job(self, job_id):
            # Two NON-terminal progress samples → the live bar advances between polls
            # and a TUI-side ETA becomes derivable. (No terminal event, so nothing
            # refetches and clobbers the streamed state within the test window.)
            yield {"job_id": job_id, "type": "progress", "done": 20000, "total": 45000}
            yield {"job_id": job_id, "type": "progress", "done": 30000, "total": 45000}

    fc = StreamClient()

    async def scenario(app, pilot):
        # Give the worker thread a moment to consume the stream + re-render.
        for _ in range(5):
            await pilot.pause(0.05)
        running = app.snapshot.get("running")
        assert running is not None
        # The streamed `done` (30000) overrode the demo's initial 17800 — proof the
        # bar is SSE-driven, not just the 3s poll.
        assert running["done"] == 30000
        # An ETA was derived TUI-side from the spaced samples and stamped on the row.
        assert running.get("_eta_s") is not None and running["_eta_s"] > 0

    app = PackratApp(client=fc, offline=False)

    async def runner():
        async with app.run_test(size=(100, 24)) as pilot:
            await scenario(app, pilot)
    asyncio.run(runner())


# --- fix #4: root detail fetches on mount/poll, not on every keypress --------
def test_root_detail_does_not_refetch_per_keypress():
    class CountingClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.status_calls = 0

        def status(self, root=None):
            if root:
                self.status_calls += 1
            return super().status(root)

    fc = CountingClient()

    async def scenario(app, pilot):
        await pilot.press("r"); await pilot.press("r"); await pilot.press("enter")
        assert _screen(app) == "RootDetailScreen"
        before = fc.status_calls
        # Focus the Jobs panel and navigate — pure re-renders, NO new daemon fetch.
        await pilot.press("j")
        await pilot.press("down"); await pilot.press("down"); await pilot.press("up")
        await pilot.pause()
        assert fc.status_calls == before, "navigation must not re-hit status <root>"

    app = PackratApp(client=fc, offline=False)

    async def runner():
        async with app.run_test(size=(120, 34)) as pilot:
            await scenario(app, pilot)
    asyncio.run(runner())
