# M6 — TUI mockups (review before implementing)

ASCII mockups of every packrat TUI interface, for review **before** M6 is built.
Grounded in PLAN §12 and the **actual** shipped data surfaces (M0–M5): the read-only
queries (`status_snapshot` / `root_detail` / `recent_jobs` / `root_jobs` /
`queued_jobs`), the SSE progress stream, and the per-op `jobs.result_json` shapes.

> **Fixed window size is a TUI design requirement, not just a mockup property** — see
> **PLAN §12 "Fixed layout"** (the authoritative statement: every interface renders in the
> same fixed 100×24 region; navigation swaps content in place, never resizes/reflows the
> frame; long lists scroll within their panel). **These frames are generated** by
> `docs/_tui_mockup_gen.py`, which renders every interface into that identical 100×24 frame
> so the doc *mechanically demonstrates* the requirement. Regenerate with
> `uv run python docs/_tui_mockup_gen.py`. (The box-drawing + `◉◐○` glyphs are each one
> terminal cell; they align in a real monospace TUI font even if a proportional preview
> nudges them.)

**Backend readiness:** nothing here needs new backend **except two small additions**,
both flagged in Open Questions: (a) the Roots list needs `last_dedup_at` per root to
draw the status dot (today only `root_detail` exposes it, not `roots_snapshot`); (b)
roots shown most-recently-registered-first. ETA is computed TUI-side; result cards
render `result_json` (no log tail); the old "duplicates (est)" stat is dropped. Every
action maps to an existing CLI verb (design tenet §1.6).

## Conventions

- `▸` selection cursor · `▶` running job · `⚠` needs attention.
- **Root status dot** (new — the freshness/health indicator in the Roots panels):
  - **`◉` solid** — scanned **and** successfully deduped (a `review_runs` row reached
    `status='completed'`: it went through **all stages**, or was already-clean).
  - **`◐` half** — scanned, but **never** a successful dedup yet.
  - **`○` hollow** — **never** scanned nor deduped (a freshly-registered root).
  - Data: `last_scan_at` (scanned-ever?) + `last_dedup_at` (newest completed dedup) per
    root. "Successful dedup" = the same all-stages-or-already-clean rule as §11's
    "deduped <age>". Purely display-derived; no schema change.
- **Focus model:** the dashboard's **Roots** and **Queue** boxes are focus targets. An
  unfocused box has a **light** frame `┌─ [R]oots ─┐`; the **focused** box gets a
  **heavy** frame + capitalized title `┏━ [R]OOTS ━┓` and a `▸` cursor.
  - `[r]` / `[q]` once → **focus** that box; `↑/↓` navigate its rows in place.
  - `[r]` / `[q]` **again** (while focused) → **maximize** into the full Roots interface
    (§2) / Queue interface (§4).
  - `Esc` un-focuses (or returns from a maximized interface); `Ctrl-C` quits (since
    `[q]` now focuses the Queue).
- Data source for each interface is called out in **‹notes›** beneath it.

---



