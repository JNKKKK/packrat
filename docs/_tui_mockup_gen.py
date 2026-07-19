# -*- coding: utf-8 -*-
r"""Generator for docs/M6-tui-mockups.md — renders every interface into an
IDENTICAL fixed W x H window, so the doc *proves* "the window size never changes".

Why generated, not hand-drawn: aligning ~15 ASCII interfaces to the same outer
dimensions by hand drifts (the first draft ranged 79–85 cols). Here `screen()`
pads/truncates every interface to the same frame, and `box()`/`hjoin()` compose
nested panels so columns line up.

Run:  uv run python docs/_tui_mockup_gen.py > docs/M6-tui-mockups.body.md
(then paste under the prose header, or just run to regenerate the frames).

Root-status dot (new): ◉ solid = scanned AND successfully deduped (a review_runs
row reached status='completed' — all stages, or already-clean); ◐ half = scanned
but never a successful dedup; ○ hollow = never scanned nor deduped.
"""
import io
import os
import sys

# Share the fixed-window constants + the pure grid helpers with the runtime, so
# the doc frames and the M6 TUI can NEVER drift on the numbers (M6-component-plan
# Resolved #1/#2). `pad`/`pager_line` here are literally the runtime's pure
# functions — a byte-identical frame is then a real assertion, not a coincidence.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from packrat.tui.framing import box, hjoin, screen  # noqa: E402
from packrat.tui.layout import pager_line  # noqa: E402
from packrat.tui.tokens import COLLECTION_W, CW, H, ROOTS_W, W  # noqa: E402,F401

out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def block(header, scr):
    out.write("\n" + header + "\n```\n" + scr + "\n```\n")


LOGO = ["", "   ___", "  (o.o)    p a c k r a t",
        r'  (>♦<)    "hoards everything, keeps a system"',
        "  /   \\    · 124,803 assets hoarded ·", ""]
FOOT_DASH = "[r] focus Roots   [q] focus Queue (again = maximize)   Ctrl-C quit"
DOTKEY = "  ◉ deduped   ◐ scanned only   ○ never"


def collbox(lastscan="2h ago"):
    return box("Collection", ["Assets    124,803", "  photos  111,240",
                              "  videos   13,563", "Trashed     3,904",
                              f"Last scan {lastscan}"], COLLECTION_W)


# dashboard row = Collection(29) + gap(1) + Roots, and must equal the inner content
# width screen() pads to (CW-2), or the hjoin overflows and the right border is clipped.
# ROOTS_W is imported from packrat.tui.tokens (same derivation) — not redefined here.


def rootrows(cursor=None):  # most-recently-registered first
    names = [("Downloads", "D:\\dump", "◐", "241"),
             ("_Trash", "D:\\Backup\\_Trash", " ", "(trash)"),
             ("Photos", "E:\\Photos2", "◐", "8,900"),
             ("Camera", "E:\\Photos", "◉", "26,150"),
             ("iPhone", "D:\\Backup\\iPhone", "◉", "98,412")]
    rows = []
    for i, (nm, pth, dot, cnt) in enumerate(names):
        cur = "▸" if cursor == i else " "
        rows.append(f"{cur} {nm:<9} {pth:<20} {dot} {cnt:>7}")
    return rows


# ============================ 1. DASHBOARD ============================
# 1.1 idle
roots = box("[R]oots", rootrows() + [DOTKEY], ROOTS_W)
qbox = box("[Q]ueue", ["  idle — no jobs running or queued."], CW - 2)
c = LOGO + hjoin(collbox(), roots) + [""] + qbox
block("### 1.1 — Idle (nothing running, no box focused)",
      screen("packrat", c, "v0.1.0 · daemon ● up", footer=FOOT_DASH))

# 1.2 running
# dashboard queue box is a FIXED-HEIGHT preview: logo+collection eat 15 rows, so it
# fits at most 4 item rows + 2 borders before the pinned footer. A 5th row would push
# the box's bottom border past the frame (silently truncated). Overflow lives in §4.
qrun = box("[Q]ueue", [
    "▶ scan iPhone     ███████████░░░░  67% 8,912/13,204 ETA 4m",
    "2 merge dump → Camera        queued · waiting for worker",
    "3 scan Photos                blocked: Photos pending dedup",
    "4 dedup Photos (confirm)     blocked: Photos pending dedup",
], CW - 2)
c = LOGO + hjoin(collbox("now"), roots) + [""] + qrun
block("### 1.2 — Work in flight (running job + durable backlog; no box focused)",
      screen("packrat", c, "v0.1.0 · daemon ● up", footer=FOOT_DASH))

