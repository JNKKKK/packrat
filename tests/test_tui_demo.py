"""Offline demo tests — multi-page pagination + every action → modal → CLI verb.

The offline demo (``packrat --offline``) uses :mod:`packrat.tui.demo` (rich data:
11 roots, 9 queued, 12 recent, every job shape) so a person can exercise every
screen and action without a daemon. These tests lock in that (a) each paginating
list spans >1 page and pages navigably, and (b) every action key opens its real
modal and surfaces the CLI verb it maps to (§1.6). Actions are no-ops on state by
design (the chosen "modal describing the flow" behavior) — we assert the verb text.
"""

from __future__ import annotations

import asyncio
import re

from textual.widgets import Static

from packrat.tui import demo
from packrat.tui.app import PackratApp


def _drive(coro_fn):
    async def runner():
        app = PackratApp(offline=True)
        async with app.run_test(size=(100, 24)) as pilot:
            await coro_fn(app, pilot)
    asyncio.run(runner())


def _scr(app) -> str:
    return type(app.screen).__name__


def _modal_text(app) -> str:
    try:
        return str(app.screen.query_one("#modal-frame", Static).render())
    except Exception:
        return ""


def _pager(app):
    m = re.search(r"page (\d+)/(\d+)", app.screen.current_frame)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _pagers(app):
    """All ``(cur, total)`` paginators in reading order (queue has one per section)."""
    return [(int(a), int(b)) for a, b in re.findall(r"page (\d+)/(\d+)", app.screen.current_frame)]


# --- demo dataset shape ----------------------------------------------------
def test_demo_has_multipage_datasets():
    assert len(demo.ROOTS) >= 6           # > one Roots page (5/page)
    assert len(demo.QUEUED) >= 5
    assert len(demo.RECENT) >= 8
    # every job shape is represented for §5 card coverage
    statuses = {j["status"] for j in demo.RECENT}
    assert {"done", "error", "interrupted"} <= statuses


def test_demo_root_dot_states_all_present():
    from packrat.tui import render
    dots = {render.root_dot(r) for r in demo.ROOTS}
    assert {"◉", "◐", "○", " "} <= dots   # deduped, scanned-only, never, trash


# --- pagination ------------------------------------------------------------
def test_roots_interface_spans_multiple_pages():
    def scenario(app, pilot):
        return _roots_pages(app, pilot)

    async def _roots_pages(app, pilot):
        await pilot.press("r")
        await pilot.press("r")                 # RootsMax
        assert _scr(app) == "RootsMax"
        cur, total = _pager(app)
        assert total >= 3 and cur == 1
        await pilot.press("right")
        assert _pager(app) == (2, total)
        await pilot.press("left")
        assert _pager(app) == (1, total)
    _drive(_roots_pages)


def test_dashboard_roots_box_pages_and_stays_fixed():
    async def scenario(app, pilot):
        await pilot.press("r")                 # focus roots box on the dashboard
        cur, total = _pager(app)
        assert total >= 2 and cur == 1
        await pilot.press("right")
        assert _pager(app)[0] == 2
        # frame never grows past the fixed 100×24
        rows = app.screen.current_frame.split("\n")
        assert len(rows) == 24 and all(len(r) == 100 for r in rows)
    _drive(scenario)


def test_queue_has_independent_per_section_paginators():
    """Each of the 3 sections (Queued, Recent) has its OWN paginator (§4 redesign)."""
    async def scenario(app, pilot):
        await pilot.press("q")
        await pilot.press("q")                 # QueueMax
        pagers = _pagers(app)
        assert len(pagers) == 2                # queued + recent each have one
        assert all(total >= 2 for _, total in pagers)   # both span >1 page (demo data)
    _drive(scenario)


def test_queue_paging_one_section_leaves_others_untouched():
    """Paging the focused section must NOT change another section's page (issue #3)."""
    async def scenario(app, pilot):
        await pilot.press("q")
        await pilot.press("q")                 # QueueMax (focus=queued by default)
        before = _pagers(app)                  # [(q_cur, q_tot), (r_cur, r_tot)]
        await pilot.press("e")                 # focus Recent
        await pilot.press("right")             # page Recent →
        after = _pagers(app)
        assert after[0] == before[0], "queued page changed when paging recent"
        assert after[1][0] == before[1][0] + 1, "recent page did not advance"
    _drive(scenario)


