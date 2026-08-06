# Data model (SQLite)

```sql
roots(
  id, path /* unique */, name /* unique, case-insensitive; leaf name or --name */,
  kind /* library|trash */, enabled, ignore_globs, last_full_scan_at,
  last_probe_at   /* when `probe` last completed on this root (see operation-probe.md; recency display) */,
  probe_new_count /* new/changed files probe last saw awaiting a scan. Set by a completed
                     probe; CLEARED to 0 by a completed scan of the root. Feeds the tui.md
                     4-state dot's top rung: a completed scan always zeroes it, so count>0
                     means exactly "a probe found unscanned files and no scan has consumed
                     them yet." Offline probe writes nothing (unreachable never reads clean). */,
  needs_dedup     /* dedup-dirty flag: 1 ⇒ scanned content awaiting a (re-)dedup (tui.md ◉ yellow).
                     SET (in the asset write's transaction, crash-atomic) when a scan indexes a
                     new/backfilled ACTIVE decodable asset, or a merge registers new content;
                     CLEARED to 0 when a dedup run reaches `completed`. An event signal, NOT a
                     last_dedup_at>last_scan_at recency test (last_scan_at bumps on every walked
                     file, so a no-op re-scan must NOT flip a deduped root back to yellow). */)
  -- (per-root scan interval deferred with scheduled scans → M8; not settable in v1)
  -- last_probe_at/probe_new_count/needs_dedup are nullable/default-0 (a row predating them reads
  --   NULL/0 = current behavior); the live DB gets them via db.connection._ensure_added_columns
  --   (ALTER TABLE ADD COLUMN), a fresh DB from SCHEMA_SQL — no migration runner (pre-release — this file).

assets(
  id, content_hash /* blake3, unique */, media_type /* photo|video (by extension) */,
  size, width, height, duration_s, captured_at /* from EXIF/ffprobe */,
  status /* active|trashed  -- no 'missing': forgotten assets are deleted */,
  undecodable /* 0|1, default 0: bytes hashed OK but the decoder rejected the pixels — set by scan;
                 such an asset has NO phash/vphash/embedding and is excluded from perceptual work.
                 Orthogonal to status (an undecodable asset is still active/trashed). Distinct from
                 a merge-created asset that is simply not-yet-fingerprinted (undecodable=0, no phash
                 yet) — see the "fully fingerprinted" predicate in operation-scan.md A2 step 4. */,
  decode_error /* nullable text: last decoder failure detail, for debugging POC-format wheels (tech-stack.md) */,
  codec /* nullable text, VIDEO only: codec name (h264|hevc|av1|vp9|…) from the decode probe (operation-scan.md
           step 8). Feeds the video stage-2 keep-lead's codec-efficiency weight (operation-dedup.md match.codec_weights).
           NULL for photo/undecodable. NOT recomputed by `scan --full` (which skips re-decoding a
           byte-unchanged, fully-fingerprinted hit — operation-scan.md A2 step 6), so a pre-existing NULL persists. */,
  added_at, trashed_at, trash_reason)

file_instances(   -- presence = row existence; a gone file has its row deleted (no 'present' flag)
  id, asset_id, root_id, path, filename, size, mtime, last_seen_at)
  -- UNIQUE(root_id, path): one row per physical file. Scan's exact-dup "hit" upserts on this key
  --   (operation-scan.md A2 step 6), so re-encountering a known file (--full re-hash, mtime drift, merge-created
  --   backfill) reuses its row instead of creating a duplicate instance. `path` is stored in the
  --   canonical long-path-safe form (operation-register.md step 1) so equality is well-defined.

phash(   asset_id, algo /* always 'pdq' — photo pHash dropped (fingerprints.md); column kept for a stable
                          shape and to leave room for a future algo without a migration */,
         bits /* blob, 256-bit PDQ */,
         quality /* 0-100 PDQ quality */ )                    -- one row per photo asset; by scan
vphash(  asset_id, frame_index, t_offset_s,
         pdq_bits /* blob, 256-bit PDQ of the sampled frame — same algo as photos (fingerprints.md), NOT
                     pHash anymore */,
         quality /* 0-100 PDQ quality; frames below video.min_frame_quality are excluded from
                     matching but still stored/flagged (fingerprints.md) */ )   -- one row per sampled frame; by scan
embeddings( asset_id, model, vector /* float32 blob, e.g. 512d */ )  -- only if scan --embed

similarity_edges(   -- pairwise near-dups; written by `dedup`, NOT scan. `distance` = PDQ Hamming
                    --   for photo (≤ t_photo_edit), or the video match score (fingerprints.md); the medium's
                    --   own cutoff decides the edge — t_photo_edit (photo) / T_match_video per-frame
                    --   + frame_match_fraction vote (video). dedup bands the photo distance into its
                    --   review stages via t_photo_recompress (operation-dedup.md); the edge itself is unbanded.
  asset_a, asset_b, media_type, distance,
  algo /* pdq|video */, created_at )
  -- CANONICAL ORDERING: always store with asset_a < asset_b (numeric id order). An edge is
  --   undirected, so this makes each pair have exactly ONE row; UNIQUE(asset_a, asset_b) then
  --   actually prevents duplicates (without it, {5,8} and {8,5} would both insert). Writers must
  --   normalize the pair before upsert; readers query both directions by testing asset_a OR asset_b.

review_runs(   -- one stateful review lifecycle (dedup OR perceptual-cleanup) per target root
  id, root_id, run_type /* dedup|cleanup-perceptual */,
  status /* pending|completed|cancelled */,
  stage /* dedup: 1=exact, 2=recompression, 3=minor-edit; the cursor within the run. cleanup: 1 */,
  stage_phase /* staged (shortcuts written, awaiting user) | applied (this stage's deletions done,
                 next stage not yet staged) — the apply-then-advance crash marker (operation-dedup.md Phase 7) */,
  prefer_internal /* dedup --prefer-internal: keep the INTERNAL copy on exact-match survivor +
                      keep-lead ties (default 0 = external is master). LOCKED at analyze; read from
                      this row by every --confirm (a bare confirm stages stage 2 and must apply the
                      same policy stage 1 used) — NOT re-passed per command like --keep-suggested.
                      A conflicting flag on a later --confirm is rejected (operation-dedup.md). */,
  t_photo_recompress, t_photo_edit, t_match_video /* PDQ thresholds SNAPSHOTTED at analyze (operation-dedup.md),
                      same lock-at-analyze pattern as prefer_internal: the run's stage bands + the
                      review histogram bins derive from these, so the CLI log and the TUI poll read
                      ONE source (review_stats.thresholds_from_row) and a later config.toml edit can't
                      retroactively rewrite an old run's bands. Nullable — a row predating the columns
                      reads NULL → callers fall back to review_stats._T_* defaults (no migration). */,
  created_at, confirmed_at )
  -- partial UNIQUE(root_id) WHERE status='pending'  → at most one open review run per folder.
  --   ONE row spans dedup's whole 3-stage sequence; `stage`/`stage_phase` track progress within it,
  --   `status` stays 'pending' until the LAST non-empty stage applies (operation-dedup.md).
  -- One facet of the architecture.md per-root exclusivity invariant: dedup, perceptual-cleanup, in-flight
  --   merge, AND scan are mutually exclusive on a root (scan is blocked by operation-scan.md A2 step 1a, not by
  --   this index, since scan opens no review_runs row).

review_actions(   -- the persisted, crash-safe plan for a review_run
  id, run_id,
  stage /* dedup 1|2|3 — which stage this action belongs to (--confirm applies WHERE stage=cursor);
            NULL for cleanup, which is single-stage */,
  folder /* exact_dup_to_delete|suspect_recompression|with_minor_edits|perceptually_identified_trash */,
  kind /* exact|perceptual */,
  reason /* exact-internal|exact-external|exact-internal-preferred|perceptual|cleanup-perceptual */,
  default_action /* delete|keep */,
  asset_id, instance_id, path,           -- the file this action targets
  survivor_instance_id,                  -- the copy being kept (stage-1 exact); NULL otherwise
  group_no, member_no, is_external,      -- perceptual grouping only (stages 2/3, cleanup)
  is_lead, lead_reason,                  -- stage-2 keep-lead: 1 on the suggested lead + why it won
                                         --   (ranking-key decision level); persisted so the read-only
                                         --   TUI poll needn't re-rank (lazy-liveness). NULL elsewhere.
  matched_trashed_asset_id, distance,    -- cleanup-perceptual only (which trashed asset, PDQ dist)
  shortcut_name )
  -- `path` is the AUTHORITATIVE target: --confirm re-stats it (operation-dedup.md Phase 6) and never trusts the
  --   DB row's liveness. So `asset_id`/`instance_id`/`survivor_instance_id` are recorded for
  --   reporting/reference and MUST tolerate becoming dangling: a legitimate scan of a *referenced*
  --   (external) root can forget a now-gone asset mid-review (architecture.md owned-vs-referenced). These FKs are
  --   therefore NOT part of any ON DELETE CASCADE — deleting an asset/instance must NOT delete
  --   review_actions rows (they'd be nulled/left dangling); confirm resolves a dangling ref toward
  --   sparing via the path stat. (The owned root can't be churned mid-review — per-root exclusivity
  --   architecture.md blocks scan on it — so only external references can dangle.)

merge_runs(   -- one merge lifecycle (operation-merge.md); the frozen plan header + cross-op guard
  id, job_id, source_path, dest_path, dest_root_id,
  status /* planning|copying|done|cancelled|error */, created_at, finished_at )
  -- partial UNIQUE(dest_root_id) WHERE status IN ('planning','copying')
  --   → at most one open merge per dest root; this is the "in-flight merge" marker that
  --     dedup (operation-dedup.md Phase 0) and cleanup (trash-model.md) check to refuse an overlapping run.
  -- completed runs are retained (queryable merge history; see operation-dedup.md note / roadmap.md #5).

merge_plan_items(   -- the persisted, crash-safe, FROZEN per-source-file plan for a merge_run
  id, run_id,
  source_rel_path,                 -- path relative to source; dest mirrors it (operation-merge.md Phase 3)
  size, mtime, content_hash,       -- from Phase 1; hash lets resume SKIP re-hashing + verify collisions
  classification /* dup-in-source|trashed|exact-known|new */,
  rep_of_hash,                     -- dup-in-source only: the sibling hash whose rep this defers to
  dest_path,                       -- final dest path incl. any numeric-suffix collision rename; NULL until copied
  progress /* pending|copied|registered|copied-unindexed|skipped|error */, error )
  --   copied         = file written+verified, DB register still pending (the crash gap; resume finishes it)
  --   registered     = terminal: file on disk AND catalogued
  --   copied-unindexed = terminal: file written to an IGNORED dest path, deliberately NOT registered
  --                      (would otherwise be forgotten by the next scan's deletion-detection — operation-merge.md Phase 3)
  -- NOTE: no metadata columns — dimensions/duration/captured_at are probed just-in-time in
  --   Phase 3 for `new` reps only (classification needs the hash alone), so they never persist.

-- tags(...) omitted for now — tagging/classification schema is TBD (embeddings.md)

jobs(    id, type,
         root_id /* nullable: the single root this job concerns — `scan <root>`, `dedup`, `cleanup`,
                    `merge`→dest. NULL for multi-root (`scan --all`) and root-less (`untrash`,
                    `trash refresh`) jobs. The TUI's per-root job list keys off this column (plus
                    `scan_results` for the per-root rows a `--all` scan writes); see tui.md. */,
         status /* queued|running|done|error|cancelled|interrupted */,
         total, done, enqueued_at, started_at, finished_at, error,
         result_json /* nullable: a compact, uniform, human-showable OUTCOME summary written at
                        terminal time by EVERY job, whatever its type or terminal status — scan
                        banner counts, dedup/cleanup staged/applied tallies, merge copied/skipped,
                        trash-refresh absorbed/emptied, untrash untrashed/forgotten. This is the
                        single surface the TUI renders as a job's "result card" (tui.md) WITHOUT joining
                        per-op tables; the richer per-op tables (scan_results/review_runs/merge_runs +
                        the operation-dedup.md audit) stay authoritative for deep forensics ([Enter] details). A job
                        that died before finishing may carry a partial or NULL result_json — its
                        `status` (+ `error`) still records the outcome, so every job is show-able. */,
         params_json )
  -- `enqueued_at` = when the row was created (as `queued`); `started_at` = when the worker actually
  --   BEGAN running it (NULL while still queued); `finished_at` = terminal time. FIFO order =
  --   enqueued_at (ties by id). A job submitted while the worker is free is enqueued and started in
  --   the same breath (both stamps ~together); one submitted while busy waits with started_at NULL
  --   in the durable backlog until it runs (architecture.md guarantee 1).
  -- `total`/`done` are a PROGRESS-DISPLAY counter only (work units finished / total, drives the
  --   bar + ETA). They are NOT the resume mechanism: on re-run each op recovers from its own
  --   authoritative durable state — scan from the fast-path (path+size+mtime skip) + last_seen_at
  --   (operation-scan.md A2), merge from per-item merge_plan_items.progress (operation-merge.md), review from review_actions
  --   (operation-dedup.md). `done` may be stale after a crash (last increment uncommitted) and that's harmless,
  --   because the authoritative state — not `done` — decides what re-runs.
  -- `queued` = submitted while a mutating job was running; waits in the durable FIFO backlog (architecture.md
  --   guarantee 1). Retained across a daemon restart and drained in order — EXCEPT a queued
  --   destructive apply (`dedup`/`cleanup --confirm`) is flipped to `interrupted` on restart, never
  --   auto-run with nobody watching (architecture.md reconciliation). Cancelling a queued job just drops it from
  --   the backlog (`cancelled`, never ran).
  -- `interrupted` = the daemon died while this job was `running` (crash/kill/power loss); set by
  --   startup reconciliation (architecture.md), NOT by the worker. It means "the process vanished, the durable
  --   per-op plan is intact, re-run the command to resume." A clean `daemon stop` also lands here
  --   (interrupted, resumable) — distinct from a user cancel, which is `cancelled` (terminal, architecture.md).

scan_results(   -- persisted scan report; one row per (completed scan job, root) so `status <root>`
                --   (and the M6 TUI) can re-render a past scan (operation-scan.md A2 Phase 5, cli.md).
  job_id, root_id, root_name,
  full, embed, profiled,                 -- the flags that produced this scan
  candidates, new, exact_dup, backfilled, matches_trashed, skipped_fastpath,
  undecodable, errors, deleted_instances, forgotten_assets, root_offline,   -- the operation-scan.md A2 banner counts
  profile_json /* ScanProfiler snapshot, NULL unless --profile */, created_at )
  -- PRIMARY KEY (job_id, root_id). A `--all` scan writes one row PER library root under a single
  --   job_id; re-scanning a root APPENDS a new row (new job_id) — the table is a growing per-root
  --   HISTORY, kept indefinitely (retention deferred — roadmap.md #10). `status <root>` reads the newest
  --   (job_id DESC). ONLY a *completed* scan writes rows — dry-run/cancel/interrupt/error write none
  --   (persist runs after the per-root loop); resuming an interrupted scan re-runs and writes then.
  -- CRUCIAL: `undecodable` (and the scan_problem_files below) are RE-DERIVED FROM THE CATALOG at
  --   scan end (assets.undecodable=1 with a live instance in the root), NOT counted per-pass — because
  --   a resume/incremental re-run FAST-PATH-SKIPS undecodables (they're "fully fingerprinted", operation-scan.md A2
  --   step 4), so a per-pass count would wrongly empty out on re-run. So this row describes the ROOT's
  --   current state, not just what this pass touched. (The other counts ARE per-pass activity.)

scan_problem_files(   -- the undecodable / unreadable files behind scan_results' counts, so the exact
                      --   paths + reasons are retrievable (not just counted). Keyed to the scan job.
  id, job_id, root_id, path, media_type,
  problem /* undecodable|read-error */,
  content_hash /* NULL for read-error — bytes never read */, detail /* decode_error or OSError text */ )
  -- `undecodable` rows are re-derived from the catalog each scan (see scan_results) → the same set
  --   re-appears on every scan of the root (grows per-scan, not per-distinct-problem — roadmap.md #10).
  -- `read-error` rows are per-pass: an unreadable file has no asset to re-derive, and leaves no row
  --   to fast-path-skip, so it is re-detected on every pass anyway.
```

