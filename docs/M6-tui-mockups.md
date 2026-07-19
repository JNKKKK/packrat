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

**Backend readiness:** nothing here needs new backend. The two additions once flagged here —
`roots_snapshot()` gaining `last_dedup_at` (for the status dot) and `photos`/`videos` (for the sort
cycle) — **have landed** (Open Q#1); roots are shown most-recently-registered-first by a **TUI-side**
sort over the snapshot (server order stays `id` ascending, CLI-compatible). ETA is computed TUI-side; a **running** job's card
renders live from the SSE stream and swaps to its `result_json` result view on completion (§5.1),
terminal cards render `result_json` (no log tail); the old "duplicates (est)" stat is dropped.
Every action maps to an existing CLI verb (design tenet §1.6).

## Conventions

- `▸` selection cursor · `▶` running job · `⚠` needs attention.
- **`page i/N` paginator** — every scrollable list draws a centered page indicator directly beneath
  it; `←/→` move between pages (always shown, `1/1` when the list fits one page). Long lists scroll
  by page within the fixed frame, never resizing it (PLAN §12 "Fixed layout").
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
│ └──────────────────────────────────────────────────────────────────────────────────────────────┘ │
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
│ ┌─ Collection ──────────────┐ ┏━ [R]OOTS   page 1/1 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓ │
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
│ ↑/↓ select root   [Enter] open detail   ←/→ page   [r] maximize   Esc unfocus                    │
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
│ ┏━ [Q]UEUE   page 1/1 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓ │
│ ┃ ▶ scan iPhone     ███████████░░░░  67% 8,912/13,204 ETA 4m                                   ┃ │
│ ┃ ▸2 merge dump → Camera       queued · waiting for worker                                     ┃ │
│ ┃ 3 scan Photos                blocked: Photos pending dedup                                   ┃ │
│ ┃ 4 dedup Photos (confirm)     blocked: Photos pending dedup                                   ┃ │
│ ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛ │
│ ↑/↓ select  [Enter] detail  ←/→ page  [c] cancel  [p] prioritize  [x] all  Esc                   │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 2. Roots interface (maximized — second `[r]`)

### 2.1 — Root list
```
┌─ packrat · Roots ────────────────────────────────────────────────────────────────── daemon ● up ─┐
│ [S]ort: most recent registered  (→ most assets → photos → videos)                                │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ ▸ Downloads  D:\dump              ◐    241  never deduped                                        │
│   _Trash     D:\Backup\_Trash     (trash)  —                                                     │
│   Photos     E:\Photos2           ◐  8,900  never deduped                                        │
│   Camera     E:\Photos            ◉ 26,150  deduped Jul 12                                       │
│   iPhone     D:\Backup\iPhone     ◉ 98,412  deduped today                                        │
│                                             page 1/1                                             │
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
│ ↑/↓ select   [Enter] open detail   ←/→ page   [s] sort   [a] add root   Esc back                 │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 — Add a root (`[a]` — the `roots register` flow)
```
┌─ packrat · Roots · add ──────────────────────────────────────────────────────────── daemon ● up ─┐
│ Register a new root (metadata-only; scan it afterward).                                          │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│   Path   ▸ \\tubie_nas\Res-v2\NewPhone____________________________                               │
│            (must exist, be a readable directory, not overlap a root)                             │
│                                                                                                  │
│   Name     [ NewPhone ]   ‹defaults to the folder leaf; must be unique›                          │
│                                                                                                  │
│   Kind     (•) library    ( ) trash                                                              │
│                                                                                                  │
│   [x] scan immediately after registering   ( ) --full   ( ) --embed                              │
│                                                                                                  │
│   ‹trash roots are never scanned; --full/--embed apply only with scan›                           │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ [Tab] next field   type to edit   [Enter] register   Esc cancel                                  │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 3. Root detail interface (`[Enter]` on a root)

### 3.1 — With a pending dedup/cleanup review (the actionable case)
```
┌─ packrat · iPhone ────────────────────────────────────────────────── D:\Backup\iPhone · library ─┐
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
│                                             page 1/1                                             │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ [s] scan  [d] dedup  [m] merge from…  [Enter] result  ↑/↓ jobs  ←/→ page  Esc                    │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 — No pending review (clean / normal)
```
┌─ packrat · Camera ───────────────────────────────────────────────────────── E:\Photos · library ─┐
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
│                                             page 1/1                                             │
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
│ [s] scan  [d] dedup  [m] merge from…  [Enter] result  ↑/↓ jobs  ←/→ page  Esc                    │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.3 — Merge from… (`[m]` — pick the source; this root is the destination)

**Variant A — `(•) Registered root`:** a paginated roots list to pick from.
```
┌─ packrat · Camera · merge from ──────────────────────────────────────────── E:\Photos · library ─┐
│ Copy files new to the whole collection INTO this root, by exact hash.                            │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ Destination   Camera   E:\Photos                                                                 │
│                                                                                                  │
│ Source   (•) Registered root     ( ) External folder                                             │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│  ▸ Downloads   D:\dump                            241 assets                                     │
│    Photos      E:\Photos2                       8,900 assets                                     │
│    iPhone      D:\Backup\iPhone                98,412 assets                                     │
│                                            page 1/1                                              │
│                                                                                                  │
│ [ ] --dry-run   (classify + preview counts; copies nothing — still empties trash)                │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ ↑/↓ pick   ←/→ page   [Tab] switch source   [ ] --dry-run   [Enter] merge   Esc                  │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Variant B — `( ) External folder`:** type an arbitrary path (need not be a root).
```
┌─ packrat · Camera · merge from ──────────────────────────────────────────── E:\Photos · library ─┐
│ Copy files new to the whole collection INTO this root, by exact hash.                            │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ Destination   Camera   E:\Photos                                                                 │
│                                                                                                  │
│ Source   ( ) Registered root     (•) External folder                                             │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│   Path   ▸ E:\iphone_dump________________________________________                                │
│            (any readable folder — a temp export, a card, a share)                                │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ [ ] --dry-run   (classify + preview counts; copies nothing — still empties trash)                │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ type to edit path   [Tab] switch source   [ ] --dry-run   [Enter] merge   Esc                    │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 4. Queue interface (maximized — second `[q]`)
```
┌─ packrat · Queue ────────────────────────────────────────────────────────────────── daemon ● up ─┐
│ Running:                                                                                         │
│  ▶ #418 scan iPhone           67% 8,912/13,204 ETA 4m  running                                   │
│                                                                                                  │
│ Queued (runs top-down):                                                                          │
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
│                                             page 1/1                                             │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ ↑/↓ select  ←/→ page  [c] cancel  [p] prioritize  [x] all  [Enter] detail  Esc                   │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 5. Job result / detail card (`[Enter]` on any job row)

### 5.1 — scan (running — live progress; `[Enter]` on the running job row)
```
┌─ packrat · Job #418 · scan iPhone · running ───────────────────────────────────── started 09:04 ─┐
│ scan  D:\Backup\iPhone                          (incremental)                                    │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ ▶ ████████████████████░░░░░░░░░░  67%   8,912 / 13,204 files   ETA 4m                            │
│                                                                                                  │
│   live so far:                                                                                   │
│     +389  new assets              2  undecodable                                                 │
│   8,510  skipped (fast-path)      0  read errors                                                 │
│                                                                                                  │
│   now scanning: D:\Backup\iPhone\2021\IMG_2231.HEIC                                              │
│                                                                                                  │
│   ‹live — refreshes as the job runs; auto-shows the result card on completion›                   │
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
│ [c] cancel job   Esc back                                                                        │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 — scan (done)
```
┌─ packrat · Job #418 · scan iPhone · done ───────────────────────────────────────── Jul 15 09:04 ─┐
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

### 5.3 — merge (done)
```
┌─ packrat · Job #421 · merge dump → Camera · done ───────────────────────────────── Jul 14 22:10 ─┐
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

### 5.4 — dedup (pending — awaiting review; carries actions)
```
┌─ packrat · Job #430 · dedup Photos (analyze) · ⚠ awaiting review ────────────────── today 11:31 ─┐
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

### 5.5 — dedup (done) & already-clean
```
┌─ packrat · Job #430 · dedup Photos (confirm) · done ─────────────────────────────── today 11:48 ─┐
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

### 5.6 — compact overview (NOT one screen): already-clean · trash-refresh · untrash · error · interrupted
```
┌─ packrat · result-card shapes (reference — not a real screen) ─────────────────────── reference ─┐
│ ‹Compact overview: five short result shapes stacked here for review — in the                     │
│  real TUI each is its OWN single-job card ([Enter] on that job), never one window.›              │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ dedup iPhone (analyze) #451 · done — already clean:                                              │
│   Already clean — no exact duplicates or near-dup groups.                                        │
│   (counts as a successful dedup → sets this root's ◉ + deduped time)                             │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ trash refresh #402 · done:  9 new trashed · 3 flipped · 1 known · 12 emptied                     │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ untrash IMG_4471.jpg #500 · done:  1 reactivated · 0 forgotten · 0 active · 0 unknown            │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ dedup Photos (confirm) #461 · ERROR:  ✗ nothing to confirm; run `dedup Photos` first.            │
│   (result_json NULL on error → shown from status + jobs.error)                                   │
│ ──────────────────────────────────────────────────────────────────────────────────────────────   │
│ scan iPhone #455 · INTERRUPTED — progress safe, re-run to resume.                                │
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

*Data source + interaction, indexed by the mockup interfaces above (§1–§5). The
[Panels](#panels-conceptual-spec) section below re-cuts the same behavior by reusable component;
[Job labels](#job-labels-type--params--display) and [Result cards](#result-cards--result_json-shapes-per-op)
give the label map and card shapes.*

- **§1 dashboard** — Header: `GET /health` + `/daemon`. Collection: `status_snapshot()` —
  **re-fetched whenever any job finishes** (the SSE terminal event triggers an immediate reload, so
  the asset/photo/video/trashed tallies and `Last scan` reflect what the job just changed) as well as
  on the light poll timer. Roots box: `roots_snapshot()` (+ `last_dedup_at` per root for the dot;
  **most-recently-registered first**) — refreshed on the same job-finished trigger, so a root's dot
  and counts update the moment its scan/dedup completes. Queue box: running job's SSE bar (TUI-computed ETA) + `queued_jobs()`
  in dequeue order, each with its `blocked` reason. This box is a **fixed-height preview** — it
  shows the running job plus the first few queued jobs (the full backlog + `‹dry-run›` rows, dimmed,
  live in the maximized §4). `[c]`→`cancel_job`, `[p]`→`prioritize_job`, `[x]`→`cancel_queued`
  (only shown when Queue is focused, so they don't clash with Roots-focus keys).
- **§2 Roots interface** — `roots_snapshot()` + `last_dedup_at`. **`[a]` → the add-root form (2.2)**,
  which submits `register_root(path, name=, kind=, scan=, full=, embed=)` (the `roots register` flow):
  validate inline on `[Enter]` (path exists / readable dir / no overlap / unique name — the same
  `RootError`s the daemon returns, surfaced in the form), then optionally kick off a scan.
  Unregister/rename stay deferred (§14 #9, shown disabled elsewhere). **`[s]` cycles the sort order**, in this
  fixed cycle (wraps back to the first): **most-recently-registered** (default; `id DESC`) →
  **most assets** (`asset_count DESC`) → **most photos** → **most videos** → (back to registered). The
  header line shows the *current* mode (`[S]ort: most recent registered`, `[S]ort: most assets`, …).
  Photo/video sort keys use the per-root `photos`/`videos` counts now returned by `roots_snapshot()`
  (added for M6 — Open Question #1, resolved). All sorting is display-side over the snapshot; no new
  endpoint.
- **§3 Root detail** — `root_detail(root)`: counts, `last_scan_at`/`last_full_scan_at`/
  **`last_dedup_at`**/`last_cleanup_at`, `pending_review` (+counts), `last_scan` banner,
  `root_jobs(root_id)` history. `[s]`/`[d]` submit scan/dedup; `[o]`/`[g]`/`[k]` act on a pending
  review (open staging / `--confirm` / `--cancel`). **`[m]` opens the merge-from picker (§3.3)** —
  this root is the fixed **destination**, and the user picks the **source** via a radio: **`(•)
  Registered root`** shows a paginated roots list (library roots only — a trash inbox is never a
  merge source; the dest root is excluded), or **`( ) External folder`** shows a path field for an
  arbitrary folder that need not be a root (a transient temp export/card/share — read-only in merge,
  §8 C). `[Tab]` toggles the radio. It submits `merge <source> --into <this root>` (the `packrat
  merge` verb), with an optional `--dry-run` toggle. Because merge owns the dest root (§3 per-root
  exclusivity), it's enqueued and shown `blocked` if a review/merge already holds it.
- **§4 Queue interface** — `queued_jobs()` + `recent_jobs(limit)`, split into three labelled
  sections: **Running** (the single active job), **Queued** (the durable backlog, dequeue order,
  each with its `blocked` reason) and **Recent** (`recent_jobs` history). Same actions as the
  focused Queue box, full-height. Mirrors `packrat jobs list`.
- **§5 job cards** — renderer keys off `status` first, `op` second (the §4 "every job show-able"
  contract):
  - **`running` (5.1)** — there is **no `result_json` yet**, so the card renders **live from the
    running job's SSE stream** (progress bar, running counts, ETA, current file) exactly like the
    dashboard/queue bars. Only a running job's card carries **`[c] cancel job`**. **On completion the
    SSE fires a terminal event → the daemon writes `result_json` → the open card refreshes in place**:
    the bar disappears and the final result view (5.2 for a scan) takes over — no manual reopen. A
    dropped stream just reconnects (job state is durable), and if the job finished while detached the
    card opens straight to the result.
  - **terminal (5.2–5.6)** — switch on `result_json.op` for the outcome tallies; `error`/`interrupted`
    may have NULL `result_json` → rendered from `status` + `jobs.error`. No log tail (per-job logs
    aren't persisted). **5.6 is not a screen** — it is a compact overview stacking five short result
    shapes (already-clean, trash-refresh, untrash, error, interrupted) into one frame for review; in
    the TUI each is its own single-job card, never one window.

## Panels (conceptual spec)

The interfaces above are composed from a small set of panels. This is the authoritative
per-panel behavior (data source + interaction); the frames show how they lay out.

- **Logo + tagline** — the packrat mascot (ASCII art of a rat clutching a `♦` — its hoard), the
  tagline "hoards everything, keeps a system", and a **live "· N assets hoarded ·" line** that
  reflects the current total-asset count (updates as scans/merges add assets). Cosmetic + a small
  at-a-glance stat.
- **Collection stats** — read-only DB rollups: total assets (photo/video split), trashed count, and
  last-scan recency. Refreshes live while jobs run (all fields from `status_snapshot`). *(A
  `similarity_edges`-derived "duplicates (est)" stat was considered and **dropped**: `similarity_edges`
  is a per-run cache that only exists after a `dedup` and is never a complete count, so a headline
  "est" number would be misleading — 0 on a fresh collection, stale after scans. Duplicate state is
  surfaced where it's real and actionable instead: the per-root `⚠ awaiting review` count.)*
- **Roots** — each registered root with path, asset count, and the freshness dot (`◉/◐/○`); trash
  roots labelled. `↑/↓` moves the selection cursor `▸`; selecting a root opens its detail. The Roots
  interface adds add-root, the `[s]` sort cycle, and paging.
- **Queue** — the global work pipeline: the one **running** mutating job at the top with its live
  bar/ETA, then the **durable backlog** of `queued` jobs in dequeue order. Each queued row shows *why*
  it waits:
  - **`queued · waiting for worker`** — runnable, just behind the running job (the common case);
  - **`blocked: root R has a pending <run> — confirm/cancel to unblock`** — its owned root is held by
    a pending review / open merge, so the worker **skips it and runs the next runnable job**, retrying
    it on each pump until the holder clears. Because dequeue is **runnable-first, not strict FIFO**, a
    runnable job legitimately passes a blocked one — so running order (and history) is by *start* time,
    not submit time.
  - `[c]` cancels the selected job (a *queued* selection is dropped from the backlog, `cancelled`,
    never ran; the *running* selection gets a cooperative stop at its next checkpoint). `[x]` cancels
    every queued job but leaves the running one. `[p]` prioritizes a queued job. `[Enter]` opens its
    result card.
  - Queued jobs carved out on restart (a destructive `--confirm` is never auto-run) appear as
    `interrupted — re-run to resume`.
- **Per-root jobs** — a root's **current** job (if any) + its **history**, newest-first, from `jobs`
  filtered by `jobs.root_id` (plus the per-root rows a `--all` scan writes to `scan_results`). Each
  row shows type, terminal status, a one-line outcome from `result_json`, and age: a **running** job
  mirrors the Queue bar; a paused **dedup/cleanup** shows `⚠ awaiting review` with the count summary
  (`[o]` opens its `_packrat_review\` folder, `[g]`/`[k]` run `--confirm`/`--cancel`); a **done** job
  shows its one-liner; an **`interrupted`** job shows `interrupted — re-run to resume`. `[Enter]` opens
  the result card.
- **Job detail / result card** — a full-screen card for one job built from `result_json` (the uniform
  outcome summary) plus a link into the richer per-op record (a scan's full banner +
  `scan_problem_files`; a dedup/cleanup run's per-stage plan + audit `proposed.json`/`applied.json`; a
  merge's per-item `merge_plan_items` tally). Terminal jobs are read-only history; a paused review also
  carries the confirm/cancel/open-in-Explorer actions. **No live log tail** — per-job logs aren't
  persisted; a running job's card renders live from the SSE stream (see §5.1), a finished job's from
  `result_json`.
- **Actions launch, nothing is refused at submit.** Submitting while the worker is busy **enqueues**
  the job (it appears in the Queue behind the running one); submitting against a root that's under
  review enqueues it too — it just shows `blocked: … — confirm/cancel to unblock` until the review is
  resolved, then runs automatically. Each action collects its target (a folder picker / path prompt),
  submits, and drops you onto the Queue (or the root's jobs) to watch. Where the now-removed global
  menu's collection-level ops (arbitrary-source `merge`, `trash refresh`, `untrash`) live is Open
  Question #2.

## Job labels (`type` + `params` → display)

Job labels are derived from `type` + `params`, **not** the type alone (pure display — no schema
field). Many operations submit multiple `jobs` rows of the *same* `type` distinguished only by a param
(e.g. `--confirm`/`--cancel`/analyze are all `type='dedup'`; a `--trash-exact` cleanup is a `preview`
then an `apply` job, both `type='cleanup'`). The label is **`<verb> <root-name> (<qualifier>)`**, where
the qualifier comes from `params_json` and the lifecycle **status** (`queued`/`running`/`done`/…) is
shown *separately* — so the qualifier stays a stable *noun* that reads correctly in every state (a
`queued`/`running`/`done` row all read `cleanup iPhone (exact · delete)`). Two display rules: **(a)**
show the root by its **name** (not full path); in the *per-root* jobs panel the root is dropped (the
header already names it), so rows read just `(exact · delete)`. `untrash` targets a raw path (owns no
root), so it shows the path's **leaf**. **(b)** a **`(dry-run)`** job is a non-mutating preview — show
the qualifier but **dim the row**.

| Type · params | Label qualifier |
|---|---|
| **scan** plain / `full` / `embed` / `full`+`embed` / `dry_run` | *(none)* / `(full)` / `(embed)` / `(full · embed)` / `(dry-run)` |
| **scan** `all=True` (owns no root) | `scan all roots` *(± the same flag suffixes)* |
| **dedup** analyze / `dry_run` / `confirm` / `confirm`+`keep_suggested` / `cancel` | `(analyze)` / `(dry-run)` / `(confirm)` / `(confirm · keep-suggested)` / `(cancel)` |
| **cleanup** mode=exact: preview / `apply` / `dry_run` | `(exact · preview)` / `(exact · delete)` / `(exact · dry-run)` |
| **cleanup** mode=undecodable: preview / `apply` / `dry_run` | `(undecodable · preview)` / `(undecodable · delete)` / `(undecodable · dry-run)` |
| **cleanup** mode=perceptual: analyze / `dry_run` / `confirm` / `cancel` | `(perceptual · analyze)` / `(perceptual · dry-run)` / `(perceptual · confirm)` / `(perceptual · cancel)` |
| **trash-refresh** (owns no root) | `trash refresh` *(no qualifier)* |
| **untrash** plain / `dry_run` (shows path leaf) | `untrash <leaf>` / `untrash <leaf> (dry-run)` |
| **merge** plain / `dry_run` (shows `<src-leaf> → <dest-root>`) | `merge <src> → D` / `merge <src> → D (dry-run)` |

(`scan --profile` is a diagnostics flag, not shown in the label — it surfaces in the detail view.)

## Result cards — `result_json` shapes per `op`

Every job writes a `result_json` with an **`op`** discriminator (`scan` / `dedup` / `cleanup` /
`merge` / `trash-refresh` / `untrash`) and a human **`summary`** string, plus op-specific counts. The
card switches on `op`; `summary` is the always-safe one-liner fallback. The renderer treats
`result_json` as *optional* and keys off **`status` first, `op` second** — so `error`/`interrupted`
jobs (which may have NULL `result_json`) still render. Verified shapes:

- **scan** → `{dry_run, full, embed, roots_scanned, roots_skipped, new, exact_dup, backfilled,
  matches_trashed, undecodable, errors, read_errors, skipped_fastpath, deleted_instances,
  forgotten_assets, candidates, summary}`.
- **merge** → `{dry_run, source, dest_root, new, exact_known, trashed, dup_in_source, collisions,
  unindexed, errors, summary}`.
- **dedup** → `{action ∈ analyze|confirm|cancel|dry-run, review_status, stage, to_delete_exact,
  groups, members, summary}` (an "already clean" analyze omits the review fields, carries `summary`).
- **cleanup** → `{mode ∈ exact|perceptual|undecodable, action ∈ preview|delete|analyze|confirm|
  cancel|dry-run, summary}` + mode-specific counts (`would_delete`, or `exact`/`perceptual`, or
  `deleted`/`already_gone`).
- **trash-refresh** → `{roots, new_trashed, flipped, already_trashed, emptied, undeletable, errors,
  summary}`.
- **untrash** → `{dry_run, untrashed, forgotten, already_active, unknown, errors, summary}`.
- **Terminal `error`/`interrupted`** → `result_json` may be **NULL**; the card renders from
  `jobs.status` + `jobs.error` (e.g. `interrupted — re-run to resume`, or the error message). This is
  the "every job is show-able" contract.

## Cross-cutting behavior

The TUI is the **default face** of the tool and, because jobs live in the daemon, a **live window
onto work started from any terminal** — open it anytime to watch progress or stop a running job. It
never *owns* a job; it submits, observes, and cancels, exactly like the CLI.

- **Live** — Queue/running views subscribe to the running job's **SSE stream**; stats/roots/history
  poll read-only snapshots on a light timer **and re-fetch immediately on the SSE job-finished
  event** — so the dashboard's Collection + Roots boxes (and any open snapshot view) update the
  instant a job completes, not just on the next poll tick. A job started elsewhere appears
  automatically; cancelling here stops the job there.
  - **ETA is computed TUI-side, not by the daemon.** Progress events carry `done`/`total` (the
    `ProgressEvent.eta_s` field exists but is left unset). The TUI derives `ETA 4m` from the observed
    rate — `(total − done) / (Δdone/Δt)` over a short trailing window of SSE events — a pure
    presentation estimate that degrades to blank until enough progress has streamed.
- **Every job is show-able** — each job writes a uniform `result_json` at terminal time whatever its
  outcome (`done`/`cancelled`/`interrupted`/`error`), so history and the result card always have
  something to render. The CLI's `status` surfaces the actionable slice of the same rows.
- **Keyboard-first**, mouse optional — `↑/↓` selection, `[r]`/`[q]` focus-then-maximize, `Enter`
  drills in, `Esc` backs out, `Ctrl-C` quits. **`←/→` page** any scrollable list (prev/next page) —
  the roots lists (§1.3 focused box, §2.1 maximized), the queue lists (§1.4, §4), and the root-detail
  Jobs list (§3) — rather than drilling in/out; each draws a **`page i/N` paginator** beneath it.
- **Observe-and-control, not a file manager** — the TUI never previews or edits media (that's
  Explorer's job). For dedup/cleanup review it links out to the staging folder (`[o]`) and waits; the
  keep/delete decisions are made by adding/removing shortcuts there, then confirmed from TUI or CLI.
- **Fixed layout — the window size never changes across interfaces (hard requirement).** Every
  interface renders inside the **same fixed 100×24 region** — one screenful the app owns for its whole
  lifetime. Navigating **swaps content in place; it never grows, shrinks, or reflows the outer frame**.
  On a larger terminal the app still presents that same fixed canvas (extra space is margin — a
  responsive/reflowing layout is explicitly *not* a v1 goal). Long lists scroll **within** their panel,
  not by resizing it. **Long values fit the width, never widen the window:** **middle-elide with `…`**
  in compact list rows (drive + leaf kept visible), **wrap onto multiple lines** in the roomier
  detail/card views; the full untruncated path is always shown in root detail (§3). **These frames are
  generated into an identical 100×24 frame to mechanically enforce this** — if a future interface can't
  fit, that's a signal to trim/elide/wrap it, not to enlarge the window. (Textual: a fixed-size root
  container, not auto-sizing widgets.)
- **Read-safe & CLI-complete (design tenet §1.6)** — everything the TUI does maps to an existing CLI
  verb; it issues no privileged operation of its own, and there is **no TUI-only action**. The TUI is
  the default face, but the CLI is the complete, authoritative surface every capability lands on first
  (so packrat stays scriptable/headless and the TUI can never outrun the CLI). Both are thin clients
  over the same daemon API. A deferred verb is shown disabled.
- **Later milestone (M6)** — the CLI + daemon job runtime are the prerequisite; the TUI is a
  presentation layer that can land once jobs are observable. It depends on two runtime pieces: the
  durable FIFO **queue** and per-job **`root_id`/`result_json`** columns.

## Open questions for review

1. **`roots_snapshot()` additions — DONE.** `roots_snapshot()` now returns (a) `last_dedup_at` per
   root (for the ◉/◐/○ dot — via `_last_completed_at(…, 'dedup')`, the same `completed`-run success
   rule as `root_detail`/§11) and (b) per-root `photos`/`videos` counts (for the `[s]` sort cycle).
   (c) **Ordering stays `id` ascending** — the query also backs `packrat status`/`roots list`, so the
   server order is unchanged and the **TUI sorts client-side** over the snapshot (the `[s]` cycle,
   §2); the CLI keeps its ascending list. No new endpoint; the sort cycle's default
   "most-recently-registered" is a TUI-side `id DESC` reordering of the same rows.
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
