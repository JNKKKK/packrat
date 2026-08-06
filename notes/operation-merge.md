# Merge a folder into an existing folder

The headline use case: export the whole iPhone to a temp folder, then copy only the
genuinely-new items into the backup folder.

**Merge is deliberately simple: `merge = discard trash + copy what's new`, decided entirely by
exact content hash.** No perceptual/near-dup matching, no CLIP, no review folder, no interactive
pause. It does collapse **byte-identical** duplicates (within the source and against the
collection), but *recompressed* near-dup cleanup is a separate concern handled by `dedup` (see [dedup](operation-dedup.md))
*after* the files are in the collection.

```
packrat merge "E:\iphone_dump" --into "D:\Backup\iPhone"          # copy new files in
packrat merge "E:\iphone_dump" --into "D:\Backup\iPhone" --dry-run  # preview counts only
```

**Guarantees:** the **source is never modified** (read-only). The destination is **copy-only**
(no deletes/overwrites of existing content). "New" is judged against the **entire collection** by
exact hash, and files matching a **trashed** hash are discarded.

**Phase 0 — Validate & refresh trash**
1. `source` must exist, be readable, and be non-empty. It is treated as a **transient temp
   folder**, not a root — its files are not part of the collection.
2. `dest` must resolve inside a registered **library root** (create the subfolder if missing),
   so that copied files become catalogued members of the collection. If `dest` is under no library
   root → error (offer to `roots register` it first). Reject if `source` and `dest` overlap. **Ignored
   dest (warn, don't block):** if the resolved `dest` itself falls under the root's ignore rules
   (allowlist/`--ignore` globs, see [scan](operation-scan.md)), do **not** hard-error — files still copy, but they will be
   left *uncatalogued* (Phase 3 step 11) and merge warns per ignored subpath (Phase 4 step 13),
   because registering under an ignored path would let the next scan silently forget them. A plain
   note here is enough; the loud warning is at report time once the exact count is known.
2a. **Per-root exclusivity (see [architecture](architecture.md), guarantee 2), on the dest root.** If the dest library root already
   has an active operation — a `pending` `review_runs` row (dedup/cleanup) or another open
   `merge_runs` (its own partial-unique index enforces the latter, see [data model](data-model.md)) — the merge job is **held in
   the queue (dequeue gate, see [architecture](architecture.md)) until that holder clears**, not run against it: a merge stages/copies
   under this root and its step-4 opportunistic scan would churn it, so it must own the root cleanly
   before proceeding, and it acquires that ownership only when it actually runs (opening `merge_runs`
   at step 5). (A `--dry-run` merge opens no run and writes nothing — but it also then skips step 4's
   scan, see below.)
3. **Refresh the trash collection** (see [trash refresh](operation-trash-refresh.md)) — absorb any files sitting in the registered trash
   roots into the trashed-hash set and empty those folders. Merge discards incoming files that
   match a trashed hash, so the trashed set must be current first. (Runs for real even under
   `--dry-run` — see below.)
4. Opportunistically fast-path-scan the `dest` root so the comparison set is current; warn if
   the collection index is stale. (This runs under merge's ownership from step 2a — no other op can
   touch the root meanwhile. Skipped under `--dry-run`, which must not mutate the catalog.)
5. Open a `jobs` row (`type='merge'`) and a **`merge_runs`** header (`status='planning'`,
   `dest_root_id`). The `merge_runs` row is the durable **cross-op guard**: its
   partial-unique `(dest_root_id) WHERE status IN ('planning','copying')` is exactly the
   "in-flight merge plan targeting this root" that dedup (see [dedup](operation-dedup.md) Phase 0) and cleanup (see [cleanup](operation-cleanup.md)) wait
   behind (dequeue gate, see [architecture](architecture.md)). **Dry-run opens neither `merge_runs` nor `merge_plan_items`** — it must
   not trip that guard and has no resume need. This plan is internal crash-safety only — merge does not pause
   for the user.

**Phase 1 — Fingerprint source** (read-only w.r.t. source; writes only the frozen plan)
6. Enumerate source media files (same allowlist/ignore rules as scan).
7. For each: **BLAKE3 only** — no metadata, no perceptual signature, no embedding (classification
   in Phase 2 needs the exact hash alone). No `assets`/`file_instances`/`phash` rows are written —
   source files are not collection members. Persist each file as a `merge_plan_items` row
   (`source_rel_path`, `size`, `mtime`, `content_hash`, `progress='pending'`) so an interrupted
   run resumes **without re-hashing the source** (the dominant SMB cost, see [SMB/NAS performance](performance.md)). *(Metadata —
   dimensions/duration/captured_at — is deferred: it's consumed only when a `new` file is
   registered, so it's probed just-in-time in Phase 3 for `new` reps only, never for skipped
   files and never persisted in the plan.)*

**Phase 2 — Classify each source file by exact hash**
8. **Collapse exact-within-source duplicates first.** Group source files by `content_hash`; for
   any hash appearing more than once, keep a single **representative** (tiebreak: **oldest
   `mtime`**, then stable by path) and mark the rest `dup-in-source` (recording `rep_of_hash`) →
   not copied. This is cheap (the hashes are already computed in Phase 1) and prevents merge from
   copying two byte-identical files into the destination as redundant instances of one asset.
9. Classify each **representative** by exact `content_hash` against the DB — no perceptual
   comparison. Write each file's `classification` onto its `merge_plan_items` row, then flip
   `merge_runs.status='copying'`. **This classification is now frozen:** resume trusts it verbatim
   and never re-derives it against the live DB (see Safety & resume). Classifications:

   | Classification | Condition                                             | Action              |
   |----------------|-------------------------------------------------------|---------------------|
   | `dup-in-source`| a byte-identical sibling in the source is the rep     | **skip** (step 8)   |
   | `trashed`      | hash matches a `trashed` asset (exact)                | **discard** (skip)  |
   | `exact-known`  | hash matches an `active` asset (already in collection)| **skip** (have it)  |
   | `new`          | hash matches nothing                                  | **copy**            |

   Note: trash / exact-known / within-source matching are all **exact-hash only**. A
   *recompressed* copy of trashed or already-owned content is not caught here — it copies as
   `new`, and `dedup` collapses recompressed near-dups later. This is the accepted cost of keeping
   merge simple; only *byte-identical* redundancy is resolved at merge time.

**Phase 3 — Copy the `new` files & register** (backup DB first)
10. For each `new` representative, copy into `dest` **mirroring the source's folder structure**:
    - **Preserve the relative path.** A source file at `<source>\<rel>\name.ext` is copied to
      `<dest>\<rel>\name.ext`, creating intermediate subfolders as needed. This keeps whatever
      organization the export produced (e.g. `2024\jan\IMG.jpg`). Files directly in `<source>`
      land directly in `<dest>`. (Folder layout is only a *starting position* — you can freely
      reorganize in Explorer afterward; packrat tracks by fingerprint, not path.)
    - Preserve the filename. On a name collision **at the same relative path**, compare by hash:
      identical content → skip (already there); different content → append a numeric suffix
      (`name (1).ext`). Because structure is mirrored, same-name files in *different* source
      subfolders no longer collide — they land under their respective subfolders.
    - Write to a temp name → flush → **verify** the written file's BLAKE3 equals the source hash →
      atomic rename into place. (Guarantees no partial/corrupt files.) → set the item's
      `progress='copied'` and store its final `dest_path` (incl. any `(1)` collision rename).
11. **Register** each copied file — **but first check its final dest path against the dest root's
    ignore set** (the same allowlist + `--ignore` globs bound to the root, see [scan](operation-scan.md)), evaluated on the
    path *relative to the root* (not to `<dest>`), because that is exactly what a later `scan` will
    test. Two outcomes:
    - **Dest path is NOT ignored (the normal case)** → **write** `assets` (`status='active'`, hash
      from Phase 1 + metadata **probed now**, `new` reps only) and `file_instances` (pointing at the
      copied `dest` path), and set `merge_plan_items.progress='registered'`, all **in one
      transaction**. Register is idempotent — `assets` keyed by unique `content_hash`,
      `file_instances` by (`root_id`,`path`) (see [data model](data-model.md)) — so replaying a partially-done file is safe.
      Perceptual signatures are **not** computed here; a later `scan`/`dedup` of `dest` fills in
      `phash`/`vphash` (and `scan --embed` the embedding). It is now a collection member, so a future
      merge recognizes it.
    - **Dest path IS ignored** → **do NOT register** (write no `assets`/`file_instances` row); set
      `progress='copied-unindexed'` and record the ignored dest path. **Rationale (this is the fix
      for the silent-forget bug):** if we registered a file living under an ignored path, the next
      `scan` would not enumerate it → its `last_seen_at` would never bump → deletion-detection
      (see [scan](operation-scan.md) Phase 3 step 11) would delete its `file_instances` row and **forget the asset while the
      file still sits on disk** (and a later merge would re-copy it as `new`). By leaving it
      unregistered, the file is simply untracked — consistent with how scan treats *any* file under
      an ignore rule, regardless of how it got there. The file is copied (structure mirrored, as
      promised) but never enters the catalog. This is surfaced loudly in the Phase 4 report.
    Committing copy-marking (step 10) and the register/unindexed decision (step 11) as separate
    committed steps closes the **rename-but-not-registered gap**: a crash in between leaves an item
    at `progress='copied'`, which resume detects and finishes (re-running step 11's branch).

**Phase 4 — Report**
12. Copied: `new` N. Skipped: `exact-known` X, `trashed` Z, `dup-in-source` W. Collisions renamed
    R. Errors E. **Source unchanged.** Suggest running `scan <dest>` then `dedup <dest>` to
    fingerprint the new files and clean up any recompressed near-dups merge let through.
13. **Ignored-destination warning (only if any `copied-unindexed` items).** For **each distinct
    ignored dest subpath**, print a line like `⚠ 12 files copied to an ignored path
    (<dest>\cache\) — NOT catalogued; packrat won't track them, and a future merge will re-copy
    them as new.` Explain the consequence
    plainly: these files are on disk but **not tracked** — a later `scan`/`dedup`/`merge` will
    ignore them, and a future merge of the same source would re-copy them as `new`. Recommend
    either moving them to a non-ignored location (then `scan <dest>`) or adjusting the root's
    ignore rules if the exclusion was unintended. Grouping per subpath (not one line per file)
    makes the usual cause — a whole excluded subtree like `Screenshots\` or `**/cache/**` — obvious
    at a glance.

**Safety & resume:**
- A DB backup is taken before the Phase 3 copy.
- **Resume trusts the frozen plan.** Re-running `merge <source> --into <dest>` while an open
  (`planning`/`copying`) `merge_runs` row exists for this dest **silently auto-resumes** it
  instead of starting fresh — but **prints a clear notice** first (e.g. "Resuming interrupted
  merge from <created_at>: N of M files already copied") so the user knows a prior run is being
  continued, not restarted. It **skips Phase 1 entirely** (hashes already in `merge_plan_items`) and
  **does not re-classify** — it replays the stored classification verbatim. Per source-file:
  - `progress='registered'` or `copied-unindexed` → terminal; skip without even stat-ing the file
    (matters over SMB).
  - `progress='copied'` (crashed between rename and DB write) → the dest file already exists and
    is hash-verified; just re-run step 11's branch (register, or mark `copied-unindexed` if its
    dest path is ignored) — no re-copy.
  - `progress='pending'`, classification `new` → copy-verify-rename then step 11 (step 10–11).
  - `dup-in-source`/`trashed`/`exact-known` → nothing to copy; mark `skipped`.
  - **Consequence of freezing (accepted):** if the collection gained a matching asset during the
    crash→resume window (the worker slot frees on crash, and a plain `scan` isn't blocked, see [architecture](architecture.md)), a
    `new` file still copies — producing a redundant *byte-identical* instance, not corruption.
    `dedup <dest>` collapses it later. This is the deliberate cost of deterministic resume that
    never re-reads source bytes.
- **Finalize:** on completion set `merge_runs.status='done'`, `finished_at`; the run and its
  items are **retained** as queryable merge history (see [roadmap](roadmap.md) #5).
- **Interruption (two paths — merge has no interactive pause and no `--cancel` flag):**
  - **Cooperative cancel** — the *generic* job cancel (see [tech stack](tech-stack.md)) via the TUI `[c]` (see [tui](tui.md)) or another
    terminal; **not** Ctrl-C (which only detaches the view, see [cli](cli.md)) and **not** a merge-specific
    `--cancel` (that's a dedup/cleanup review verb). The worker sees the flag at its next
    per-file checkpoint, sets `merge_runs.status='cancelled'`, and stops. Already-copied files
    stay — merge is copy-only, so a partial copy leaves nothing unsafe; those files are now real
    collection members. Re-running `merge` does **not** auto-resume a `cancelled` run (it's a
    deliberate stop); it starts a fresh plan.
  - **Process death or clean `daemon stop`** (crash / reboot / power loss / graceful shutdown) —
    the run is left open (`planning`/`copying`) and its `jobs` row is reconciled to `interrupted`
    on next daemon start (see [architecture](architecture.md)), **not** `cancelled`; re-running `merge <source> --into <dest>`
    silently auto-resumes it per above. (This is why a stop/crash differs from a cancel: only the
    explicit cancel above discards the plan.)
- `--dry-run` runs Phases 1–2 logic **in memory only** and prints the classification counts /
  would-copy list — it opens **no** `merge_runs`/`merge_plan_items` rows (so it neither trips the
  cross-op guard nor leaves a resumable run) and writes no asset rows. It **also computes the
  would-be-ignored destinations** (test each `new` file's projected dest rel-path against the dest
  root's ignore set) and prints the same per-subpath ignored-destination warning as Phase 4 step
  13 — so the user learns about an ignored `--into` target *before* copying, when it is still
  cheap to fix. **But Phase 0's "refresh the trash collection" still runs for real** — trash
  folders are absorbed and emptied even in dry-run (see [trash refresh](operation-trash-refresh.md)); only the copy and all plan/asset writes
  are skipped.
- Merge is copy-only (non-destructive), so it proceeds without a typed confirmation; use
  `--dry-run` first to preview.

**Live Photos:** a paired `.HEIC` + `.MOV` is judged per file by hash. If you previously merged
one half, only the other half is `new` and copies — no special pairing logic in v1 (a
`--keep-pairs` option is a possible later addition; see [roadmap](roadmap.md) #2).

---