def test_queue_cursor_autofollows_within_focused_section():
    """↑/↓ scrolls the FOCUSED section's page so its ▸ stays visible."""
    async def scenario(app, pilot):
        await pilot.press("q")
        await pilot.press("q")                 # QueueMax, focus=queued
        for _ in range(8):                     # past the 6-row queued window
            await pilot.press("down")
        assert "▸" in app.screen.current_frame, "cursor vanished (no auto-follow)"
        assert _pagers(app)[0][0] >= 2, "queued page did not follow its cursor"
    _drive(scenario)


def test_queue_section_focus_switches_with_letter_keys():
    """[r]/[q]/[e] focus the Running/Queued/Recent sections."""
    async def scenario(app, pilot):
        await pilot.press("q")
        await pilot.press("q")                 # QueueMax
        await pilot.press("r")
        assert app.screen.focus == "running"
        assert "[R]UNNING:" in app.screen.current_frame   # focused → uppercased
        await pilot.press("e")
        assert app.screen.focus == "recent"
        assert "REC[E]NT:" in app.screen.current_frame
        await pilot.press("q")
        assert app.screen.focus == "queued"
        assert "[Q]UEUED (RUNS TOP-DOWN):" in app.screen.current_frame
    _drive(scenario)


def test_dashboard_queue_box_pages_and_autofollows():
    """The focused dashboard Queue box pages (←/→) and the ▸ auto-follows (↑/↓)."""
    async def scenario(app, pilot):
        await pilot.press("q")                 # focus the Queue box (single press)
        assert _scr(app) == "Dashboard"
        qtitle = [ln for ln in app.screen.current_frame.split("\n") if "[Q]UEUE" in ln]
        assert qtitle and re.search(r"page 1/\d+", qtitle[0])
        total = int(re.search(r"page 1/(\d+)", qtitle[0]).group(1))
        assert total >= 2                      # demo backlog spans >1 preview page
        await pilot.press("right")
        qtitle2 = [ln for ln in app.screen.current_frame.split("\n") if "[Q]UEUE" in ln]
        assert re.search(r"page 2/", qtitle2[0])
        # down-arrow auto-follow keeps the cursor visible
        for _ in range(6):
            await pilot.press("down")
        f = app.screen.current_frame
        assert "▸" in f
        rows = f.split("\n")
        assert len(rows) == 24 and all(len(r) == 100 for r in rows)  # still fixed
    _drive(scenario)


def test_focused_box_border_is_accent_colored():
    """A focused box's heavy border carries the accent (focus-border) color."""
    from rich.console import Console
    import io
    from packrat.tui.tokens import DEFAULT_THEME as T

    async def scenario(app, pilot):
        await pilot.press("r")                 # focus Roots (heavy border)
        con = Console(file=io.StringIO(), force_terminal=True,
                      color_system="truecolor", width=100, height=24)
        con.print(app.screen.query_one("#frame").render(), end="")
        out = con.file.getvalue()
        hexc = T.color("focus-border").lstrip("#")
        rgb = f"{int(hexc[0:2], 16)};{int(hexc[2:4], 16)};{int(hexc[4:6], 16)}"
        assert rgb in out, "focused heavy border is not accent-colored"
    _drive(scenario)


# --- page-change resets cursor to the new page's first item ----------------
def _cursor_row(app) -> int | None:
    for i, ln in enumerate(app.screen.current_frame.split("\n")):
        if "▸" in ln:
            return i
    return None


def test_roots_page_change_moves_cursor_to_new_page():
    """←/→ in the Roots list puts the ▸ on the new page's first item, not the old."""
    async def scenario(app, pilot):
        await pilot.press("r")
        await pilot.press("r")                     # RootsMax
        await pilot.press("down")
        await pilot.press("down")                  # cursor low on page 1
        row_before = _cursor_row(app)
        await pilot.press("right")                 # page 2
        assert _pager(app)[0] == 2
        row_after = _cursor_row(app)
        assert row_after is not None, "cursor left behind on the previous page"
        assert row_after < row_before, "cursor did not reset to the top of the new page"
    _drive(scenario)


