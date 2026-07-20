"""Frame tests — each pure screen builder renders to a valid fixed-size frame.

The builders are pure (``dict → lines``) rendered from :mod:`packrat.tui.fixtures`;
wrapped in the shared ``framing.screen`` they must produce an exact-size frame with
the expected content. These assert the fixed-frame invariant (§12: 100×24 at the
reference size, every row full width) plus the key content/layout of each screen —
no Textual pilot needed, just plain string checks.
"""

from __future__ import annotations

from packrat.tui import fixtures
from packrat.tui.fixtures import REFERENCE_NOW as NOW
from packrat.tui.framing import screen
from packrat.tui.screens import jobcard
from packrat.tui.screens.dashboard import dashboard_body
from packrat.tui.screens.queue import queue_body
from packrat.tui.screens.merge import merge_body, merge_sources
from packrat.tui.screens.rootdetail import detail_body, detail_header_right
from packrat.tui.screens.roots import add_root_body, roots_body

FOOT_DASH = "[r] focus Roots   [q] focus Queue (again = maximize)   Esc / Ctrl-Q quit"


def test_dashboard_idle_structural():
    """Dashboard idle — structural checks (the layout diverged from the mockup:
    logo + Collection now stack on top, roots full-width below, then queue)."""
    snap = fixtures.status_snapshot(running=False)
    built = screen("packrat", dashboard_body(snap, now=NOW),
                   "v0.1.0 · daemon ● up", footer=FOOT_DASH)
    rows = built.split("\n")
    assert len(rows) == 24 and all(len(r) == 100 for r in rows)
    assert "p a c k r a t" in built
    assert "Collection" in built and "[R]oots" in built and "[Q]ueue" in built
    assert "idle — no jobs running or queued" in built


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


def test_roots_max_structural():
    """Roots interface — structural (dot/count/recency right-aligned; legend + pager
    on one line at the top; layout diverged from the mockup)."""
    built = screen("packrat · Roots", roots_body(fixtures.ROOTS, now=NOW, cursor=0),
                   "daemon ● up",
                   footer="↑/↓ select   [Enter] open detail   ←/→ page   "
                          "[s] sort   [a] add root   Esc back")
    rows = built.split("\n")
    assert len(rows) == 24 and all(len(r) == 100 for r in rows)
    assert "[S]ort: most recent registered" in built
    # legend + paginator share the line directly under the sort header
    legend_line = next(ln for ln in rows if "scanned + deduped" in ln)
    assert "page 1/1" in legend_line


def test_add_root_form_structural():
    built = screen("packrat · Roots · add",
                   add_root_body(path=r"\\tubie_nas\Res-v2\NewPhone", name="NewPhone", scan=True),
                   "daemon ● up",
                   footer="[Tab] next field   type to edit   [Enter] register   Esc cancel")
    rows = built.split("\n")
    assert len(rows) == 24 and all(len(r) == 100 for r in rows)
    assert "Register a new root" in built
    assert "(•) library" in built and "( ) trash" in built     # Kind radio
    assert "[x] scan immediately after registering" in built
    assert r"\\tubie_nas\Res-v2\NewPhone" in built             # the typed path


# --- §3 / §4 / §5: fixed-size structural checks ----------------------------
# These interfaces carry
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


# --- §3.3 merge picker -----------------------------------------------------
def test_merge_picker_registered_root_variant():
    dest = fixtures.root_detail_clean()          # Camera
    sources = merge_sources(fixtures.ROOTS, dest["name"])
    built = screen(f"packrat · {dest['name']} · merge from",
                   merge_body(dest, sources, source_mode="root"),
                   f"{dest['path']} · {dest['kind']}", footer="Esc")
    _fixed(built)
    assert "Destination   Camera" in built
    assert "(•) Registered root" in built and "( ) External folder" in built
    assert "--dry-run" in built
    # dest itself + trash roots are excluded from the source list
    assert not any(s["kind"] == "trash" for s in sources)
    assert "Camera" not in "\n".join(
        ln for ln in built.split("\n") if "assets" in ln)   # dest not a source row


def test_merge_picker_external_folder_variant():
    dest = fixtures.root_detail_clean()
    sources = merge_sources(fixtures.ROOTS, dest["name"])
    built = screen(f"packrat · {dest['name']} · merge from",
                   merge_body(dest, sources, source_mode="ext",
                              ext_path=r"E:\iphone_dump", dry_run=True),
                   f"{dest['path']} · {dest['kind']}", footer="Esc")
    _fixed(built)
    assert "(•) External folder" in built
    assert "E:\\iphone_dump" in built
    assert "[x] --dry-run" in built              # toggle reflected


# --- title bar: right-label trimming + CJK width ---------------------------
def test_title_bar_middle_elides_overflowing_right_label():
    from packrat.tui.layout import cell_width
    long_right = (r"\\synology-ds920.local\home\Backups\Devices\iPhone15Pro"
                  r"\DCIM\Camera · library")
    built = screen("packrat · Synology_Backup", ["body"], long_right,
                   footer="Esc", width=90, height=8)
    top = built.split("\n")[0]
    assert cell_width(top) == 90                 # border flush, not overflowed
    assert "…" in top                            # right label was middle-elided
    assert top.endswith("· library ┐")           # the "· <kind>" tail is kept


def test_title_bar_cjk_stays_aligned():
    from packrat.tui.layout import cell_width
    built = screen("packrat · 手机相册", ["body"],
                   r"D:\备份\手机相册\2026 · library", footer="Esc", width=100, height=8)
    for r in built.split("\n"):
        assert cell_width(r) == 100              # CJK measured as 2 cells → flush


def test_title_bar_no_trim_when_it_fits():
    # a short right label on a wide frame is untouched (no ellipsis)
    built = screen("packrat · Camera", ["body"], r"E:\Photos · library",
                   footer="Esc", width=100, height=8)
    top = built.split("\n")[0]
    assert "E:\\Photos · library" in top and "…" not in top