Notes
- **Two asset states only (`active`/`trashed`), presence = row existence.** When a file is found
  gone, its `file_instances` row is deleted. Then: if the asset is `active` and now has **zero**
  instances → **delete the asset and all its dependent rows** (`phash`, `vphash`, `embeddings`,
  `similarity_edges`) — we forget it entirely, because a plain filesystem delete must not be
  remembered as trash (see [trash model](trash-model.md)). If the asset is `trashed`, it is kept at zero instances (its
  fingerprint is the trash memory). Enforce dependent-row cleanup with `ON DELETE CASCADE`.
- `assets` rows with `status='trashed'` **retain their fingerprints forever** — this is the
  trash memory used to exclude re-appearing junk. The physical file may be long gone.
- **Unreachable-root / incomplete-listing guard (per-directory):** deletion-detection (removing
  gone instances) reconciles an instance only if its **containing directory was fully and cleanly
  enumerated** this pass. If a directory listing errored/timed out mid-scan (common on SMB — see
  [performance](performance.md)), instances under *that subtree* are left untouched; a fully offline root (unplugged drive,
  missing share) skips everything. Incomplete data must never be read as "files deleted," which
  would wrongly forget fingerprints — but the scope is the affected subtree, not the whole root, so
  one flaky folder doesn't stall reconciliation collection-wide (see [scan](operation-scan.md) A2 step 11, [performance](performance.md)).
- Vector search: start with a memory-mapped numpy matrix (100K × 512 float32 ≈ 200 MB;
  brute-force cosine is milliseconds). Upgrade to `hnswlib`/`sqlite-vec` only if needed.
- Perceptual candidate search: brute-force Hamming in numpy, or a BK-tree if it gets slow.