def test_queue_page_change_moves_cursor_to_new_page():
    """←/→ in the maximized Queue moves the ▸ to the first job on the new page."""
    async def scenario(app, pilot):
        await pilot.press("q")
        await pilot.press("q")                     # QueueMax
        await pilot.press("right")                 # page 2 → cursor on its first job
        assert _pager(app)[0] == 2
        assert _cursor_row(app) is not None, "cursor not visible on the new page"
        await pilot.press("left")                  # back to page 1
        assert _pager(app)[0] == 1
        assert _cursor_row(app) is not None
    _drive(scenario)


def test_dashboard_box_page_change_moves_cursor():
    """←/→ in the focused dashboard Roots box moves the cursor onto the new page."""
    async def scenario(app, pilot):
        await pilot.press("r")                     # focus Roots box
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("right")                 # page 2
        f = app.screen.current_frame
        # the ▸ must be present somewhere in the (now page-2) roots box
        assert "▸" in f
    _drive(scenario)


# --- modal is truly modal (the c/p crash fix) ------------------------------
def test_modal_swallows_unbound_keys_no_crash():
    """Keys a modal doesn't bind must not leak to the screen beneath (or crash).

    Regression for the ``No screens on stack`` abort: pressing ``c`` repeatedly
    (or other queue-action keys) while a confirm modal is open used to bubble to
    the Dashboard, re-push modals, and underflow the stack. The modal now swallows
    unbound keys, so the stack stays intact.
    """
    async def scenario(app, pilot):
        await pilot.press("q")                     # focus Queue box
        await pilot.press("down")                  # select a queued job
        await pilot.press("c")                     # open the confirm modal
        assert _scr(app) == "ConfirmModal"
        # hammer keys the modal does NOT bind — must stay on the modal, no crash
        for k in ("c", "p", "x", "q", "r", "c", "c"):
            await pilot.press(k)
        assert _scr(app) == "ConfirmModal", "unbound key leaked to the screen beneath"
        assert len(app.screen_stack) == 3          # base + dashboard + modal
    _drive(scenario)


def test_modal_own_bindings_still_work_after_swallow_fix():
    """The swallow fix must not break the modal's own y/n/enter/escape bindings."""
    async def scenario(app, pilot):
        # y confirms
        await pilot.press("q")
        await pilot.press("down")
        await pilot.press("c")
        await pilot.press("y")
        await pilot.pause()
        assert _scr(app) == "MessageModal"         # confirmed → shows the verb
        await pilot.press("enter")                 # dismiss the notice
        await pilot.pause()
        assert _scr(app) == "Dashboard"
        # queue box is still focused (returning from a modal keeps focus), so a
        # cancel + decline stays on the dashboard. (No extra `q`, which would
        # maximize the already-focused queue.)
        await pilot.press("c")
        assert _scr(app) == "ConfirmModal"
        await pilot.press("n")
        await pilot.pause()
        assert _scr(app) == "Dashboard"
    _drive(scenario)


def test_root_detail_jobs_list_pages():
    async def scenario(app, pilot):
        await pilot.press("r")
        await pilot.press("r")
        await pilot.press("enter")             # open first root's detail
        assert _scr(app) == "RootDetailScreen"
        cur, total = _pager(app)
        assert total >= 2                      # per-root history spans pages
    _drive(scenario)


# --- actions → modal → CLI verb (§1.6) -------------------------------------
def test_root_detail_scan_dedup_merge_verbs():
    async def scenario(app, pilot):
        await pilot.press("r")
        await pilot.press("r")
        await pilot.press("enter")
        for key, want in (("s", "packrat scan"), ("d", "packrat dedup"), ("m", "--into")):
            await pilot.press(key)
            await pilot.pause()
            assert _scr(app) == "MessageModal", (key, _scr(app))
            assert want in _modal_text(app), (key, _modal_text(app))
            await pilot.press("enter")
            await pilot.pause()
    _drive(scenario)


