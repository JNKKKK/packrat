# M6 — TUI mockups (review before implementing)

ASCII mockups of every packrat TUI screen/state, for review **before** M6 is built.
Grounded in PLAN §12 (layout + panels) and the **actual** data surfaces already
shipped (M0–M5): the read-only queries (`status_snapshot` / `root_detail` /
`recent_jobs` / `root_jobs` / `queued_jobs`), the SSE progress stream, and the
per-op `jobs.result_json` shapes verified in the pre-M6 readiness pass.

**Nothing here needs new backend** (per the readiness audit): ETA is computed
TUI-side from the streamed `done`/`total` rate; the result card renders `result_json`
(no log tail); the "duplicates (est)" stat was dropped. Every action maps to an
existing CLI verb / endpoint (design tenet §1.6).

Conventions used in these mockups:
- `▸` selection cursor · `▶` running job · `⚠` needs attention · `●/○` recent/stale scan dot
- `[x]` = a single-key binding · dimmed text shown as `‹dim›…` (dry-run rows, hints)
- Data source for each panel is called out in **‹notes›** under the screen.

---

## Screen 1 — Dashboard, idle (the default `packrat` view, nothing running)

```
┌─ packrat ──────────────────────────────────────────────── v0.1.0 · daemon ● up ─┐
│                                                                                  │
│       ___                                                                        │
│      (o.o)      p a c k r a t                                                    │
│      (>♦<)      "hoards everything, keeps a system"                              │
│      /   \      · 124,803 assets hoarded ·                                       │
│                                                                                  │
│  ┌─ Collection ─────────────────┐  ┌─ Roots ───────────────────────────────┐    │
│  │ Assets      124,803          │  │ ▸iPhone    D:\Backup\iPhone   ● 98,412 │    │
│  │  photos     111,240          │  │  Camera    E:\Photos          ● 26,150 │    │
│  │  videos      13,563          │  │  Downloads D:\dump            ○    241  │    │
│  │ Trashed       3,904          │  │  _Trash    D:\Backup\_Trash    (trash) │    │
│  │ Last scan   2h ago           │  │                                        │    │
│  │                              │  │  ● recent  ○ stale  ▸ select → jobs    │    │
│  └──────────────────────────────┘  └────────────────────────────────────────┘   │
│                                                                                  │
│  ┌─ Queue ─────────────────────────────────────────────────────────────────┐    │
│  │  idle — no jobs running or queued.                                        │    │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
│  What do you want to do?                                                         │
│   [s] Scan a folder     [d] Dedup a folder     [m] Merge into a folder           │
│   [t] Trash (refresh / cleanup)   [r] Manage roots   [j] Jobs   [q] Quit          │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**‹notes›** Header: `GET /health` + `GET /daemon` (version, `● up`). Logo line + Collection
panel: `status_snapshot()` (`assets`/`photos`/`videos`/`trashed`; "last scan" = max root
`last_scan_at`). Roots panel: `roots_snapshot()` (`name`/`path`/`asset_count`/`kind`; the
`●/○` dot from `last_scan_at` recency). Menu = single-key launchers (Screen 6). No root
selected yet → no per-root jobs panel (it appears in Screen 3).

---

## Screen 2 — Dashboard, work in flight (running job + durable backlog)

```
┌─ packrat ──────────────────────────────────────────────── v0.1.0 · daemon ● up ─┐
│       ___                                                                        │
│      (o.o)      p a c k r a t         · 124,803 assets hoarded ·                 │
│      (>♦<)      "hoards everything, keeps a system"                              │
│                                                                                  │
│  ┌─ Collection ─────────────────┐  ┌─ Roots ───────────────────────────────┐    │
│  │ Assets      124,803          │  │ ▸iPhone    D:\Backup\iPhone   ● 98,412 │    │
│  │  photos     111,240          │  │  Camera    E:\Photos          ● 26,150 │    │
│  │  videos      13,563          │  │  Photos    E:\Photos2         ○  8,900 │    │
│  │ Trashed       3,904          │  │  _Trash    D:\Backup\_Trash    (trash) │    │
│  │ Last scan   now              │  │  ● recent  ○ stale  ▸ select → jobs    │    │
│  └──────────────────────────────┘  └────────────────────────────────────────┘   │
│                                                                                  │
│  ┌─ Queue ─────────────────────────────────────────────────────────────────┐    │
│  │ ▶ scan iPhone        ███████████████░░░░░  67%  8,912/13,204  ETA 4m      │    │
│  │ 2 merge dump → Camera         queued · waiting for worker                 │    │
│  │ 3 scan Photos                 blocked: Photos has a pending dedup         │    │
│  │ 4 dedup Photos (confirm)      blocked: Photos has a pending dedup         │    │
│  │ 5 ‹merge dump → Camera (dry-run)›  queued · waiting for worker            │    │
│  │ ↑/↓ select   [c] cancel selected   [p] prioritize   [x] cancel-all queued │    │
│  │ [Enter] detail                                                            │    │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
│  What do you want to do?                                                         │
│   [s] Scan   [d] Dedup   [m] Merge   [t] Trash   [r] Roots   [j] Jobs   [q] Quit  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**‹notes›** Queue: the one `running` job (top, live bar from the **SSE stream**; `ETA` computed
TUI-side from the streamed rate) + the durable backlog (`queued_jobs()` in dequeue order
`priority DESC, enqueued_at, id`). Row labels from `jobs.job_label` (`<verb> <root> (<qual>)`).
`blocked:` reason from each queued row's `blocked` holder (§3). Row 5 is a `(dry-run)` →
**dimmed**. Keys: `[c]`→`cancel_job`, `[p]`→`prioritize_job`, `[x]`→`cancel_queued`,
`[Enter]`→Screen 4.

