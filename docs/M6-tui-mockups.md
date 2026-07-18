# M6 — TUI mockups (review before implementing)

ASCII mockups of every packrat TUI interface, for review **before** M6 is built.
Grounded in PLAN §12 (layout + panels) and the **actual** data surfaces already
shipped (M0–M5): the read-only queries (`status_snapshot` / `root_detail` /
`recent_jobs` / `root_jobs` / `queued_jobs`), the SSE progress stream, and the
per-op `jobs.result_json` shapes verified in the pre-M6 readiness pass.

**Nothing here needs new backend** (per the readiness audit) except one tiny sort
tweak (roots shown most-recently-registered first — see 1.3 notes): ETA is computed
TUI-side from the streamed `done`/`total` rate; the result card renders `result_json`
(no log tail); the "duplicates (est)" stat was dropped. Every action maps to an
existing CLI verb / endpoint (design tenet §1.6).

Conventions used in these mockups:
- `▸` selection cursor · `▶` running job · `⚠` needs attention · `●/○` recent/stale scan dot
- **Focus:** an unfocused box has a **light** frame `┌─ [R]oots ─┐`; the **focused** box has a
  **heavy** frame + emphasized title `┏━ [R]OOTS ━┓` and shows a `▸` selection cursor.
- `[x]` = a single-key binding · dimmed text shown as `‹dim›…` (dry-run rows, hints)
- Data source for each interface is called out in **‹notes›** under it.

**Navigation model (new):** the dashboard's **Roots** and **Queue** boxes are focus targets.
- Press `[r]` / `[q]` once → **focus** that box (heavy frame); `↑/↓` now navigate its rows in place.
- Press `[r]` / `[q]` **again** (while focused) → **maximize** it into its full-screen interface
  (Roots interface §2 / Queue interface §4).
- `Esc` un-focuses (back to no-focus dashboard) or, from a maximized interface, returns to the dashboard.
- Because `[q]` now focuses the Queue, **quit is `Ctrl-C`** (shown in the footer).

---

## 1. Main dashboard (`packrat`, no args)

Logo + **Collection** (stats, read-only) + focusable **[R]oots** and **[Q]ueue** boxes. No action
menu — the dashboard is the observe-and-steer surface; per-root operations launch from a root's
detail interface (§3), and where collection-level ops (merge / trash / untrash) launch is an open
question (below).

### 1.1 — Idle (nothing running, no box focused)

```
┌─ packrat ──────────────────────────────────────────────── v0.1.0 · daemon ● up ─┐
│                                                                                  │
│       ___                                                                        │
│      (o.o)      p a c k r a t                                                    │
│      (>♦<)      "hoards everything, keeps a system"                              │
│      /   \      · 124,803 assets hoarded ·                                       │
│                                                                                  │
│  ┌─ Collection ─────────────────┐  ┌─ [R]oots ─────────────────────────────┐    │
│  │ Assets      124,803          │  │  iPhone    D:\Backup\iPhone   ● 98,412 │    │
│  │  photos     111,240          │  │  Camera    E:\Photos          ● 26,150 │    │
│  │  videos      13,563          │  │  Downloads D:\dump            ○    241  │    │
│  │ Trashed       3,904          │  │  _Trash    D:\Backup\_Trash    (trash) │    │
│  │ Last scan   2h ago           │  │                                        │    │
│  │                              │  │  ● recent  ○ stale                     │    │
│  └──────────────────────────────┘  └────────────────────────────────────────┘   │
│                                                                                  │
│  ┌─ [Q]ueue ───────────────────────────────────────────────────────────────┐    │
│  │  idle — no jobs running or queued.                                        │    │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
│  [r] focus Roots   [q] focus Queue   (press again to maximize)   Ctrl-C quit      │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 — Work in flight (running job + durable backlog; still no box focused)

```
┌─ packrat ──────────────────────────────────────────────── v0.1.0 · daemon ● up ─┐
│      (o.o)  p a c k r a t     · 124,803 assets hoarded ·                         │
│                                                                                  │
│  ┌─ Collection ─────────────────┐  ┌─ [R]oots ─────────────────────────────┐    │
│  │ Assets      124,803          │  │  iPhone    D:\Backup\iPhone   ● 98,412 │    │
│  │  photos     111,240          │  │  Camera    E:\Photos          ● 26,150 │    │
│  │  videos      13,563          │  │  Photos    E:\Photos2         ○  8,900 │    │
│  │ Trashed       3,904          │  │  _Trash    D:\Backup\_Trash    (trash) │    │
│  │ Last scan   now              │  │  ● recent  ○ stale                     │    │
│  └──────────────────────────────┘  └────────────────────────────────────────┘   │
│                                                                                  │
│  ┌─ [Q]ueue ───────────────────────────────────────────────────────────────┐    │
│  │ ▶ scan iPhone        ███████████████░░░░░  67%  8,912/13,204  ETA 4m      │    │
│  │ 2 merge dump → Camera         queued · waiting for worker                 │    │
│  │ 3 scan Photos                 blocked: Photos has a pending dedup         │    │
│  │ 4 dedup Photos (confirm)      blocked: Photos has a pending dedup         │    │
│  │ 5 ‹merge dump → Camera (dry-run)›  queued · waiting for worker            │    │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
│  [r] focus Roots   [q] focus Queue   (press again to maximize)   Ctrl-C quit      │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### 1.3 — Roots box focused (one `[r]`): heavy frame, arrow-navigable in place