def test_pending_review_actions_map_to_verbs():
    async def scenario(app, pilot):
        # navigate the Roots list to Photos (the pending-review root)
        await pilot.press("r")
        await pilot.press("r")
        for _ in range(11):
            sel = [ln for ln in app.screen.current_frame.split("\n") if "▸" in ln]
            if sel and "Photos" in sel[0]:
                break
            await pilot.press("down")
        await pilot.press("enter")
        assert _scr(app) == "RootDetailScreen"
        assert "awaiting review" in app.screen.current_frame
        # [g] confirm → confirm modal → dedup --confirm
        await pilot.press("g")
        await pilot.pause()
        assert _scr(app) == "ConfirmModal"
        await pilot.press("y")
        await pilot.pause()
        assert "dedup Photos --confirm" in _modal_text(app)
        await pilot.press("enter")
        await pilot.pause()
        # [k] cancel run → dedup --cancel
        await pilot.press("k")
        await pilot.pause()
        assert _scr(app) == "ConfirmModal"
        await pilot.press("y")
        await pilot.pause()
        assert "dedup Photos --cancel" in _modal_text(app)
    _drive(scenario)


def test_queue_cancel_prioritize_cancelall_verbs():
    async def scenario(app, pilot):
        await pilot.press("q")
        await pilot.press("q")                 # QueueMax (focus=queued)
        await pilot.press("down")              # move within the Queued section
        # [c] cancel → confirm → jobs cancel <id>
        await pilot.press("c")
        await pilot.pause()
        assert _scr(app) == "ConfirmModal"
        await pilot.press("y")
        await pilot.pause()
        assert re.search(r"jobs cancel \d+", _modal_text(app))
        await pilot.press("enter")
        await pilot.pause()
        # [p] prioritize → jobs prioritize <id>
        await pilot.press("p")
        await pilot.pause()
        assert re.search(r"jobs prioritize \d+", _modal_text(app))
        await pilot.press("enter")
        await pilot.pause()
        # [x] cancel-all → confirm → cancel --all-queued
        await pilot.press("x")
        await pilot.pause()
        assert _scr(app) == "ConfirmModal"
        await pilot.press("y")
        await pilot.pause()
        assert "--all-queued" in _modal_text(app)
    _drive(scenario)


def test_add_root_register_verb():
    async def scenario(app, pilot):
        await pilot.press("r")
        await pilot.press("r")
        await pilot.press("a")
        assert _scr(app) == "AddRootScreen"
        await pilot.press("enter")
        await pilot.pause()
        assert _scr(app) == "MessageModal"
        assert "roots register" in _modal_text(app)
    _drive(scenario)


def test_add_root_tab_navigates_fields():
    """[Tab]/[Shift+Tab] cycle the add-root form fields; the ▸ marker follows."""
    async def scenario(app, pilot):
        await pilot.press("r")
        await pilot.press("r")
        await pilot.press("a")
        assert _scr(app) == "AddRootScreen"
        assert app.screen._field == "path"          # starts on the path field
        order = []
        for _ in range(4):
            await pilot.press("tab")
            order.append(app.screen._field)
        assert order == ["name", "kind", "scan", "path"], order   # wraps
        await pilot.press("shift+tab")
        assert app.screen._field == "scan"          # backwards
        # the ▸ cursor is actually shown on the focused field's line
        await pilot.press("tab")                    # → path
        assert any("▸" in ln and "Path" in ln for ln in app.screen.current_frame.split("\n"))
    _drive(scenario)


def test_add_root_space_toggles_and_typing_edits():
    """[Space] toggles Kind/scan; typing + backspace edit the focused text field."""
    async def scenario(app, pilot):
        await pilot.press("r")
        await pilot.press("r")
        await pilot.press("a")
        # Tab to Kind, toggle library→trash
        await pilot.press("tab")            # name
        await pilot.press("tab")            # kind
        assert app.screen.kind == "library"
        await pilot.press("space")
        assert app.screen.kind == "trash"
        # Tab to scan, toggle off
        await pilot.press("tab")            # scan
        assert app.screen.scan is True
        await pilot.press("space")
        assert app.screen.scan is False
        # Tab to name, type + backspace
        await pilot.press("tab")            # path
        await pilot.press("tab")            # name
        before = app.screen.root_name
        await pilot.press("Z")
        assert app.screen.root_name == before + "Z"
        await pilot.press("backspace")
        assert app.screen.root_name == before
        # the register verb reflects the edited state (trash kind)
        await pilot.press("enter")
        await pilot.pause()
        assert "--kind trash" in _modal_text(app)
    _drive(scenario)