# 1.3 roots focused (heavy frame, cursor, arrow-navigable in place)
# focused (compact) boxes carry the paginator in the TITLE bar, not a content row
rootf = box("[R]OOTS   page 1/1", rootrows(cursor=0) + [DOTKEY], ROOTS_W, heavy=True)
c = LOGO + hjoin(collbox(), rootf) + [""] + qbox
block("### 1.3 — Roots box focused (one `[r]`): heavy frame, arrow-navigable in place",
      screen("packrat", c, "v0.1.0 · daemon ● up",
             footer="↑/↓ select root   [Enter] open detail   ←/→ page   [r] maximize   Esc unfocus"))

# 1.4 queue focused
qf = box("[Q]UEUE   page 1/1", [
    "▶ scan iPhone     ███████████░░░░  67% 8,912/13,204 ETA 4m",
    "▸2 merge dump → Camera       queued · waiting for worker",
    "3 scan Photos                blocked: Photos pending dedup",
    "4 dedup Photos (confirm)     blocked: Photos pending dedup",
], CW - 2, heavy=True)
c = LOGO + hjoin(collbox("now"), roots) + [""] + qf
block("### 1.4 — Queue box focused (one `[q]`): heavy frame, arrow-navigable in place",
      screen("packrat", c, "v0.1.0 · daemon ● up",
             footer="↑/↓ select  [Enter] detail  ←/→ page  [c] cancel  [p] prioritize  [x] all  Esc"))

# ============================ 2. ROOTS INTERFACE ============================
rlines = ["[S]ort: most recent registered  (→ most assets → photos → videos)",
          "─" * (CW - 4),
          "▸ Downloads  D:\\dump              ◐    241  never deduped",
          "  _Trash     D:\\Backup\\_Trash     (trash)  —",
          "  Photos     E:\\Photos2           ◐  8,900  never deduped",
          "  Camera     E:\\Photos            ◉ 26,150  deduped Jul 12",
          "  iPhone     D:\\Backup\\iPhone     ◉ 98,412  deduped today",
          pager_line(CW - 2),          # paginator drawn directly under the list
          "",
          "◉ scanned + deduped   ◐ scanned only   ○ never scanned"]
block("## 2. Roots interface (maximized — second `[r]`)\n\n### 2.1 — Root list",
      screen("packrat · Roots", rlines, "daemon ● up",
             footer="↑/↓ select   [Enter] open detail   ←/→ page   [s] sort   [a] add root   Esc back"))

# 2.2 add-root form (register). ▸ marks the focused field; [Tab] moves between fields.
alines = ["Register a new root (metadata-only; scan it afterward).",
          "─" * (CW - 4),
          "  Path   ▸ \\\\tubie_nas\\Res-v2\\NewPhone____________________________",
          "           (must exist, be a readable directory, not overlap a root)",
          "",
          "  Name     [ NewPhone ]   ‹defaults to the folder leaf; must be unique›",
          "",
          "  Kind     (•) library    ( ) trash",
          "",
          "  [x] scan immediately after registering   ( ) --full   ( ) --embed",
          "",
          "  ‹trash roots are never scanned; --full/--embed apply only with scan›"]
block("### 2.2 — Add a root (`[a]` — the `roots register` flow)",
      screen("packrat · Roots · add", alines, "daemon ● up",
             footer="[Tab] next field   type to edit   [Enter] register   Esc cancel"))

# ============================ 3. ROOT DETAIL ============================
d1 = ["assets  98,412  (photos 92,110 · videos 6,302)     files 98,540",
      "scanned 2h ago    last full scan Jul 10    deduped today 11:31",
      "─" * (CW - 4),
      "⚠ dedup — awaiting review (stage 2 of 3)",
      "    240 to delete (exact) · 18 groups / 47 members (default-keep)",
      "    review: D:\\Backup\\iPhone\\_packrat_review\\_suspect_recompression\\",
      "    [o] open in Explorer   [g] confirm stage   [k] cancel run",
      "─" * (CW - 4),
      "Jobs (newest first):",
      " ▸ dedup  ⚠ awaiting review · 240 delete · 18 grp/47 mbr   11:31",
      "   scan   done     +412 new · 3 undecodable                09:04",
      "   merge  done     240 copied · 1 trashed skipped          Jul 14",
      "   scan   interrupted — re-run to resume                   Jul 13",
      pager_line(CW - 2)]         # paginator under the jobs list
block("## 3. Root detail interface (`[Enter]` on a root)\n\n"
      "### 3.1 — With a pending dedup/cleanup review (the actionable case)",
      screen("packrat · iPhone", d1, "D:\\Backup\\iPhone · library",
             footer="[s] scan  [d] dedup  [m] merge from…  [Enter] result  ↑/↓ jobs  ←/→ page  Esc"))

