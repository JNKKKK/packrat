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
import sys

out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

W, H = 80, 24            # OUTER window — fixed for EVERY interface
CW = W - 2               # content columns inside the outer border


def pad(s, n):
    return s[:n] + " " * (n - len(s)) if len(s) < n else s[:n]


def box(title, lines, width, right="", heavy=False):
    tl, tr, bl, br, h, v = ("┏", "┓", "┗", "┛", "━", "┃") if heavy else ("┌", "┐", "└", "┘", "─", "│")
    lt = f"{h} {title} "
    rt = f" {right} {h}" if right else h
    core = lt + h * max(0, (width - 2 - len(lt) - len(rt))) + rt
    rows = [tl + pad(core, width - 2) + tr]
    for ln in lines:
        rows.append(v + " " + pad(ln, width - 4) + " " + v)
    rows.append(bl + h * (width - 2) + br)
    return rows


def hjoin(a, b, gap=1):
    hgt = max(len(a), len(b))
    wa, wb = len(a[0]), len(b[0])
    a = a + [" " * wa] * (hgt - len(a))
    b = b + [" " * wb] * (hgt - len(b))
    return [a[i] + " " * gap + b[i] for i in range(hgt)]


def screen(title, content, right=""):
    lt = f"─ {title} "
    rt = f" {right} ─" if right else "─"
    top = "┌" + pad(lt + "─" * max(0, (CW - len(lt) - len(rt))) + rt, CW) + "┐"
    body = ["│ " + pad(ln, CW - 2) + " │" for ln in content]
    while len(body) < H - 2:
        body.append("│ " + " " * (CW - 2) + " │")
    body = body[:H - 2]
    return "\n".join([top] + body + ["└" + "─" * CW + "┘"])


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
                              f"Last scan {lastscan}"], 29)


def rootrows():  # most-recently-registered first
    return ["  Downloads D:\\dump           ◐    241",
            "  _Trash    D:\\Backup\\_Trash    (trash)",
            "  Photos    E:\\Photos2         ◐  8,900",
            "  Camera    E:\\Photos          ◉ 26,150",
            "  iPhone    D:\\Backup\\iPhone    ◉ 98,412"]


# ============================ 1. DASHBOARD ============================
# 1.1 idle
roots = box("[R]oots", rootrows() + [DOTKEY], 45)
qbox = box("[Q]ueue", ["  idle — no jobs running or queued."], CW - 2)
c = LOGO + hjoin(collbox(), roots) + [""] + qbox + ["", FOOT_DASH]
block("### 1.1 — Idle (nothing running, no box focused)",
      screen("packrat", c, "v0.1.0 · daemon ● up"))

# 1.2 running
qrun = box("[Q]ueue", [
    "▶ scan iPhone     ███████████░░░░  67% 8,912/13,204 ETA 4m",
    "2 merge dump → Camera        queued · waiting for worker",
    "3 scan Photos                blocked: Photos pending dedup",
    "4 dedup Photos (confirm)     blocked: Photos pending dedup",
    "5 ‹merge dump → Camera (dry-run)›  queued · waiting",
], CW - 2)
c = LOGO + hjoin(collbox("now"), roots) + [""] + qrun + ["", FOOT_DASH]
block("### 1.2 — Work in flight (running job + durable backlog; no box focused)",
      screen("packrat", c, "v0.1.0 · daemon ● up"))

# 1.3 roots focused (heavy frame, cursor, arrow-navigable in place)
rootf = box("[R]OOTS", ["▸ Downloads D:\\dump           ◐    241",
                        "  _Trash    D:\\Backup\\_Trash    (trash)",
                        "  Photos    E:\\Photos2         ◐  8,900",
                        "  Camera    E:\\Photos          ◉ 26,150",
                        "  iPhone    D:\\Backup\\iPhone    ◉ 98,412",
                        DOTKEY], 45, heavy=True)
c = LOGO + hjoin(collbox(), rootf) + [""] + qbox + \
    ["", "↑/↓ select root   [Enter]/→ open detail   [r] maximize   Esc unfocus"]
block("### 1.3 — Roots box focused (one `[r]`): heavy frame, arrow-navigable in place",
      screen("packrat", c, "v0.1.0 · daemon ● up"))

# 1.4 queue focused
qf = box("[Q]UEUE", [
    "▶ scan iPhone     ███████████░░░░  67% 8,912/13,204 ETA 4m",
    "▸2 merge dump → Camera       queued · waiting for worker",
    "3 scan Photos                blocked: Photos pending dedup",
    "4 dedup Photos (confirm)     blocked: Photos pending dedup",
], CW - 2, heavy=True)
c = LOGO + hjoin(collbox("now"), roots) + [""] + qf + \
    ["", "↑/↓ select  [Enter] detail  [c] cancel  [p] prioritize  [x] all  Esc"]
block("### 1.4 — Queue box focused (one `[q]`): heavy frame, arrow-navigable in place",
      screen("packrat", c, "v0.1.0 · daemon ● up"))