def test_job_card_covers_every_shape():
    """Open a card for each recent-job shape; each renders inside the fixed frame."""
    async def scenario(app, pilot):
        await pilot.press("q")
        await pilot.press("q")                 # QueueMax
        # walk down through running+queued+recent, opening each card
        seen_titles = set()
        for _ in range(len(demo.RECENT) + len(demo.QUEUED) + 1):
            await pilot.press("enter")
            if _scr(app) == "JobCard":
                f = app.screen.current_frame
                assert len(f.split("\n")) == 24 and all(len(r) == 100 for r in f.split("\n"))
                seen_titles.add(f.split("\n")[0])
                await pilot.press("escape")
            await pilot.press("down")
        assert len(seen_titles) >= 5           # several distinct card shapes opened
    _drive(scenario)


# --- Ctrl-C quits from ANY screen (the hang regression) --------------------
def _quits_from(setup_keys) -> int:
    """Run the app to a screen via ``setup_keys``, press Ctrl-C, return exit code."""
    app = PackratApp(offline=True)

    async def auto(pilot):
        await pilot.pause()
        for k in setup_keys:
            await pilot.press(k)
            await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()

    app.run(headless=True, auto_pilot=auto)
    return app.return_code


def test_ctrl_c_quits_from_dashboard():
    assert _quits_from([]) == 0


def test_ctrl_c_quits_from_maximized_screens():
    assert _quits_from(["q", "q"]) == 0          # QueueMax
    assert _quits_from(["r", "r"]) == 0          # RootsMax
    assert _quits_from(["r", "r", "enter"]) == 0  # RootDetail


def test_ctrl_c_quits_from_modal():
    # The reported hang: Ctrl-C inside a confirm modal must still quit (priority
    # binding), not leave a blank, unresponsive terminal.
    assert _quits_from(["q", "down", "c"]) == 0   # ConfirmModal open


def test_rapid_action_keys_do_not_corrupt_stack():
    """Hammering c/p/x/q while a modal is open must not underflow the screen stack."""
    async def scenario(app, pilot):
        await pilot.press("q")                 # focus queue box
        await pilot.press("down")
        await pilot.press("c")                 # open ConfirmModal
        assert _scr(app) == "ConfirmModal"
        for k in ("c", "p", "x", "q", "r", "c", "c", "p"):
            await pilot.press(k)               # unbound-on-modal keys — must be inert
        assert _scr(app) == "ConfirmModal", "a key leaked to the screen beneath"
        assert len(app.screen_stack) == 3      # base + Dashboard + ConfirmModal
    _drive(scenario)


def test_modal_is_visible_not_collapsed():
    """A modal must render at a real size with visible content.

    Regression for the "TUI disappears, only acrylic shows" bug: ``width/height:
    auto`` collapsed the modal container to 0×0, so it covered the dashboard but
    drew nothing. Assert the modal-frame has a non-zero region and its box + prompt
    actually composite to the screen."""
    async def scenario(app, pilot):
        await pilot.press("q")
        await pilot.press("down")
        await pilot.press("c")                 # open ConfirmModal
        await pilot.pause()
        frame = app.screen.query_one("#modal-frame", Static)
        assert frame.region.width > 0 and frame.region.height > 0, "modal collapsed to 0×0"
        # content actually renders (box border + the confirm prompt)
        import io
        from rich.console import Console
        con = Console(file=io.StringIO(), force_terminal=True, color_system="truecolor", width=60)
        con.print(frame.render(), end="")
        out = con.file.getvalue()
        assert any(c in out for c in "┌┏"), "no box border rendered"
        assert "Cancel" in out, "modal prompt text not rendered"
    _drive(scenario)