d2 = ["assets  26,150  (photos 25,900 · videos 250)      files 26,150",
      "scanned 1d ago    last full scan Jul 08    deduped Jul 12",
      "─" * (CW - 4),
      "No pending review.   (cleaned: never)",
      "─" * (CW - 4),
      "Last scan (Jul 15 09:31): +26 new · 0 exact-dup · 0 undecodable",
      "Jobs (newest first):",
      " ▸ scan   done     +26 new                                 Jul 15",
      "   dedup  done     already clean                           Jul 12",
      "   merge  done     1,204 copied · 12 exact-known           Jul 08",
      pager_line(CW - 2)]         # paginator under the jobs list
block("### 3.2 — No pending review (clean / normal)",
      screen("packrat · Camera", d2, "E:\\Photos · library",
             footer="[s] scan  [d] dedup  [m] merge from…  [Enter] result  ↑/↓ jobs  ←/→ page  Esc"))

# 3.3 — merge FROM picker (opened by [m] on the root detail). This root is the fixed
# DEST; the user picks the SOURCE via a radio: (•) Registered root → a paginated roots
# list (this root excluded), or ( ) External folder → a free-typed path field. Copies
# only files new to the whole collection, by exact hash. ▸ = selection cursor.
# --- variant A: "Registered root" selected → paginated roots list (library roots
#     only — a trash inbox is never a merge source; the dest root is excluded too) ---
d3 = ["Copy files new to the whole collection INTO this root, by exact hash.",
      "─" * (CW - 4),
      "Destination   Camera   E:\\Photos",
      "",
      "Source   (•) Registered root     ( ) External folder",
      "─" * (CW - 4),
      " ▸ Downloads   D:\\dump                            241 assets",
      "   Photos      E:\\Photos2                       8,900 assets",
      "   iPhone      D:\\Backup\\iPhone                98,412 assets",
      pager_line(CW - 4, 1, 1),
      "",
      "[ ] --dry-run   (classify + preview counts; copies nothing — still empties trash)"]
block("### 3.3 — Merge from… (`[m]` — pick the source; this root is the destination)\n\n"
      "**Variant A — `(•) Registered root`:** a paginated roots list to pick from.",
      screen("packrat · Camera · merge from", d3, "E:\\Photos · library",
             footer="↑/↓ pick   ←/→ page   [Tab] switch source   [ ] --dry-run   [Enter] merge   Esc"))

# --- variant B: "External folder" selected → free-typed path field ---
d3b = ["Copy files new to the whole collection INTO this root, by exact hash.",
       "─" * (CW - 4),
       "Destination   Camera   E:\\Photos",
       "",
       "Source   ( ) Registered root     (•) External folder",
       "─" * (CW - 4),
       "  Path   ▸ E:\\iphone_dump________________________________________",
       "           (any readable folder — a temp export, a card, a share)",
       "",
       "",
       "",
       "",
       "[ ] --dry-run   (classify + preview counts; copies nothing — still empties trash)"]
block("**Variant B — `( ) External folder`:** type an arbitrary path (need not be a root).",
      screen("packrat · Camera · merge from", d3b, "E:\\Photos · library",
             footer="type to edit path   [Tab] switch source   [ ] --dry-run   [Enter] merge   Esc"))

# ============================ 4. QUEUE INTERFACE ============================
qlines = ["Running:",
          " ▶ #418 scan iPhone           67% 8,912/13,204 ETA 4m  running",
          "",
          "Queued (runs top-down):",
          "   #419 merge dump → Camera        queued · waiting",
          "   #420 scan Photos                blocked: Photos pending dedup",
          "   #421 dedup Photos (confirm)     blocked: Photos pending dedup",
          "",
          "Recent:",
          "   #417 dedup Photos (confirm) done   52 deleted · 9 spared 11:48",
          "   #416 cleanup iPhone (exact·delete) done  3 deleted      10:20",
          "   #415 scan Camera            done   +26 new             09:31",
          "   #414 merge dump → iPhone    done   240 copied          Jul 14",
          "   #413 ‹scan iPhone (dry-run)› done  2,110 would index    Jul 14",
          "   #412 dedup iPhone (cancel)  cancelled  —               Jul 13",
          "   #411 scan Photos            interrupted  re-run        Jul 13",
          "",
          pager_line(CW - 2)]         # paginator under the combined running/recent list
block("## 4. Queue interface (maximized — second `[q]`)",
      screen("packrat · Queue", qlines, "daemon ● up",
             footer="↑/↓ select  ←/→ page  [c] cancel  [p] prioritize  [x] all  [Enter] detail  Esc"))