### 1.1 — Idle (nothing running, no box focused)
```
┌─ packrat ───────────────────────────────────────────────────────────────── v0.1.0 · daemon ● up ─┐
│                                                                                                  │
│    ___                                                                                           │
│   (o.o)    p a c k r a t                                                                         │
│   (>♦<)    "hoards everything, keeps a system"                                                   │
│   /   \    · 124,803 assets hoarded ·                                                            │
│                                                                                                  │
│ ┌─ Collection ──────────────┐ ┌─ [R]oots ──────────────────────────────────────────────────────┐ │
│ │ Assets    124,803         │ │   Downloads D:\dump              ◐     241                     │ │
│ │   photos  111,240         │ │   _Trash    D:\Backup\_Trash       (trash)                     │ │
│ │   videos   13,563         │ │   Photos    E:\Photos2           ◐   8,900                     │ │
│ │ Trashed     3,904         │ │   Camera    E:\Photos            ◉  26,150                     │ │
│ │ Last scan 2h ago          │ │   iPhone    D:\Backup\iPhone     ◉  98,412                     │ │
│ └───────────────────────────┘ │   ◉ deduped   ◐ scanned only   ○ never                         │ │
│                               └────────────────────────────────────────────────────────────────┘ │
│                                                                                                  │
│ ┌─ [Q]ueue ────────────────────────────────────────────────────────────────────────────────────┐ │
│ │   idle — no jobs running or queued.                                                          │ │
│ └──────────────────────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ [r] focus Roots   [q] focus Queue (again = maximize)   Ctrl-C quit                               │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 — Work in flight (running job + durable backlog; no box focused)
```
┌─ packrat ───────────────────────────────────────────────────────────────── v0.1.0 · daemon ● up ─┐
│                                                                                                  │
│    ___                                                                                           │
│   (o.o)    p a c k r a t                                                                         │
│   (>♦<)    "hoards everything, keeps a system"                                                   │
│   /   \    · 124,803 assets hoarded ·                                                            │
│                                                                                                  │
│ ┌─ Collection ──────────────┐ ┌─ [R]oots ──────────────────────────────────────────────────────┐ │
│ │ Assets    124,803         │ │   Downloads D:\dump              ◐     241                     │ │
│ │   photos  111,240         │ │   _Trash    D:\Backup\_Trash       (trash)                     │ │
│ │   videos   13,563         │ │   Photos    E:\Photos2           ◐   8,900                     │ │
│ │ Trashed     3,904         │ │   Camera    E:\Photos            ◉  26,150                     │ │
│ │ Last scan now             │ │   iPhone    D:\Backup\iPhone     ◉  98,412                     │ │
│ └───────────────────────────┘ │   ◉ deduped   ◐ scanned only   ○ never                         │ │
│                               └────────────────────────────────────────────────────────────────┘ │
│                                                                                                  │
│ ┌─ [Q]ueue ────────────────────────────────────────────────────────────────────────────────────┐ │
│ │ ▶ scan iPhone     ███████████░░░░  67% 8,912/13,204 ETA 4m                                   │ │
│ │ 2 merge dump → Camera        queued · waiting for worker                                     │ │
│ │ 3 scan Photos                blocked: Photos pending dedup                                   │ │
│ │ 4 dedup Photos (confirm)     blocked: Photos pending dedup                                   │ │
│ │ 5 ‹merge dump → Camera (dry-run)›  queued · waiting                                          │ │
│ [r] focus Roots   [q] focus Queue (again = maximize)   Ctrl-C quit                               │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 1.3 — Roots box focused (one `[r]`): heavy frame, arrow-navigable in place
```
┌─ packrat ───────────────────────────────────────────────────────────────── v0.1.0 · daemon ● up ─┐
│                                                                                                  │
│    ___                                                                                           │
│   (o.o)    p a c k r a t                                                                         │
│   (>♦<)    "hoards everything, keeps a system"                                                   │
│   /   \    · 124,803 assets hoarded ·                                                            │
│                                                                                                  │
│ ┌─ Collection ──────────────┐ ┏━ [R]OOTS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓ │
│ │ Assets    124,803         │ ┃ ▸ Downloads D:\dump              ◐     241                     ┃ │
│ │   photos  111,240         │ ┃   _Trash    D:\Backup\_Trash       (trash)                     ┃ │
│ │   videos   13,563         │ ┃   Photos    E:\Photos2           ◐   8,900                     ┃ │
│ │ Trashed     3,904         │ ┃   Camera    E:\Photos            ◉  26,150                     ┃ │
│ │ Last scan 2h ago          │ ┃   iPhone    D:\Backup\iPhone     ◉  98,412                     ┃ │
│ └───────────────────────────┘ ┃   ◉ deduped   ◐ scanned only   ○ never                         ┃ │
│                               ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛ │
│                                                                                                  │
│ ┌─ [Q]ueue ────────────────────────────────────────────────────────────────────────────────────┐ │
│ │   idle — no jobs running or queued.                                                          │ │
│ └──────────────────────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ ↑/↓ select root   [Enter]/→ open detail   [r] maximize   Esc unfocus                             │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 1.4 — Queue box focused (one `[q]`): heavy frame, arrow-navigable in place
```
┌─ packrat ───────────────────────────────────────────────────────────────── v0.1.0 · daemon ● up ─┐
│                                                                                                  │
│    ___                                                                                           │
│   (o.o)    p a c k r a t                                                                         │
│   (>♦<)    "hoards everything, keeps a system"                                                   │
│   /   \    · 124,803 assets hoarded ·                                                            │
│                                                                                                  │
│ ┌─ Collection ──────────────┐ ┌─ [R]oots ──────────────────────────────────────────────────────┐ │
│ │ Assets    124,803         │ │   Downloads D:\dump              ◐     241                     │ │
│ │   photos  111,240         │ │   _Trash    D:\Backup\_Trash       (trash)                     │ │
│ │   videos   13,563         │ │   Photos    E:\Photos2           ◐   8,900                     │ │
│ │ Trashed     3,904         │ │   Camera    E:\Photos            ◉  26,150                     │ │
│ │ Last scan now             │ │   iPhone    D:\Backup\iPhone     ◉  98,412                     │ │
│ └───────────────────────────┘ │   ◉ deduped   ◐ scanned only   ○ never                         │ │
│                               └────────────────────────────────────────────────────────────────┘ │
│                                                                                                  │
│ ┏━ [Q]UEUE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓ │
│ ┃ ▶ scan iPhone     ███████████░░░░  67% 8,912/13,204 ETA 4m                                   ┃ │
│ ┃ ▸2 merge dump → Camera       queued · waiting for worker                                     ┃ │
│ ┃ 3 scan Photos                blocked: Photos pending dedup                                   ┃ │
│ ┃ 4 dedup Photos (confirm)     blocked: Photos pending dedup                                   ┃ │
│ ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛ │
│ ↑/↓ select  [Enter] detail  [c] cancel  [p] prioritize  [x] all  Esc                             │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 2. Roots interface (maximized — second `[r]`)
```
┌─ Roots ──────────────────────────────────────────────────────────────────────────── daemon ● up ─┐
│ most-recently-registered first                                                                   │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ ▸ Downloads  D:\dump              ◐    241  never deduped                                        │
│   _Trash     D:\Backup\_Trash     (trash)  —                                                     │
│   Photos     E:\Photos2           ◐  8,900  never deduped                                        │
│   Camera     E:\Photos            ◉ 26,150  deduped Jul 12                                       │
│   iPhone     D:\Backup\iPhone     ◉ 98,412  deduped today                                        │
│                                                                                                  │
│ ◉ scanned + deduped   ◐ scanned only   ○ never scanned                                           │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ ↑/↓ select   [Enter]/→ open detail   [a] add root   Esc back                                     │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 3. Root detail interface (`[Enter]`/`→` on a root)

### 3.1 — With a pending dedup/cleanup review (the actionable case)
```
┌─ iPhone ──────────────────────────────────────────────────────────── D:\Backup\iPhone · library ─┐
│ assets  98,412  (photos 92,110 · videos 6,302)     files 98,540                                  │
│ scanned 2h ago    last full scan Jul 10    deduped today 11:31                                   │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ ⚠ dedup — awaiting review (stage 2 of 3)                                                         │
│     240 to delete (exact) · 18 groups / 47 members (default-keep)                                │
│     review: D:\Backup\iPhone\_packrat_review\_suspect_recompression\                             │
│     [o] open in Explorer   [g] confirm stage   [k] cancel run                                    │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ Jobs (newest first):                                                                             │
│  ▸ dedup  ⚠ awaiting review · 240 delete · 18 grp/47 mbr   11:31                                 │
│    scan   done     +412 new · 3 undecodable                09:04                                 │
│    merge  done     240 copied · 1 trashed skipped          Jul 14                                │
│    scan   interrupted — re-run to resume                   Jul 13                                │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ [s] scan  [d] dedup  [m] merge into…  [Enter] result  ↑/↓ jobs  Esc                              │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 — No pending review (clean / normal)
```
┌─ Camera ─────────────────────────────────────────────────────────────────── E:\Photos · library ─┐
│ assets  26,150  (photos 25,900 · videos 250)      files 26,150                                   │
│ scanned 1d ago    last full scan Jul 08    deduped Jul 12                                        │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ No pending review.   (cleaned: never)                                                            │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ Last scan (Jul 15 09:31): +26 new · 0 exact-dup · 0 undecodable                                  │
│ Jobs (newest first):                                                                             │
│  ▸ scan   done     +26 new                                 Jul 15                                │
│    dedup  done     already clean                           Jul 12                                │
│    merge  done     1,204 copied · 12 exact-known           Jul 08                                │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ [s] scan  [d] dedup  [m] merge into…  [Enter] result  ↑/↓ jobs  Esc                              │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 4. Queue interface (maximized — second `[q]`)
```
┌─ Queue ──────────────────────────────────────────────────────────────────────────── daemon ● up ─┐
│ Running + queued (runs top-down):                                                                │
│  ▶ #418 scan iPhone           67% 8,912/13,204 ETA 4m  running                                   │
│    #419 merge dump → Camera        queued · waiting                                              │
│    #420 scan Photos                blocked: Photos pending dedup                                 │
│    #421 dedup Photos (confirm)     blocked: Photos pending dedup                                 │
│                                                                                                  │
│ Recent:                                                                                          │
│    #417 dedup Photos (confirm) done   52 deleted · 9 spared 11:48                                │
│    #416 cleanup iPhone (exact·delete) done  3 deleted      10:20                                 │
│    #415 scan Camera            done   +26 new             09:31                                  │
│    #414 merge dump → iPhone    done   240 copied          Jul 14                                 │
│    #413 ‹scan iPhone (dry-run)› done  2,110 would index    Jul 14                                │
│    #412 dedup iPhone (cancel)  cancelled  —               Jul 13                                 │
│    #411 scan Photos            interrupted  re-run        Jul 13                                 │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ ↑/↓ select  [c] cancel  [p] prioritize  [x] all  [Enter] detail  Esc                             │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 5. Job result / detail card (`[Enter]` on any job row)