```
┌─ packrat ──────────────────────────────────────────────── v0.1.0 · daemon ● up ─┐
│      (o.o)  p a c k r a t     · 124,803 assets hoarded ·                         │
│                                                                                  │
│  ┌─ Collection ─────────────────┐  ┏━ [R]OOTS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓    │
│  │ Assets      124,803          │  ┃ ▸Downloads D:\dump            ○    241  ┃    │
│  │  photos     111,240          │  ┃  _Trash    D:\Backup\_Trash    (trash) ┃    │
│  │  videos      13,563          │  ┃  Photos    E:\Photos2         ○  8,900 ┃    │
│  │ Trashed       3,904          │  ┃  Camera    E:\Photos          ● 26,150 ┃    │
│  │ Last scan   2h ago           │  ┃  iPhone    D:\Backup\iPhone   ● 98,412 ┃    │
│  │                              │  ┃  ● recent  ○ stale                     ┃    │
│  └──────────────────────────────┘  ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛    │
│                                                                                  │
│  ┌─ [Q]ueue ───────────────────────────────────────────────────────────────┐    │
│  │  idle — no jobs running or queued.                                        │    │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
│  ↑/↓ select root   [Enter]/→ open detail   [r] maximize Roots   Esc unfocus        │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**‹notes›** Focusing highlights the frame + title and shows the `▸` cursor. **Roots are listed
most-recently-registered first** (here `Downloads` newest → `iPhone` oldest) — a change from
`roots_snapshot()`'s current `ORDER BY r.id` (oldest-first); the TUI sorts desc (or we add an
`order` option to the query — tiny, flagged in open questions). `↑/↓` moves the cursor; `[Enter]`
or `→` opens that root's **detail interface** (§3); a second `[r]` maximizes to the **Roots
interface** (§2). `Esc` un-focuses.

### 1.4 — Queue box focused (one `[q]`): heavy frame, arrow-navigable in place

```
┌─ packrat ──────────────────────────────────────────────── v0.1.0 · daemon ● up ─┐
│      (o.o)  p a c k r a t     · 124,803 assets hoarded ·                         │
│                                                                                  │
│  ┌─ Collection ─────────────────┐  ┌─ [R]oots ─────────────────────────────┐    │
│  │ Assets      124,803          │  │  iPhone    D:\Backup\iPhone   ● 98,412 │    │
│  │ Trashed       3,904          │  │  Camera    E:\Photos          ● 26,150 │    │
│  │ Last scan   now              │  │  Photos    E:\Photos2         ○  8,900 │    │
│  └──────────────────────────────┘  └────────────────────────────────────────┘   │
│                                                                                  │
│  ┏━ [Q]UEUE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓    │
│  ┃ ▶ scan iPhone        ███████████████░░░░░  67%  8,912/13,204  ETA 4m      ┃    │
│  ┃ ▸2 merge dump → Camera         queued · waiting for worker                ┃    │
│  ┃ 3 scan Photos                  blocked: Photos has a pending dedup        ┃    │
│  ┃ 4 dedup Photos (confirm)       blocked: Photos has a pending dedup        ┃    │
│  ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛    │
│                                                                                  │
│  ↑/↓ select   [Enter] detail   [c] cancel   [p] prioritize   [x] cancel-all        │
│  [q] maximize Queue   Esc unfocus                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**‹notes›** Same focus model for the Queue. `↑/↓` selects a job; `[c]`→`cancel_job`,
`[p]`→`prioritize_job` (queued only), `[x]`→`cancel_queued`, `[Enter]`→job card (§5). A second
`[q]` maximizes to the **Queue interface** (§4). Data: `queued_jobs()` (dequeue order) + the running
job's SSE bar. (`[c]`/`[p]` only appear when the Queue is focused, so they don't collide with the
Roots-focus keys.)

