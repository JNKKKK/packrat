"""Golden-frame tests — live builders render byte-equal to the mockup frames.

The crux of the component-plan's testing strategy (§Testing): the doc frames in
``docs/M6-tui-mockups.md`` and the live render both come from **one source**
(:mod:`packrat.tui.fixtures`), so asserting a builder's output equals the doc's
frame slice keeps the doc honest *and* the UI standardized in one assertion.

Each builder is pure (dict → lines); we wrap it in the shared ``framing.screen``
with the same title/right/footer the generator uses, and compare to the fenced
block in the doc. These need no Textual pilot — plain string equality.
"""

from __future__ import annotations

from pathlib import Path

from packrat.tui import fixtures
from packrat.tui.fixtures import REFERENCE_NOW as NOW
from packrat.tui.framing import screen
from packrat.tui.screens import jobcard
from packrat.tui.screens.dashboard import dashboard_body
from packrat.tui.screens.queue import queue_body
from packrat.tui.screens.rootdetail import detail_body, detail_header_right
from packrat.tui.screens.roots import add_root_body, roots_body

DOC = Path(__file__).resolve().parents[1] / "docs" / "M6-tui-mockups.md"


def _blocks() -> list[str]:
    text = DOC.read_text(encoding="utf-8").replace("\r\n", "\n")
    blocks, cur = [], None
    for ln in text.split("\n"):
        if ln.strip() == "```":
            if cur is None:
                cur = []
            else:
                blocks.append("\n".join(cur))
                cur = None
        elif cur is not None:
            cur.append(ln)
    return blocks


def _frame(needle: str) -> str:
    """The doc frame containing `needle` (a unique substring of that interface)."""
    for b in _blocks():
        if needle in b:
            return b
    raise AssertionError(f"no doc frame contains {needle!r}")


def _assert_frame(built: str, needle: str) -> None:
    exp = _frame(needle)
    if built != exp:
        diff = []
        for i, (x, y) in enumerate(zip(built.split("\n"), exp.split("\n"))):
            if x != y:
                diff.append(f"  row {i}:\n    built: {x!r}\n    doc  : {y!r}")
        raise AssertionError("frame mismatch (regenerate or fix builder):\n" + "\n".join(diff))


FOOT_DASH = "[r] focus Roots   [q] focus Queue (again = maximize)   Ctrl-C quit"


def test_dashboard_idle_matches_frame_1_1():
    snap = fixtures.status_snapshot(running=False)
    built = screen("packrat", dashboard_body(snap, now=NOW),
                   "v0.1.0 · daemon ● up", footer=FOOT_DASH)
    _assert_frame(built, "idle — no jobs running or queued")


def test_dashboard_running_structural():
    """§1.2 (work in flight): structural checks, not byte-exact.

    The mockup's queue preview uses hand-authored positional numbering + shortened
    reasons that don't derive cleanly from the data model, so we assert the frame's
    invariants (fixed size, running bar with ETA, blocked rows present) rather than
    byte-equality. The stable chrome/content frames are asserted byte-exact above.
    """
    snap = fixtures.status_snapshot(running=True)
    built = screen("packrat", dashboard_body(snap, now=NOW),
                   "v0.1.0 · daemon ● up", footer=FOOT_DASH)
    rows = built.split("\n")
    assert len(rows) == 24 and all(len(r) == 100 for r in rows)
    assert "▶ scan iPhone" in built and "ETA" in built
    assert "67%" in built and "8,912/13,204" in built
    assert "blocked: Photos pending dedup" in built
    assert "queued · waiting for worker" in built


def test_roots_max_matches_frame_2_1():
    built = screen("packrat · Roots", roots_body(fixtures.ROOTS, now=NOW, cursor=0),
                   "daemon ● up",
                   footer="↑/↓ select   [Enter] open detail   ←/→ page   "
                          "[s] sort   [a] add root   Esc back")
    _assert_frame(built, "[S]ort: most recent registered")


def test_add_root_form_matches_frame_2_2():
    built = screen("packrat · Roots · add",
                   add_root_body(path=r"\\tubie_nas\Res-v2\NewPhone", name="NewPhone", scan=True),
                   "daemon ● up",
                   footer="[Tab] next field   type to edit   [Enter] register   Esc cancel")
    _assert_frame(built, "Register a new root")