### 5.1 — scan (done)
```
┌─ Job #418 · scan iPhone · done ─────────────────────────────────────────────────── Jul 15 09:04 ─┐
│ scan  D:\Backup\iPhone                       (incremental)                                       │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│   412  new assets           0  exact-dup instances                                               │
│     0  filled-in fingerprints  17  identified as trash                                           │
│     3  undecodable           0  read errors                                                      │
│ 8,912  skipped (fast-path)   2  instances gone (1 forgotten)                                     │
│                                                                                                  │
│ Problem files (3):                                                                               │
│   [undecodable] D:\Backup\iPhone\2019\IMG_0032.HEIC                                              │
│        PIL: cannot identify image file                                                           │
│   [undecodable] D:\Backup\iPhone\clips\old.3gp                                                   │
│   [undecodable] D:\Backup\iPhone\2018\IMG_9910.HEIC                                              │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ Esc back                                                                                         │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 — merge (done)
```
┌─ Job #421 · merge dump → Camera · done ─────────────────────────────────────────── Jul 14 22:10 ─┐
│ merge  E:\iphone_dump  →  Camera                                                                 │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│   240  copied (new)                                                                              │
│    18  skipped — exact-known (already in collection)                                             │
│     1  skipped — trashed (matched trash memory)                                                  │
│     6  skipped — dup-in-source (byte-identical siblings)                                         │
│     2  collisions renamed       0  errors                                                        │
│                                                                                                  │
│ Source unchanged.  Next: `scan Camera` then `dedup Camera`.                                      │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ Esc back                                                                                         │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 5.3 — dedup (pending — awaiting review; carries actions)
```
┌─ Job #430 · dedup Photos (analyze) · ⚠ awaiting review ──────────────────────────── today 11:31 ─┐
│ dedup  E:\Photos2       stage 2 of 3 · _suspect_recompression\                                   │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│   Stage 1 exact          ✓ applied  (12 deleted)                                                 │
│ ▶ Stage 2 recompression   staged · 18 groups / 47 members (KEEP)                                 │
│   Stage 3 minor-edits    pending                                                                 │
│   review: E:\Photos2\_packrat_review\_suspect_recompression\                                     │
│                                                                                                  │
│ [o] open review folder   [g] confirm this stage   [k] cancel run                                 │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ Esc back                                                                                         │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 5.4 — dedup (done) & already-clean
```
┌─ Job #430 · dedup Photos (confirm) · done ───────────────────────────────────────── today 11:48 ─┐
│ All stages reviewed.                                                                             │
│ 52 deleted (12 exact · 40 near-dup) · 9 spared.                                                  │
│ Audit: %APPDATA%\packrat\audit\dedup\Photos\430\                                                 │
│        (proposed.json / applied.json)                                                            │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ Esc back                                                                                         │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 5.5 — already-clean · trash-refresh · untrash · error · interrupted
```
┌─ Job #451 · dedup iPhone (analyze) · done ───────────────────────────────────────────── history ─┐
│ Already clean — no exact duplicates or near-dup groups.                                          │
│ (counts as a successful dedup → sets this root's ◉ + deduped time)                               │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ trash refresh #402 · done:                                                                       │
│   9 new trashed · 3 flipped · 1 known · 12 emptied                                               │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ untrash IMG_4471.jpg #500 · done:                                                                │
│   1 reactivated · 0 forgotten · 0 already-active · 0 unknown                                     │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ dedup Photos (confirm) #461 · ERROR:                                                             │
│   ✗ nothing to confirm; run `dedup Photos` first.                                                │
│   (result_json NULL on error → shown from status + jobs.error)                                   │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ scan iPhone #455 · INTERRUPTED — progress safe, re-run to resume.                                │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ Esc back                                                                                         │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Notes per interface

- **§1 dashboard** — Header: `GET /health` + `/daemon`. Collection: `status_snapshot()`.
  Roots box: `roots_snapshot()` (+ `last_dedup_at` per root for the dot; **most-recently-
  registered first**). Queue box: running job's SSE bar (TUI-computed ETA) + `queued_jobs()`
  in dequeue order, each with its `blocked` reason. `(dry-run)` rows dimmed. `[c]`→`cancel_job`,
  `[p]`→`prioritize_job`, `[x]`→`cancel_queued` (only shown when Queue is focused, so they don't
  clash with Roots-focus keys).
- **§2 Roots interface** — `roots_snapshot()` id-desc + `last_dedup_at`; `[a]`→register flow
  (unregister/rename deferred, §14 #9, shown disabled).
- **§3 Root detail** — `root_detail(root)`: counts, `last_scan_at`/`last_full_scan_at`/
  **`last_dedup_at`**/`last_cleanup_at`, `pending_review` (+counts), `last_scan` banner,
  `root_jobs(root_id)` history. `[s]`/`[d]`/`[m]` submit scan/dedup/merge-into; `[o]`/`[g]`/`[k]`
  act on a pending review (open staging / `--confirm` / `--cancel`).
- **§4 Queue interface** — `queued_jobs()` + `recent_jobs(limit)`, one list. Same actions as the
  focused Queue box, full-height with history. Mirrors `packrat jobs list`.
- **§5 job cards** — switch on `result_json.op`; `error`/`interrupted` may have NULL result_json →
  rendered from `status` + `jobs.error` (renderer keys off `status` first, `op` second — the §4
  "every job show-able" contract). No log tail (per-job logs aren't persisted).

## Cross-cutting behavior

- **Live** — Queue/running views subscribe to the running job's SSE stream; stats/roots/history
  poll read-only snapshots on a light timer. A job started elsewhere appears automatically.
- **Keyboard-first** — `↑/↓` selection, `[r]`/`[q]` focus-then-maximize, `Enter`/`→` drills in,
  `Esc`/`←` backs out, `Ctrl-C` quits.
- **Observe-and-control** — review decisions are made in Explorer (`[o]`); the TUI never previews
  or edits media.
- **Every action = a CLI verb (§1.6)** — no TUI-only capability; a deferred verb is shown disabled.

## Open questions for review

1. **`roots_snapshot()` needs `last_dedup_at` + most-recent-first order** for the dot and the Roots
   list. `last_dedup_at` is a per-root `_last_completed_at(...,'dedup')` (already used in
   `root_detail`) — add it to `roots_snapshot`'s SELECT, and sort `id DESC`. Small; but
   `roots_snapshot` also backs `packrat status`/`roots list` (fine either way — confirm we want the
   CLI list re-ordered too, or keep the DESC order TUI-side only).
2. **Where do collection-level ops launch now that the menu is gone?** Per-root `scan`/`dedup`/
   `merge --into <root>` live on §3. But `merge` (arbitrary source), `trash refresh`, `untrash`
   aren't root-scoped. Options: (a) a few global keys on the dashboard; (b) CLI-only for v1;
   (c) a command palette. (Leaning: (a).)
3. **`Ctrl-C` quit** (since `[q]` = focus Queue). OK, or a different quit key?
4. **Count-confirm for one-shot cleanup** (exact/undecodable) — lives wherever cleanup launches
   from (depends on Q2); a blocking typed-count modal in the TUI, or route to CLI?
5. **Window size 100×24 — DECIDED** (PLAN §12 "Fixed layout": one fixed region, never resized between
   interfaces). Long NAS paths/labels are handled at 100×24, not by widening: **middle-elide with `…`
   in compact list rows** (drive + leaf kept visible), **wrap onto multiple lines in detail/card
   views**, and the full path is always shown in root detail (§3). No open sub-question left here.
6. **Logo/mascot** — placeholder; final ASCII rat is cosmetic.