# ============================ 2. ROOTS INTERFACE ============================
rlines = ["most-recently-registered first",
          "─" * (CW - 4),
          "▸ Downloads  D:\\dump              ◐    241  never deduped",
          "  _Trash     D:\\Backup\\_Trash     (trash)  —",
          "  Photos     E:\\Photos2           ◐  8,900  never deduped",
          "  Camera     E:\\Photos            ◉ 26,150  deduped Jul 12",
          "  iPhone     D:\\Backup\\iPhone     ◉ 98,412  deduped today",
          "",
          "◉ scanned + deduped   ◐ scanned only   ○ never scanned",
          "",
          "↑/↓ select   [Enter]/→ open detail   [a] add root   Esc back"]
block("## 2. Roots interface (maximized — second `[r]`)",
      screen("Roots", rlines, "daemon ● up"))

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
      "",
      "[s] scan  [d] dedup  [m] merge into…  [Enter] result  ↑/↓ jobs  Esc"]
block("## 3. Root detail interface (`[Enter]`/`→` on a root)\n\n"
      "### 3.1 — With a pending dedup/cleanup review (the actionable case)",
      screen("iPhone", d1, "D:\\Backup\\iPhone · library"))

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
      "",
      "[s] scan  [d] dedup  [m] merge into…  [Enter] result  ↑/↓ jobs  Esc"]
block("### 3.2 — No pending review (clean / normal)",
      screen("Camera", d2, "E:\\Photos · library"))

# ============================ 4. QUEUE INTERFACE ============================
qlines = ["Running + queued (runs top-down):",
          " ▶ #418 scan iPhone           67% 8,912/13,204 ETA 4m  running",
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
          "↑/↓ select  [c] cancel  [p] prioritize  [x] all  [Enter] detail  Esc"]
block("## 4. Queue interface (maximized — second `[q]`)",
      screen("Queue", qlines, "daemon ● up"))

# ============================ 5. JOB RESULT CARDS ============================
block("## 5. Job result / detail card (`[Enter]` on any job row)\n\n### 5.1 — scan (done)",
      screen("Job #418 · scan iPhone · done", [
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
          "  [undecodable] D:\\Backup\\iPhone\\2018\\IMG_9910.HEIC",
          "", "Esc back"], "Jul 15 09:04"))

block("### 5.2 — merge (done)",
      screen("Job #421 · merge dump → Camera · done", [
          "merge  E:\\iphone_dump  →  Camera",
          "─" * (CW - 4),
          "  240  copied (new)",
          "   18  skipped — exact-known (already in collection)",
          "    1  skipped — trashed (matched trash memory)",
          "    6  skipped — dup-in-source (byte-identical siblings)",
          "    2  collisions renamed       0  errors",
          "",
          "Source unchanged.  Next: `scan Camera` then `dedup Camera`.",
          "", "Esc back"], "Jul 14 22:10"))

block("### 5.3 — dedup (pending — awaiting review; carries actions)",
      screen("Job #430 · dedup Photos (analyze) · ⚠ awaiting review", [
          "dedup  E:\\Photos2       stage 2 of 3 · _suspect_recompression\\",
          "─" * (CW - 4),
          "  Stage 1 exact          ✓ applied  (12 deleted)",
          "▶ Stage 2 recompression   staged · 18 groups / 47 members (KEEP)",
          "  Stage 3 minor-edits    pending",
          "  review: E:\\Photos2\\_packrat_review\\_suspect_recompression\\",
          "",
          "[o] open review folder   [g] confirm this stage   [k] cancel run",
          "Esc back"], "today 11:31"))

block("### 5.4 — dedup (done) & already-clean",
      screen("Job #430 · dedup Photos (confirm) · done", [
          "All stages reviewed.",
          "52 deleted (12 exact · 40 near-dup) · 9 spared.",
          "Audit: %APPDATA%\\packrat\\audit\\dedup\\Photos\\430\\",
          "       (proposed.json / applied.json)",
          "", "Esc back"], "today 11:48"))

block("### 5.5 — already-clean · trash-refresh · untrash · error · interrupted",
      screen("Job #451 · dedup iPhone (analyze) · done", [
          "Already clean — no exact duplicates or near-dup groups.",
          "(counts as a successful dedup → sets this root's ◉ + deduped time)",
          "─" * (CW - 4),
          "trash refresh #402 · done:",
          "  9 new trashed · 3 flipped · 1 known · 12 emptied",
          "─" * (CW - 4),
          "untrash IMG_4471.jpg #500 · done:",
          "  1 reactivated · 0 forgotten · 0 already-active · 0 unknown",
          "─" * (CW - 4),
          "dedup Photos (confirm) #461 · ERROR:",
          "  ✗ nothing to confirm; run `dedup Photos` first.",
          "  (result_json NULL on error → shown from status + jobs.error)",
          "─" * (CW - 4),
          "scan iPhone #455 · INTERRUPTED — progress safe, re-run to resume.",
          "Esc back"], "history"))

out.flush()
