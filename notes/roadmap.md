# Build milestones (each independently useful)

**Status: v1 (M0–M6) is DONE.** ✅ The complete register/scan/dedup/trash/merge workflow plus the
TUI — everything needed to hoard, dedup, and merge a real collection through Explorer — is
implemented. **M6.5 (probe + periodic scheduler + 4-state dot) also landed** post-v1. **M7
(semantic embeddings) and M8 (hardening) remain** and are not yet built.

**What "v1" means (resolves the scope ambiguity):** **v1 = M0–M6**. The "**(v1)**" qualifiers
elsewhere (non-goals in [goals and concepts](goals-and-concepts.md), the schema's deferred knobs in [data model](data-model.md)) refer to this scope. **M7 (semantic
embeddings) and M8 (hardening — scheduled scans, hnswlib, watchdog) are post-v1**; embeddings are
opt-in infrastructure whose tagging behavior is still TBD (see [embeddings](embeddings.md)), and M8 is polish/scale, not core
function.

- ✅ **M0 — Skeleton + job runtime + decode smoke test**: repo layout, **`config.toml` (see [config](tech-stack.md) —
  auto-create-with-defaults + per-job reload)**, core library, SQLite schema; auto-spawned daemon
  with the **single-worker job queue** (submit / stream progress / cooperative-cancel; one job
  *runs* at a time with the rest waiting in a **durable backlog**, dequeued **runnable-first** so a
  job whose owned root is under review is held+skipped (not rejected at submit) until the holder
  clears — see [architecture](architecture.md)) and **startup reconciliation** (orphaned `running` → `interrupted`, durable `queued`
  backlog drained with the destructive-apply carve-out; resume-on-re-run, see [architecture](architecture.md)), CLI
  client with **Ctrl-C-detaches** and
  `--detach`, `daemon start/stop/status`. **Plus the smoke test (see [tech stack](tech-stack.md))** — one real sample of every
  allowlisted extension (and the RAW group) run through decode→hash→perceptual→embed to resolve
  the ⚠ cells (AVIF, RAW/cr3, `pdqhash` Windows wheel) before building on them.
- ✅ **M1 — Register + scan (exact identity)**: `roots register` (metadata-only root creation) and
  `roots list`, then the `scan` job — walker, fast-path, BLAKE3, metadata, asset/file-instance
  model, exact byte-identity resolution (attach instances), deletion detection — plus `status`. No
  embeddings, no perceptual. Now the collection is known by exact hash.
- ✅ **M2 — Perceptual signatures (scan)**: PDQ for both photos and video frames (+ quality) written
  to `phash`/`vphash` during scan, with the sampling/quality parameters (see [fingerprints](fingerprints.md)). No pairwise matching
  yet — just the inputs. No GPU/CLIP. No `imagehash` dependency.