# ============================ 5. JOB RESULT CARDS ============================
# 5.1 running: rendered LIVE from the SSE progress stream (bar/counts/ETA — no
# result_json yet). On completion the stream fires a terminal event → the SAME card
# swaps in place to the result view (5.2), dropping the bar for the final tallies.
block("## 5. Job result / detail card (`[Enter]` on any job row)\n\n"
      "### 5.1 — scan (running — live progress; `[Enter]` on the running job row)",
      screen("packrat · Job #418 · scan iPhone · running", [
          "scan  D:\\Backup\\iPhone                          (incremental)",
          "─" * (CW - 4),
          "▶ " + "█" * 20 + "░" * 10 + "  67%   8,912 / 13,204 files   ETA 4m",
          "",
          "  live so far:",
          "    +389  new assets              2  undecodable",
          "  8,510  skipped (fast-path)      0  read errors",
          "",
          "  now scanning: D:\\Backup\\iPhone\\2021\\IMG_2231.HEIC",
          "",
          "  ‹live — refreshes as the job runs; auto-shows the result card on completion›"],
          "started 09:04", footer="[c] cancel job   Esc back"))

block("### 5.2 — scan (done)",
      screen("packrat · Job #418 · scan iPhone · done", [
          "scan  D:\\Backup\\iPhone                       (incremental)",
          "─" * (CW - 4),
          "  412  new assets           0  exact-dup instances",
          "    0  filled-in fingerprints  17  identified as trash",
          "    3  undecodable           0  read errors",
          "8,912  skipped (fast-path)   2  instances gone (1 forgotten)",
          "",
          "Problem files (3):",
          "  [undecodable] D:\\Backup\\iPhone\\2019\\IMG_0032.HEIC",
          "       PIL: cannot identify image file",
          "  [undecodable] D:\\Backup\\iPhone\\clips\\old.3gp",
          "  [undecodable] D:\\Backup\\iPhone\\2018\\IMG_9910.HEIC"],
          "Jul 15 09:04", footer="Esc back"))

block("### 5.3 — merge (done)",
      screen("packrat · Job #421 · merge dump → Camera · done", [
          "merge  E:\\iphone_dump  →  Camera",
          "─" * (CW - 4),
          "  240  copied (new)",
          "   18  skipped — exact-known (already in collection)",
          "    1  skipped — trashed (matched trash memory)",
          "    6  skipped — dup-in-source (byte-identical siblings)",
          "    2  collisions renamed       0  errors",
          "",
          "Source unchanged.  Next: `scan Camera` then `dedup Camera`."],
          "Jul 14 22:10", footer="Esc back"))

block("### 5.4 — dedup (pending — awaiting review; carries actions)",
      screen("packrat · Job #430 · dedup Photos (analyze) · ⚠ awaiting review", [
          "dedup  E:\\Photos2       stage 2 of 3 · _suspect_recompression\\",
          "─" * (CW - 4),
          "  Stage 1 exact          ✓ applied  (12 deleted)",
          "▶ Stage 2 recompression   staged · 18 groups / 47 members (KEEP)",
          "  Stage 3 minor-edits    pending",
          "  review: E:\\Photos2\\_packrat_review\\_suspect_recompression\\",
          "",
          "[o] open review folder   [g] confirm this stage   [k] cancel run"],
          "today 11:31", footer="Esc back"))

block("### 5.5 — dedup (done) & already-clean",
      screen("packrat · Job #430 · dedup Photos (confirm) · done", [
          "All stages reviewed.",
          "52 deleted (12 exact · 40 near-dup) · 9 spared.",
          "Audit: %APPDATA%\\packrat\\audit\\dedup\\Photos\\430\\",
          "       (proposed.json / applied.json)"],
          "today 11:48", footer="Esc back"))

# 5.6 is NOT a real screen — it is a COMPACT OVERVIEW that stacks five short result
# shapes (each its own single-job card in the real TUI) into one frame for review, so
# the doc doesn't spend five near-empty frames on them. The title/lead say so.
block("### 5.6 — compact overview (NOT one screen): already-clean · trash-refresh · "
      "untrash · error · interrupted",
      screen("packrat · result-card shapes (reference — not a real screen)", [
          "‹Compact overview: five short result shapes stacked here for review — in the",
          " real TUI each is its OWN single-job card ([Enter] on that job), never one window.›",
          "─" * (CW - 4),
          "dedup iPhone (analyze) #451 · done — already clean:",
          "  Already clean — no exact duplicates or near-dup groups.",
          "  (counts as a successful dedup → sets this root's ◉ + deduped time)",
          "─" * (CW - 4),
          "trash refresh #402 · done:  9 new trashed · 3 flipped · 1 known · 12 emptied",
          "─" * (CW - 4),
          "untrash IMG_4471.jpg #500 · done:  1 reactivated · 0 forgotten · 0 active · 0 unknown",
          "─" * (CW - 4),
          "dedup Photos (confirm) #461 · ERROR:  ✗ nothing to confirm; run `dedup Photos` first.",
          "  (result_json NULL on error → shown from status + jobs.error)",
          "─" * (CW - 4),
          "scan iPhone #455 · INTERRUPTED — progress safe, re-run to resume."],
          "reference", footer="Esc back"))

out.flush()