---

## Screen 3 — Dashboard, a root selected (per-root jobs panel appears)

```
┌─ packrat ──────────────────────────────────────────────── v0.1.0 · daemon ● up ─┐
│      (o.o)  p a c k r a t     · 124,803 assets hoarded ·                         │
│                                                                                  │
│  ┌─ Collection ─────────────────┐  ┌─ Roots ───────────────────────────────┐    │
│  │ Assets      124,803          │  │ ▸iPhone    D:\Backup\iPhone   ● 98,412 │    │
│  │ Trashed       3,904          │  │  Camera    E:\Photos          ● 26,150 │    │
│  │ Last scan   2h ago           │  │  Photos    E:\Photos2         ○  8,900 │    │
│  └──────────────────────────────┘  └────────────────────────────────────────┘   │
│                                                                                  │
│  ┌─ Queue ─────────────────────────────────────────────────────────────────┐    │
│  │  idle — no jobs running or queued.                                        │    │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
│  ┌─ iPhone — jobs ───────────────────────────────────────────────────────────┐  │
│  │   dedup   ⚠ awaiting review · 240 delete · 18 grp/47 mbr        11:31       │  │
│  │   scan    done      +412 new · 3 undecodable                    09:04       │  │
│  │ ▸ merge   done      240 copied · 1 trashed skipped              Jul 14      │  │
│  │   scan    interrupted — re-run to resume                        Jul 13      │  │
│  │   dedup   done      already clean                               Jul 12      │  │
│  │ ↑/↓ select  [Enter] result  [o] open review  [g] confirm  [k] cancel        │  │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  iPhone · 98,412 assets · scanned 2h ago · deduped 11:31 · last full scan Jul 10 │
│                                                                                  │
│   [s] Scan   [d] Dedup   [m] Merge   [t] Trash   [r] Roots   [j] Jobs   [q] Quit  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**‹notes›** Selecting a root in the Roots panel (`↑/↓`) populates the **per-root jobs** panel
below (`root_jobs(root_id)` — newest-first history + any current job). Each row: type · terminal
status · a one-liner from `jobs.result_json.summary` · age. A paused dedup/cleanup shows
`⚠ awaiting review` + the `root_detail().pending_review.counts` summary and enables `[o]` (open
`_packrat_review\` in Explorer — TUI-side `os.startfile`), `[g]`/`[k]` (submit dedup/cleanup
`--confirm`/`--cancel`). The one-line footer draws the recency facts from `root_detail`
(`last_scan_at`, **`last_dedup_at`** → "deduped 11:31", `last_full_scan_at`). An `interrupted`
row reads "re-run to resume" (from `status`, not result_json).

---

## Screen 4 — Job detail / result card (`[Enter]` on any job row)

The card switches on `result_json.op`. `Esc` returns. There is **no log tail** (per-job logs
aren't persisted); a terminal job renders `result_json`, a running one mirrors the live bar.

### 4a · scan (done)
```
┌─ Job #418 · scan iPhone · done ──────────────────────────────── Jul 15 09:04 ─┐
│                                                                                │
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
│                                                                                │
│  Esc back                                                                      │
└────────────────────────────────────────────────────────────────────────────────┘
```
**‹notes›** Counts from `result_json` (scan shape). Problem files: `root_detail().problem_files`
(live-derived undecodables + this-pass read-errors, §11). `(incremental)` / `(full)` from `full`.

### 4b · merge (done)
```
┌─ Job #421 · merge dump → Camera · done ──────────────────────── Jul 14 22:10 ─┐
│                                                                                │
│  merge  E:\iphone_dump  →  Camera                                              │
│  ───────────────────────────────────────────────────────────────────────────  │
│    240  copied (new)                                                           │
│     18  skipped — exact-known (already in collection)                          │
│      1  skipped — trashed (matched trash memory)                               │
│      6  skipped — dup-in-source (byte-identical siblings)                      │
│      2  collisions renamed        0  errors                                    │
│                                                                                │
│  Source unchanged.  Next: `scan Camera` then `dedup Camera` to fingerprint     │
│  the new files and clean recompressed near-dups.                               │
│                                                                                │
│  Esc back                                                                      │
└────────────────────────────────────────────────────────────────────────────────┘
```
**‹notes›** All from `result_json` (merge shape: `new`/`exact_known`/`trashed`/`dup_in_source`/
`collisions`/`errors`). If `unindexed > 0`, add a ⚠ line: "N copied to an ignored dest path — NOT
catalogued".

### 4c · dedup (pending — awaiting review; carries actions)
```
┌─ Job #430 · dedup Photos (analyze) · ⚠ awaiting review ──────── today 11:31 ─┐
│                                                                                │
│  dedup  E:\Photos2         stage 2 of 3 · _suspect_recompression\             │
│  ───────────────────────────────────────────────────────────────────────────  │
│    Stage 1 exact          ✓ applied  (12 deleted)                              │
│  ▶ Stage 2 recompression   staged · 18 groups / 47 members  (default KEEP)     │
│    Stage 3 minor-edits    pending                                              │
│                                                                                │
│    Review in Explorer, then confirm/cancel:                                    │
│      E:\Photos2\_packrat_review\_suspect_recompression\                        │
│                                                                                │
│  [o] open review folder   [g] confirm this stage   [k] cancel whole run        │
│  Esc back                                                                      │
└────────────────────────────────────────────────────────────────────────────────┘
```
**‹notes›** A *pending* review is the one interactive card. Stage cursor + counts from
`root_detail().pending_review` (`stage`, `counts`). `[o]`=open staging folder, `[g]`=submit
`dedup --confirm`, `[k]`=`dedup --cancel`. (Cleanup `--trash-perceptual` pending looks the same
with the `_perceptually_identified_trash\` folder and `X exact / P perceptual` counts.)

### 4d · dedup (done) & already-clean
```
┌─ Job #430 · dedup Photos (confirm) · done ───────────────────── today 11:48 ─┐
│    All stages reviewed.  52 deleted (12 exact · 40 near-dup) · 9 spared.       │
│    Audit: %APPDATA%\packrat\audit\dedup\Photos\430\  (proposed/applied.json)   │
│    Esc back                                                                    │
└────────────────────────────────────────────────────────────────────────────────┘