---

## 2. Roots interface (maximized — second `[r]`)

Full-screen root list — the same rows as the focused dashboard box, given the whole screen. Most
recently registered first. `[Enter]`/`→` opens root detail (§3); `Esc` returns to the dashboard.

```
┌─ Roots ──────────────────────────────────────────────────────── daemon ● up ─┐
│  most-recently-registered first                                                │
│  ─────────────────────────────────────────────────────────────────────────    │
│  ▸ Downloads  D:\dump                    library   ○     241  never deduped    │
│    _Trash     D:\Backup\_Trash           trash     —        —                  │
│    Photos     E:\Photos2                 library   ○   8,900  never deduped    │
│    Camera     E:\Photos                  library   ● 26,150   deduped Jul 12   │
│    iPhone     D:\Backup\iPhone           library   ● 98,412   deduped today    │
│                                                                                │
│  ● scanned recently   ○ stale / never                                          │
│  ↑/↓ select   [Enter]/→ open detail   [a] add root   Esc back                  │
└────────────────────────────────────────────────────────────────────────────────┘
```

**‹notes›** `roots_snapshot()` (id-desc for most-recent-first) + `last_dedup_at` per root for the
"deduped <age> / never deduped" column. `[a]` add root → the register flow (§3 notes / deferred
unregister+rename per §14 #9). A `trash` root shows no dedup/scan-dot (scan never touches it).

---

## 3. Root detail interface (`[Enter]`/`→` on a root)

Everything about one root: identity, counts, recency (incl. the new **last successful dedup**), any
pending review, its job history, and the per-root action keys. `Esc`/`←` returns to where you came
from (dashboard or Roots interface).

### 3.1 — With a pending dedup/cleanup review (the actionable case)

```
┌─ iPhone ─────────────────────────────────────── D:\Backup\iPhone · library ──┐
│  assets   98,412  (photos 92,110 · videos 6,302)     files 98,540             │
│  scanned  2h ago      last full scan  Jul 10      deduped  today 11:31        │
│  ───────────────────────────────────────────────────────────────────────────  │
│  ⚠ dedup — awaiting review (stage 2 of 3)                                      │
│      240 to delete (exact) · 18 groups / 47 members (near-dup, default-keep)   │
│      review: D:\Backup\iPhone\_packrat_review\_suspect_recompression\          │
│      [o] open in Explorer   [g] confirm stage   [k] cancel run                 │
│  ───────────────────────────────────────────────────────────────────────────  │
│  Jobs (newest first):                                                          │
│   ▸ dedup   ⚠ awaiting review · 240 delete · 18 grp/47 mbr        11:31         │
│     scan    done      +412 new · 3 undecodable                    09:04         │
│     merge   done      240 copied · 1 trashed skipped              Jul 14        │
│     scan    interrupted — re-run to resume                        Jul 13        │
│                                                                                │
│  [s] scan   [d] dedup   [m] merge into…   [Enter] job result   ↑/↓ jobs   Esc back│
└────────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 — No pending review (clean / normal)

```
┌─ Camera ──────────────────────────────────────────── E:\Photos · library ───┐
│  assets   26,150  (photos 25,900 · videos 250)       files 26,150            │
│  scanned  1d ago      last full scan  Jul 08      deduped  Jul 12            │
│  ───────────────────────────────────────────────────────────────────────────  │
│  No pending review.  (cleaned: never)                                          │
│  ───────────────────────────────────────────────────────────────────────────  │
│  Last scan (Jul 15 09:31): +26 new · 0 exact-dup · 0 undecodable · 0 errors    │
│  Jobs (newest first):                                                          │
│   ▸ scan    done      +26 new                                     Jul 15        │
│     dedup   done      already clean                               Jul 12        │
│     merge   done      1,204 copied · 12 exact-known                Jul 08       │
│                                                                                │
│  [s] scan   [d] dedup   [m] merge into…   [Enter] job result   ↑/↓ jobs   Esc back│
└────────────────────────────────────────────────────────────────────────────────┘
```

**‹notes›** All from `root_detail(root)`: counts, `last_scan_at`, `last_full_scan_at`,
**`last_dedup_at`** ("deduped today/Jul 12/never"), `last_cleanup_at` ("cleaned …"),
`pending_review` (+ counts), `last_scan` banner, and `root_jobs(root_id)` for the history list.
Per-root actions: `[s]`→`submit_scan(root)`, `[d]`→`submit_dedup(root)`, `[m]`→merge with this root
as `--into`, `[o]`/`[g]`/`[k]` on a pending review (open staging / `--confirm` / `--cancel`).
`[Enter]` on a job row → its result card (§5).

---

## 4. Queue interface (maximized — second `[q]`)

Full-screen work queue + recent history — the "all jobs, all roots, one place" view (mirrors
`packrat jobs list`). `Esc` returns to the dashboard.

```
┌─ Queue ──────────────────────────────────────────────────────── daemon ● up ─┐
│  Running + queued (runs top-down):                                             │
│   ▶ #418 scan iPhone            67%  8,912/13,204  ETA 4m   running            │
│     #419 merge dump → Camera         queued · waiting for worker               │
│     #420 scan Photos                 blocked: Photos has a pending dedup       │
│     #421 dedup Photos (confirm)      blocked: Photos has a pending dedup       │
│                                                                                │
│  Recent:                                                                       │
│     #417 dedup Photos (confirm)  done         52 deleted · 9 spared    11:48   │
│     #416 cleanup iPhone (exact · delete)  done   3 deleted             10:20   │
│     #415 scan Camera             done         +26 new                  09:31   │
│     #414 merge dump → iPhone     done         240 copied · 1 trashed    Jul 14 │
│     #413 ‹scan iPhone (dry-run)› done         2,110 would index         Jul 14 │
│     #412 dedup iPhone (cancel)   cancelled    —                         Jul 13 │
│     #411 scan Photos             interrupted  re-run to resume          Jul 13 │
│  ↑/↓ select   [c] cancel   [p] prioritize   [x] cancel-all   [Enter] detail  Esc │
└────────────────────────────────────────────────────────────────────────────────┘
```

**‹notes›** `queued_jobs()` (top, dequeue order) + `recent_jobs(limit)` (below), one selection list.
`[c]`/`[p]`/`[x]` act on the selection; `[Enter]` → job card (§5). Same data as the dashboard Queue
box, just full-height with history.

---

## 5. Job result / detail card (`[Enter]` on any job row)

Switches on `result_json.op`. `Esc` returns. **No log tail** (per-job logs aren't persisted) — a
terminal job renders `result_json`; a running one mirrors the live bar. `error`/`interrupted` may
have NULL `result_json` → rendered from `jobs.status` + `jobs.error` (renderer keys off `status`
first, `op` second — the §4 "every job show-able" contract).

### 5.1 — scan (done)
```
┌─ Job #418 · scan iPhone · done ──────────────────────────────── Jul 15 09:04 ─┐
│  scan  D:\Backup\iPhone                                    (incremental)       │
│  ───────────────────────────────────────────────────────────────────────────  │
│    412  new assets            0  exact-dup instances                           │
│      0  filled-in fingerprints   17  identified as trash                       │
│      3  undecodable            0  read errors                                  │
│  8,912  skipped (fast-path)    2  instances gone (1 asset forgotten)           │
│                                                                                │
│  Problem files (3):                                                            │
│    [undecodable] D:\Backup\iPhone\2019\IMG_0032.HEIC                           │
│         PIL: cannot identify image file                                        │
│    [undecodable] D:\Backup\iPhone\clips\old.3gp                                │
│    [undecodable] D:\Backup\iPhone\2018\IMG_9910.HEIC                           │
│  Esc back                                                                      │
└────────────────────────────────────────────────────────────────────────────────┘
```
**‹notes›** Counts from `result_json` (scan shape); problem files from `root_detail().problem_files`
(live-derived undecodables + this-pass read-errors). `(incremental)`/`(full)` from `full`.

### 5.2 — merge (done)
```
┌─ Job #421 · merge dump → Camera · done ──────────────────────── Jul 14 22:10 ─┐
│  merge  E:\iphone_dump  →  Camera                                              │
│  ───────────────────────────────────────────────────────────────────────────  │
│    240  copied (new)                                                           │
│     18  skipped — exact-known (already in collection)                          │
│      1  skipped — trashed (matched trash memory)                               │
│      6  skipped — dup-in-source (byte-identical siblings)                      │
│      2  collisions renamed        0  errors                                    │
│  Source unchanged.  Next: `scan Camera` then `dedup Camera`.                   │
│  Esc back                                                                      │
└────────────────────────────────────────────────────────────────────────────────┘
```
**‹notes›** From `result_json` (merge shape). If `unindexed > 0`, add a ⚠ "N copied to an ignored
dest path — NOT catalogued" line.

### 5.3 — dedup (pending — awaiting review; carries actions)
```
┌─ Job #430 · dedup Photos (analyze) · ⚠ awaiting review ──────── today 11:31 ─┐
│  dedup  E:\Photos2         stage 2 of 3 · _suspect_recompression\             │
│  ───────────────────────────────────────────────────────────────────────────  │
│    Stage 1 exact          ✓ applied  (12 deleted)                              │
│  ▶ Stage 2 recompression   staged · 18 groups / 47 members  (default KEEP)     │
│    Stage 3 minor-edits    pending                                              │
│    review: E:\Photos2\_packrat_review\_suspect_recompression\                  │
│  [o] open review folder   [g] confirm this stage   [k] cancel whole run   Esc  │
└────────────────────────────────────────────────────────────────────────────────┘
```
**‹notes›** The one interactive card. Stage cursor + counts from `root_detail().pending_review`.
`[o]` open staging, `[g]` `dedup --confirm`, `[k]` `dedup --cancel`. Cleanup `--trash-perceptual`
pending looks the same with the `_perceptually_identified_trash\` folder + `X exact / P perceptual`.

### 5.4 — dedup (done) & already-clean
```
┌─ Job #430 · dedup Photos (confirm) · done ───────────────────── today 11:48 ─┐
│    All stages reviewed.  52 deleted (12 exact · 40 near-dup) · 9 spared.       │
│    Audit: %APPDATA%\packrat\audit\dedup\Photos\430\  (proposed/applied.json)   │
│    Esc back                                                                    │
└────────────────────────────────────────────────────────────────────────────────┘

┌─ Job #451 · dedup iPhone (analyze) · done ───────────────────── Jul 12 08:00 ─┐
│    Already clean — no exact duplicates or near-dup groups to review.           │
│    (counts as a successful dedup → set this root's "deduped" time)             │
│    Esc back                                                                    │
└────────────────────────────────────────────────────────────────────────────────┘
```

### 5.5 — trash-refresh · untrash · error · interrupted
```
┌─ Job #402 · trash refresh · done ────────────────────────────── Jul 14 20:01 ─┐
│    9 new trashed · 3 flipped active→trashed · 1 already known · 12 emptied     │
└────────────────────────────────────────────────────────────────────────────────┘

┌─ Job #500 · untrash IMG_4471.jpg · done ─────────────────────── today 14:22 ─┐
│    1 reactivated in place · 0 forgotten · 0 already active · 0 unknown         │
└────────────────────────────────────────────────────────────────────────────────┘

┌─ Job #461 · dedup Photos (confirm) · error ──────────────────── Jul 13 10:15 ─┐
│    ✗ nothing to confirm for 'Photos'; run `dedup Photos` first.                │
│    (result_json NULL on error — rendered from status + jobs.error)             │
└────────────────────────────────────────────────────────────────────────────────┘

┌─ Job #455 · scan iPhone · interrupted ───────────────────────── Jul 13 09:40 ─┐
│    ⚠ interrupted — the daemon stopped; your progress is safe.                  │
│    Re-run to resume:  packrat scan iPhone                                      │
└────────────────────────────────────────────────────────────────────────────────┘
```

---

## Cross-cutting behavior (all interfaces)

- **Live.** Queue/running views subscribe to the running job's **SSE stream** (moving bar, TUI-side
  ETA); stats/roots/history poll read-only snapshots on a light timer. A job started in another
  terminal appears here automatically.
- **Keyboard-first, mouse optional.** `↑/↓` moves selection in the focused/maximized list;
  `[r]`/`[q]` focus-then-maximize the Roots/Queue boxes; `Enter`/`→` drills in, `Esc`/`←` backs out;
  `Ctrl-C` quits. Bracketed keys are contextual actions.
- **Observe-and-control, not a file manager.** Review decisions are made in Explorer (`[o]` opens
  the staging folder); the TUI only stages/confirms/cancels. It never previews or edits media.
- **Every action = a CLI verb (tenet §1.6).** No TUI-only capability; a not-yet-built verb
  (unregister/rename) is shown disabled, never as a live-but-fake button.

---

## Open questions for review

1. **Where do collection-level ops launch now that the menu is gone?** Per-root ops (`scan`/`dedup`/
   `merge --into <this root>`) live on the root detail interface (§3). But `merge` (arbitrary
   source), `trash refresh`, and `untrash` aren't root-scoped. Options: (a) a small "actions" key on
   the dashboard that isn't a full menu; (b) launch them only from the CLI for v1 and keep the TUI
   observe+per-root-only; (c) a lightweight command palette. (Leaning: (a) a couple of global keys,
   e.g. `[t]` trash, `[u]` untrash, `[m]` merge — but that edges back toward a menu, so flagging.)
2. **Roots most-recent-first ordering** needs `roots_snapshot()` to sort `id DESC` (today it's
   `id ASC`). `roots_snapshot` also backs `packrat status`/`roots list` (which read fine either way).
   Prefer: add an `order` param, or just sort TUI-side? (Leaning: sort TUI-side — no query change,
   `roots list` keeps its stable ascending order.)
3. **`Ctrl-C` as quit** (since `[q]` = focus Queue). OK, or bind quit to something else (e.g. a
   `[Q]` capital, or an explicit `[x]` from the un-focused dashboard)?
4. **Focus affordance** — heavy frame + emphasized title shown here. Enough, or also add a status-bar
   "focus: Roots" hint?
5. **Count-confirm for one-shot cleanup** (exact/undecodable delete) — where it now lives depends on
   Q1 (how trash/cleanup launches from the TUI). Deferred until Q1 is decided.
6. **Logo/mascot** — placeholder art; final ASCII rat is cosmetic.
```