- ✅ **M3 — Dedup operation**: single-folder `dedup` as a **3-stage sequence** — matching engine (see [fingerprints](fingerprints.md))
  over DB fingerprints + lazy liveness, `similarity_edges`/`review_runs`(+`stage`/`stage_phase`)/
  `review_actions`(+`stage`) tables, exact-dup resolution (stage 1: oldest-mtime internal /
  drop-on-external), perceptual banding into recompression (stage 2, + all video) and minor-edit
  (stage 3, photo) stages, Windows-shortcut staging (`_exact_dup_to_delete\` /
  `_suspect_recompression\` / `_with_minor_edits\`), the pending+stage-cursor state machine with
  `--confirm` auto-advance, `--cancel`, `--dry-run`, and the audit trail (see [dedup](operation-dedup.md)) (`proposed.json` +
  `applied.json` in APPDATA). Builds the perceptual matching engine (see [fingerprints](fingerprints.md)) (also reused by
  `cleanup --trash-perceptual`).
- ✅ **M4 — Trash model**: multiple `kind='trash'` roots, "refresh the trash collection" (see [trash refresh](operation-trash-refresh.md) —
  index trash-folder files → record/flip assets to `trashed` → empty the folders), scan's refusal
  to index trash roots, `packrat cleanup` (mode-required: `--trash-exact` count-confirm removal;
  `--trash-perceptual` stateful staging of recompressed-trash matches for review — reuses the M3
  engine; `--undecodable` culls the folder's undecodable files + marks them trashed, see [tech stack](tech-stack.md)), and
  `trash refresh`. Comes before merge because merge's headline value is excluding
  trashed-but-still-on-device content.
- ✅ **M5 — Merge workflow**: `merge` — refresh-trash-first, exact-hash classification
  (dup-in-source / trashed / exact-known / new; byte-identical collapse only), copy-only ingest
  of `new` files with hash-verify + register. No perceptual matching or review folder — simple and
  one-shot (resumable from its plan).
- ✅ **M6 — TUI (`packrat` no-args)**: full-terminal Textual app (see [tui](tui.md)) — dashboard (logo + Collection
  stats + Roots + Queue), maximized Roots (sort cycle, add-root form, merge-from picker) and Queue
  (three independently-paged sections), root detail with per-root job history + the scan/dedup/merge/
  cleanup/review actions, and the per-`op` job result cards. Built as a **pure render core + thin
  Textual widgets**: responsive full-terminal layout (surplus model over a 100×24 minimum), CJK-aware
  width, role-based color on a transparent (acrylic-friendly) background, paste-aware path inputs, and
  every action submitting a real daemon call (see [design tenets](goals-and-concepts.md)). The default entrypoint and a live window onto
  daemon jobs from any terminal; `--offline` renders bundled sample data. Pure presentation over the
  M0 runtime's durable **queue** + per-job **`root_id`/`result_json`** columns (see [architecture](architecture.md) / [data model](data-model.md)).
- ✅ **M6.5 — Probe + periodic scheduler**: the `probe` job (see [probe](operation-probe.md) — cheap discovery: count
  new/changed files without fingerprinting; owns its root; one-pending-per-root submit-dedup;
  `packrat probe`/`--all`, `POST /probe`), the **general periodic scheduler** it realizes
  (`jobs/scheduler.py` — APScheduler `BackgroundScheduler` + a declarative `PeriodicTask` registry,
  in-memory jobstore, wired into the daemon lifespan; `probe-all` its first task, every 24 h), and
  the **4-state status dot** it drives (see [tui](tui.md) — `◉` green deduped / `◉` yellow need-dedup / `◐` grey
  new-files-probed / `○` grey never; `roots.last_probe_at`/`probe_new_count`, plus a `needs_dedup`
  event flag set by a scan/merge that indexes new dedup-able content and cleared on dedup completion —
  so a no-op re-scan never flips a deduped root back to yellow). This realizes the "Scheduler
  (APScheduler)" line (see [architecture](architecture.md)) ahead of M8's scheduled *scans*, and adds APScheduler as a core dep.
- **M7 — Semantic embeddings**: opt-in `scan --embed` CLIP pass writing the `embeddings` table;
  brute-force cosine search scaffold. Tagging/classification behavior on top is **TBD** (see [embeddings](embeddings.md)).
- **M8 — Hardening**: scheduled interval-scan triggers (**the scheduler now exists — M6.5**; M8
  adds a `full-scan-all` `PeriodicTask` on it), DB backup, resumability polish, larger-scale perf
  (hnswlib), SMB tuning (see [performance](performance.md)), optional watchdog real-time mode.

---

# Open questions / risks

1. **Near-dup thresholds** `t_photo_recompress`, `t_photo_edit`, and `T_match_video` need empirical
   tuning on your real data (burst shots and edited copies are the hard photo cases; heavy re-encodes
   the hard video ones). They are **separate cutoffs** (see [fingerprints](fingerprints.md)): `t_photo_edit` is the photo match
   decision and `t_photo_recompress` bands matched photos into dedup's stage-2/stage-3 review (see [dedup](operation-dedup.md));
   the video cutoff only feeds the `frame_match_fraction` vote and tolerates frame noise, so expect
   `T_match_video` to land more permissive. Calibrate all three — plus the `video.*` structure knobs —
   on a small labeled sample. **Single-signal risk (accepted, see [embeddings](embeddings.md) gap review):** photos rely on
   **PDQ alone** — pHash is not stored. If calibration shows PDQ-only precision/recall is
   inadequate, adding a second signal (pHash, or an AND/OR gate) means **re-decoding the whole
   collection** to backfill it (a multi-hour `--full`-style pass over SMB). The bet is that PDQ at
   sane thresholds is sufficient for the iPhone-re-export reality; validate on the labeled sample
   *before* the first full scan so a signal change is cheap.
2. **Live Photos & sidecars** (.AAE edits, paired .MOV): decide grouping rules.
3. **Video near-dup** is genuinely hard for heavy re-encodes; sampled per-frame **PDQ** +
   duration-aligned majority voting (see [fingerprints](fingerprints.md)) is a pragmatic start. Because the frame descriptor is
   already PDQ, the natural upgrade is **TMK+PDQF** (whose per-frame descriptor is a PDQ variant) —
   consider it if recall proves insufficient. The `video.*` knobs (frame count, fraction, quality
   gate) plus `T_match_video` all need calibration on real clips (see #1 above).
4. **Shortcut creation mechanism:** `.lnk` files need creating without a copy — via `pywin32`
   (`win32com` Shell.CreateShortcut) or `winshell`. Confirm thumbnail preview works for `.lnk`
   targets in Explorer (it does for real files; verify in the M3 spike). Fallback if `.lnk`
   previews disappoint: NTFS hardlinks (same volume only) or symlinks (needs privilege).
5. **Audit-trail retention (see [dedup](operation-dedup.md)):** the knob now exists — `audit.retention_days` in `config.toml`
   (see [config](tech-stack.md)), default `0` = keep forever. What remains deferred is only the **pruning pass** that acts
   on a `>0` value (nothing deletes old audits yet). **Merge:** its `merge_runs`/`merge_plan_items`
   rows are now **retained on completion** (see [merge](operation-merge.md) Safety & resume), giving merge a queryable
   in-DB history (source, dest, per-file classification/disposition). Open sub-question: do we
   *also* want merge to emit the same on-disk `proposed.json`/`applied.json` under
   `%APPDATA%\packrat\audit\merge\…` for symmetry with dedup/cleanup, or is the retained DB plan
   enough? (Leaning: DB plan is sufficient for v1; on-disk audit is a nicety.)
6. **Recompressed-trash on merge (accepted):** `merge` excludes trashed content by **exact hash
   only** — a *recompressed* copy of trashed content slips through as `new` on ingest. This is the
   accepted cost of keeping merge simple/one-shot; it is caught afterward by
   `cleanup <dest> --trash-perceptual` (see [trash model](operation-cleanup.md)), which stages recompressed-trash matches for review.
   (`dedup` still excludes trashed assets from grouping — see [fingerprints](fingerprints.md) — so cleanup is the dedicated path.)
7. **`packrat config` command (deferred):** v1 config is a hand-edited, auto-created
   `%APPDATA%\packrat\config.toml` (see [config](tech-stack.md)) — there is no CLI to read/write keys. A future
   `packrat config get/set` (with value validation and a `--json` view) is a nicety; the TOML
   format is chosen to be forward-compatible with it. Not needed for v1, which only requires the
   file to exist, self-document its defaults, and reload per job.
8. **Batch / list untrash (deferred):** v1 `untrash` (see [trash model](operation-untrash.md)) is **by-file only** — you present the
   file(s) to forget from trash memory, matched by exact hash. Deferred niceties: (a) a
   **read-only `packrat trash list`** (metadata-only view of trash memory — count, by reason, by
   date — no preview, since no pixels are stored); (b) a **batch `untrash --since <time>` /
   `--reason <r>`** to bulk-undo a bad refresh without re-presenting files (uses existing
   `trashed_at`/`trash_reason`; would need a typed count-confirm since it acts without a file in
   hand). Not required for v1: presenting recovered files (e.g. from the Recycle Bin) already covers
   the accidental-trash case.
9. **Root removal / rename (deferred):** v1's `roots` command has `register` (add, see [scan](operation-register.md)) and
   `list` (see [cli](cli.md)) — but not `roots unregister` (drop a root: delete its `roots` row + cascade its
   instances/orphaned assets, with a typed confirm) or `roots rename` (change a root's `name`
   handle, re-checking global uniqueness). Needed before the TUI's "Manage roots" panel (see [tui](tui.md)) can
   do more than add + list; scoped as a small follow-on to the `roots` group, not v1-critical.
10. **Scan-result retention (deferred; accepted growth):** every completed scan persists a
   `scan_results` row per root + a `scan_problem_files` row per current problem file (see [data model](data-model.md), [scan](operation-scan.md) Phase 5),
   kept **indefinitely** so the M6 TUI can navigate scan history. Two accumulation facts,
   accepted for now: (a) re-scanning a root **appends** a new `scan_results` row (never replaces),
   so a frequently-scanned root grows one row per scan; (b) because the undecodable problem set is
   re-derived from the catalog each scan, `scan_problem_files` re-inserts a row for *every* current
   undecodable on *every* scan — it grows **per-scan, not per-distinct-problem** (a root with 50
   permanent undecodables scanned 200× → ~10K rows, mostly duplicates). Rows are tiny so this is
   fine at v1 scale, but unbounded. Deferred fix: a retention knob (mirroring `audit.retention_days`,
   see [dedup](operation-dedup.md)) — e.g. keep the last N `scan_results` per root or prune older than N days, cascading their
   problem files — plus possibly deduping the current-undecodable list against the previous scan's.
   `status <root>` reads only the newest row, so this is purely storage hygiene, not correctness.