┌─ Job #451 · dedup iPhone (analyze) · done ───────────────────── Jul 12 08:00 ─┐
│    Already clean — no exact duplicates or near-dup groups to review.           │
│    Esc back                                                                    │
└────────────────────────────────────────────────────────────────────────────────┘
```
**‹notes›** `result_json.summary` covers both ("run completed" / "already clean"). Deep forensics
(per-stage disposition) come from the §8.1 audit files, linked by path (not inlined).

### 4e · trash-refresh · untrash · error / interrupted
```
┌─ Job #402 · trash refresh · done ────────────────────────────── Jul 14 20:01 ─┐
│    9 new trashed · 3 flipped active→trashed · 1 already known · 12 emptied     │
│    (0 could not delete, 0 unreadable)                                          │
└────────────────────────────────────────────────────────────────────────────────┘

┌─ Job #500 · untrash IMG_4471.jpg · done ─────────────────────── today 14:22 ─┐
│    1 reactivated in place · 0 forgotten · 0 already active · 0 unknown         │
└────────────────────────────────────────────────────────────────────────────────┘

┌─ Job #461 · dedup Photos (confirm) · error ──────────────────── Jul 13 10:15 ─┐
│    ✗ nothing to confirm for 'Photos'; run `dedup Photos` first.                │
│    (result_json is NULL for an error — rendered from status + jobs.error)      │
└────────────────────────────────────────────────────────────────────────────────┘

