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


def _screen(app) -> str:
    return type(app.screen).__name__


# --- rendering -------------------------------------------------------------
def test_dashboard_renders_logo_and_fixed_frame():
    # The offline demo uses the rich `demo` dataset (a job runs + a backlog), so
    # the dashboard shows the running/queue preview, not the idle message. Assert
    # the logo + the fixed 100×24 frame invariant.
    async def scenario(app, pilot):
        f = app.screen.current_frame
        assert "p a c k r a t" in f
        assert "scan Archive" in f          # the demo's running job is visible
        assert len(f.split("\n")) == 24
        assert all(len(line) == 100 for line in f.split("\n"))
    _drive(scenario)


# --- focus → maximize table (§focus model) --------------------------------
def test_focus_then_maximize_roots():
    async def scenario(app, pilot):
        await pilot.press("r")
        assert "[R]OOTS" in app.screen.current_frame       # focused heavy frame
        await pilot.press("r")
        assert _screen(app) == "RootsMax"                  # maximized
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