# --- §3 / §4 / §5: fixed-size structural checks ----------------------------
# The stable chrome/content frames above are byte-exact; these interfaces carry
# hand-authored illustrative detail in the mockups, so we assert the fixed-frame
# invariant + key content rather than byte-equality (same policy as §1.2).
def _fixed(frame: str) -> list[str]:
    rows = frame.split("\n")
    assert len(rows) == 24, f"{len(rows)} rows"
    assert all(len(r) == 100 for r in rows), "a row is not 100 cells"
    return rows


def test_root_detail_pending_fits_and_shows_review():
    d = fixtures.root_detail_pending()
    jobs = [fixtures.DEDUP_PENDING, fixtures.SCAN_DONE,
            fixtures.MERGE_DONE, fixtures.SCAN_INTERRUPTED]
    built = screen(f"packrat · {d['name']}", detail_body(d, now=NOW, jobs=jobs),
                   detail_header_right(d), footer="Esc")
    _fixed(built)
    assert "⚠ dedup — awaiting review (stage 2 of 3)" in built
    assert "240 to delete (exact) · 18 groups / 47 members" in built
    assert "[o] open in Explorer" in built


def test_root_detail_clean_fits_and_shows_no_review():
    d = fixtures.root_detail_clean()
    built = screen(f"packrat · {d['name']}", detail_body(d, now=NOW, jobs=[fixtures.SCAN_DONE]),
                   detail_header_right(d), footer="Esc")
    _fixed(built)
    assert "No pending review." in built


def test_queue_interface_fits_and_has_three_sections():
    built = screen("packrat · Queue",
                   queue_body(fixtures.RUNNING_SCAN, fixtures.queued_jobs(),
                              fixtures.recent_jobs(), now=NOW, focus="queued"),
                   "daemon ● up", footer="Esc")
    _fixed(built)
    # three per-section headers with their focus accelerators; queued is focused
    # (uppercased) here, running/recent are not.
    assert "[R]unning:" in built
    assert "[Q]UEUED (RUNS TOP-DOWN):" in built     # focused → uppercased
    assert "Rec[e]nt:" in built
    assert "▶ #418 scan iPhone" in built
    # each section has its OWN paginator (independent windows) → two "page i/N"
    assert built.count("page ") >= 2


def test_scan_result_card_fits():
    j = fixtures.SCAN_DONE
    built = screen(jobcard.card_title(j), jobcard.card_body(j, now=NOW), "Jul 15", footer="Esc back")
    _fixed(built)
    assert "Job #418 · scan iPhone · done" in built
    assert "new assets" in built


def test_merge_result_card_fits():
    j = fixtures.MERGE_DONE
    built = screen(jobcard.card_title(j), jobcard.card_body(j, now=NOW), "Jul 14", footer="Esc back")
    _fixed(built)
    assert "copied (new)" in built


def test_running_card_shows_live_bar():
    j = fixtures.RUNNING_SCAN
    built = screen(jobcard.card_title(j), jobcard.card_body(j, now=NOW), "started", footer="Esc")
    _fixed(built)
    assert "running" in built and "‹live" in built


def test_dedup_pending_card_carries_actions():
    j = fixtures.DEDUP_PENDING
    built = screen(jobcard.card_title(j), jobcard.card_body(j, now=NOW), "today", footer="Esc")
    _fixed(built)
    assert "awaiting review" in built
    assert "[o] open review folder" in built and "[g] confirm this stage" in built


def test_error_card_renders_from_status():
    j = fixtures.CLEANUP_ERROR
    built = screen(jobcard.card_title(j), jobcard.card_body(j, now=NOW), "today", footer="Esc")
    _fixed(built)
    assert "nothing to confirm" in built     # rendered from jobs.error (result_json NULL)


def test_interrupted_card_renders_from_status():
    j = fixtures.SCAN_INTERRUPTED
    built = screen(jobcard.card_title(j), jobcard.card_body(j, now=NOW), "Jul 13", footer="Esc")
    _fixed(built)
    assert "interrupted" in built and "re-run to resume" in built


def test_already_clean_dedup_card():
    j = fixtures.DEDUP_CLEAN
    built = screen(jobcard.card_title(j), jobcard.card_body(j, now=NOW), "Jul 13", footer="Esc")
    _fixed(built)
    assert "already clean" in built