┌─ Job #455 · scan iPhone · interrupted ───────────────────────── Jul 13 09:40 ─┐
│    ⚠ interrupted — the daemon stopped; your progress is safe.                  │
│    Re-run to resume:  packrat scan iPhone                                      │
└────────────────────────────────────────────────────────────────────────────────┘
```
**‹notes›** `done` cards read `result_json.summary` + fields. **`error`/`interrupted`** may have
NULL `result_json` → the card renders from `jobs.status` + `jobs.error` (the §4 "every job
show-able" contract; renderer keys off `status` first, `op` second).

---

## Screen 5 — Jobs view (`[j]` — the full queue + recent history, one screen)

```
┌─ Jobs ───────────────────────────────────────────────────────── daemon ● up ─┐
│  Queue (runs top-down):                                                        │
│   ▶ #418 scan iPhone            67%  8,912/13,204  ETA 4m   running            │
│     #419 merge dump → Camera         queued · waiting for worker               │
│     #420 scan Photos                 blocked: Photos has a pending dedup       │
│                                                                                │
│  Recent:                                                                       │
│     #417 dedup Photos (confirm)  done         52 deleted · 9 spared   11:48    │
│     #416 cleanup iPhone (exact · delete)  done   3 deleted            10:20    │
│     #415 scan Camera             done         +26 new                 09:31    │
│     #414 merge dump → iPhone     done         240 copied · 1 trashed   Jul 14  │
│     #413 ‹scan iPhone (dry-run)› done         2,110 would index        Jul 14  │
│     #412 dedup iPhone (cancel)   cancelled    —                        Jul 13  │
│     #411 scan Photos             interrupted  re-run to resume         Jul 13  │
│  ↑/↓ select   [c] cancel   [p] prioritize   [Enter] detail   [Esc] back        │
└────────────────────────────────────────────────────────────────────────────────┘
```

**‹notes›** A dedicated full-height view of the same data as the dashboard Queue panel, but with
recent **history** too — `queued_jobs()` (top) + `recent_jobs(limit)` (below), one selection list.
Mirrors the `packrat jobs list` CLI. `[c]`/`[p]` act on the selected row (cancel/prioritize);
`[Enter]` → Screen 4. This is optional (the dashboard Queue + per-root panel may suffice) — included
so the "one place to see all jobs" flow is reviewable.

---

## Screen 6 — Menu action flows (launching an operation)

Each menu key collects its target inline (a path prompt / picker), then submits and drops you back
to the dashboard Queue to watch. **Nothing is refused at submit** (§3) — a busy worker or under-review
root just enqueues (shown `queued`/`blocked`).

### 6a · [s] Scan / [d] Dedup — pick a registered root
```
┌─ Scan a folder ──────────────────────────────────────────────────────────────┐
│  Which root?                                                                   │
│   ▸ iPhone      D:\Backup\iPhone       ● scanned 2h ago                        │
│     Camera      E:\Photos              ● scanned 1d ago                        │
│     Photos      E:\Photos2             ○ never scanned                         │
│   ─────────────────────────────────────────────────────────────────────────   │
│   [ ] --full (re-fingerprint everything)   [ ] --dry-run                       │
│   ↑/↓ select   Space toggle option   [Enter] scan   [Esc] cancel               │
└────────────────────────────────────────────────────────────────────────────────┘
```
**‹notes›** Root list from `roots_snapshot()` (library roots; a trash root is not offered — scan
rejects it). `[Enter]` → `submit_scan(root, full=, dry_run=)`. Dedup is the same picker without the
flags (dedup analyze takes no options; `--confirm`/`--cancel` come from a pending run's card, 4c).

### 6b · [m] Merge — source path + destination root
```
┌─ Merge into a folder ────────────────────────────────────────────────────────┐
│  Source folder (temp export, read-only):                                       │
│    E:\iphone_dump_2026-07__________________________________                    │
│                                                                                │
│  Into (a library root, or a subfolder of one):                                 │
│   ▸ iPhone      D:\Backup\iPhone                                               │
│     Camera      E:\Photos                                                      │
│     …or type a subfolder path:  D:\Backup\iPhone\2026\________                 │
│   ─────────────────────────────────────────────────────────────────────────   │
│   [ ] --dry-run (preview counts; still refreshes trash)                        │
│   [Enter] merge   [Esc] cancel                                                 │
└────────────────────────────────────────────────────────────────────────────────┘
```
**‹notes›** `submit_merge(source, into, dry_run=)`. `into` resolves by containment (a root name or a
subfolder path — `resolve_dest`, §8 C). A bad `--into` (under no library root / a trash root) returns
a 400 the flow surfaces inline.

### 6c · [t] Trash — refresh or cleanup
```
┌─ Trash ──────────────────────────────────────────────────────────────────────┐
│   [1] Refresh trash collection   — absorb + empty all trash folders (§6.1)     │
│   [2] Cleanup a library folder   — cull trashed / undecodable files            │
│   [Esc] back                                                                   │
│                                                                                │
│  ── if [2] Cleanup ───────────────────────────────────────────────────────    │
│  Root:  ▸ iPhone   Camera   Photos                                             │
│  Mode:  (•) --trash-exact   ( ) --trash-perceptual   ( ) --undecodable         │
│  [ ] --dry-run     [Enter] run   [Esc] cancel                                  │
│                                                                                │
│  ⚠ exact / undecodable delete: a typed count-confirm appears before deleting.  │
└────────────────────────────────────────────────────────────────────────────────┘
```
**‹notes›** `[1]`→`submit_trash_refresh()`. `[2]`→`submit_cleanup(root, mode=, dry_run=)`. For the
one-shot delete modes (exact/undecodable), the TUI runs the preview job, reads `cleanup_preview`, and
shows a **typed count-confirm modal** (mirrors the CLI) before the apply job. `--trash-perceptual`
stages for review → then appears as a pending-review card (4c) with the trash convention.

### 6d · [r] Manage roots
```
┌─ Manage roots ───────────────────────────────────────────────────────────────┐
│   iPhone      D:\Backup\iPhone       library   98,412 assets   ● 2h ago        │
│   Camera      E:\Photos              library   26,150 assets   ● 1d ago        │
│   _Trash      D:\Backup\_Trash       trash     —                               │
│                                                                                │
│   [a] Add a root (register)      [Enter] view root detail                      │
│   [u] Unregister   [n] Rename    ‹deferred — §14 #9›                           │
│   [Esc] back                                                                   │
│                                                                                │
│  ── [a] Add a root ──────────────────────────────────────────────────────     │
│   Path:  D:\Backup\NewPhone__________________________                          │
│   Name:  (leaf: NewPhone)_______   Kind: (•) library  ( ) trash                │
│   [ ] --scan after registering    [Enter] register   [Esc] cancel              │
└────────────────────────────────────────────────────────────────────────────────┘
```
**‹notes›** List = `roots_snapshot()`. `[a]`→`register_root(path, name=, kind=, scan=)`. `[u]`
unregister / `[n]` rename are **greyed out** until the deferred `roots unregister`/`rename` verbs
land (§14 #9) — per tenet §1.6 the TUI can't offer an action with no CLI verb behind it, so they're
shown-but-disabled with a "deferred" note rather than hidden.

---

## Cross-cutting behavior (all screens)

- **Live.** Queue/running panels subscribe to the running job's **SSE stream** (moving bar, ETA);
  stats/roots/history poll read-only snapshots on a light timer. A job started in another terminal
  appears here automatically.
- **Keyboard-first, mouse optional.** `↑/↓` moves selection in the focused list; `Tab` cycles focus
  (Roots → Queue → per-root jobs); bracketed keys are the actions. Single global keys (`s d m t r j q`)
  launch from anywhere on the dashboard.
- **Observe-and-control, not a file manager.** Review decisions are made in Explorer (`[o]` opens the
  staging folder); the TUI only stages/confirms/cancels. It never previews or edits media.
- **Every action = a CLI verb (tenet §1.6).** No TUI-only capability; a not-yet-built verb
  (unregister/rename) is shown disabled, never as a live-but-fake button.

---

## Open questions for review

1. **Screen 5 (dedicated Jobs view) — keep or fold in?** The dashboard already shows the Queue +
   per-root history. Is a separate full-screen jobs list worth it, or is `[Enter]`-on-a-queue-row
   detail enough? (Leaning: keep it — it's the "all jobs, all roots, one place" view + matches
   `packrat jobs`.)
2. **Prioritize `[p]` in the TUI** — the mockups add `[p]` to the Queue/Jobs panels (maps to
   `jobs prioritize`). §12's original mockup predates the prioritize feature; confirm we want it as a
   TUI key (yes per tenet §1.6, since it's a CLI verb).
3. **Count-confirm modal for exact/undecodable cleanup** — shown as a blocking modal (6c). OK, or
   should the TUI route one-shot deletes to "run in a terminal" to keep the typed-count gate purely
   CLI? (Leaning: modal — it's still a typed count, just in the TUI.)
4. **Logo/mascot** — placeholder art here; final ASCII rat is a cosmetic detail to settle.
5. **Merge `--into` subfolder entry** (6b) — a picker + free-text hybrid. Acceptable, or
   root-only for v1 (type the subfolder in a later pass)?
```
