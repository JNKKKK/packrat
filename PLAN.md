# Media Collection Manager — Project Plan

Project name: **`packrat`** — a local, GPU-accelerated daemon + CLI/TUI for
managing (not displaying) a personal photo/video collection: fingerprint-based dedup
and an Explorer-driven "merge new stuff / trash junk" workflow. Hoards
everything, but keeps a system.

Target: Windows 10, single user, RTX GPU available, collection >100K assets.

---

## 1. Goals & non-goals

**Goals**
- Treat the entire collection as **one logical set**, spanning multiple folders ("roots").
- Track assets by **content fingerprint**, never by path. Files may be freely moved/renamed
  in Explorer without the system losing track of them.
- **Merge** workflow: fingerprint a temp folder and copy into a destination folder *only* the
  assets new to the whole collection, deciding by **exact hash** (byte-identical copies collapse;
  trashed content is excluded).
- **Dedup** as a separate, reviewed operation — find **perceptual near-duplicates**
  (re-compressed / resized / re-encoded), for **both photos and video**, and stage them in
  Explorer for the user to resolve. Not automatic and not part of merge.
- **Trash memory**: remember fingerprints of assets I've deliberately trashed, so they are
  excluded from future merges even when re-exported from the iPhone.
- **Semantic embeddings** (opt-in CLIP pass) stored per asset, to enable later capabilities
  such as semantic search and junk-flagging — with any human review done **in Explorer**. The
  embedding is infrastructure only; specific tagging/classification behavior is TBD (§7).
- Run work in a background **daemon** so jobs outlive the terminal that launched them; drive it
  through a **CLI** and an ASCII **TUI** (the `packrat` no-args entrypoint).

**Non-goals (v1)**
- No gallery/viewer UI. Explorer *is* the UI for reviewing files.
- No cloud, no multi-user, no mobile app.
- No editing of asset pixels/metadata (we only copy/move whole files).

**Design tenets**
1. **Fingerprint is identity.** Paths are just where a fingerprint currently lives.
2. **Explorer is the review surface.** The system stages files into folders; the human
   accepts/rejects by keeping/removing files; the CLI resumes.
3. **Never destroy silently.** Merges are copy-only. Deletions go to a trash folder /
   Recycle Bin, and originals are removed only after explicit confirmation.
4. **Idempotent & resumable.** Any index/merge/tag job can be interrupted and re-run.
5. **Lazy when safe, thorough on schedule.** Skip re-fingerprinting when `path` + exact `size` +
   near-`mtime` (tolerant) are unchanged; do full sweeps on a fixed interval as the backstop.

---

## 2. Core concepts

- **Asset** — a unique piece of content, identified by fingerprint. This is the thing we
  "know exists." Has status: `active` or `trashed` — there is no `missing`. When a non-trashed
  asset loses its last file instance, we simply **forget it** (delete the asset and its
  fingerprints), because a plain filesystem delete must not be remembered (§6). Only `trashed`
  fingerprints are retained across zero instances.
- **File instance** — a physical file at a path on disk. Many file instances can map to one
  asset (same photo living in two folders). **Presence is row existence**: a `file_instances`
  row exists iff we believe a file lives at that path; discovering it gone deletes the row (no
  `present` flag). This split is what makes "track by fingerprint,
  files move around" work cleanly.
- **Root** — a registered folder tree, each with a globally-unique name (§8 A1). Types:
  - `library` root — folders whose contents belong to the collection (e.g. the iPhone backup
    folder). Indexed by `scan`.
  - `trash` root — a transient **inbox**: files the user drops in are absorbed into the permanent
    trashed-hash set and the folder emptied ("refresh the trash collection", §6.1). **`scan` never
    touches trash roots.** Any number of trash roots may exist; they form one logical trashed set.
- **Fingerprint layers** (cheap → expensive):
  1. **Fast-path key**: `path` + exact `size` + tolerant `mtime` (§8 A2) — used only to *skip*
     re-fingerprinting unchanged files, never for identity.
  2. **Content hash**: BLAKE3 of file bytes — exact-duplicate identity.
  3. **Perceptual signature**: robust to recompression/resize.
     - Photo: PDQ (256-bit, primary) + pHash (corroborating).
     - Video: duration + sequence of frame pHashes sampled across the timeline.
  4. **Semantic embedding**: CLIP vector — computed **only** on an opt-in `scan --embed`, for
     future semantic search / junk-flagging (§7, TBD). **Never used in any duplicate decision**
     (dedup, merge, or cleanup) — those rely solely on the content hash (exact) and perceptual
     signature (near-dup).

---

## 3. Architecture

Two thin clients (CLI, TUI) drive a background **daemon** that actually runs the work. The
daemon owns the DB and a single-worker job queue; clients only submit jobs, stream progress, and
cancel. This is what makes a job survive the terminal that launched it.

```
   ┌────────────────────┐        ┌────────────────────┐
   │ CLI  `packrat scan…`│        │ TUI  `packrat`     │   ← logo + stats + live/recent
   │ (Typer)            │        │ (Textual)          │      jobs + operation menu
   └─────────┬──────────┘        └─────────┬──────────┘
             │  submit job / stream progress / cancel   (localhost HTTP + token)
             └───────────────┬──────────────┘
                             ▼
             ┌───────────────────────────────────────────────────────────┐
             │                      packrat daemon                        │
             │  ┌──────────────────────────────────────────────────────┐ │
             │  │ Job queue — ONE mutating job at a time (serialized)   │ │
             │  │   scan · dedup · merge · cleanup · trash refresh ·    │ │
             │  │   embed — each cooperatively cancellable + resumable  │ │
             │  └──────────────────────────────────────────────────────┘ │
             │  Scheduler (APScheduler → interval scans)                  │
             │  core library (fingerprint · match engine · trash · review)│
             │  SQLite (WAL)  +  perceptual/vector search  +  (opt) CLIP  │
             └───────────────────────────────────────────────────────────┘
```

**Daemon** — single long-running process, auto-spawned detached on first client use (no manual
"start the server" step). Owns:
- the **SQLite DB** (single writer — see concurrency below);
- a **persisted job queue** running **one mutating job at a time**; each job is cooperatively
  cancellable and checkpointed/resumable (per §8);
- the **scheduler** for interval scans (submits scan jobs like any client);
- the review-run state (`review_runs`) and audit trail.
Exposes a small HTTP API on `127.0.0.1` with a local token (`%APPDATA%\packrat\token`).

**CLI** — thin client. `packrat scan D:\…` submits a job and **streams its progress**. Key
property: **the job runs in the daemon, not the terminal**, so:
- **Ctrl-C detaches the view, it does NOT stop the job.** The CLI prints "still running — type
  `packrat` to track or stop it." (`--detach` submits and returns immediately without streaming.)
- Killing the terminal, closing SSH, logging out — none touch the running job.

**TUI** (`packrat` with no args) — the default face of the tool: the packrat logo, global stats
(total indexed assets, per-root counts), **live and recent job runs with progress**, and a menu
to launch operations. It is also where you **cancel** a running job. Because jobs live in the
daemon, the TUI is a *window* onto them — open it anytime, from any terminal, to watch or stop
work started elsewhere. (TUI appearance & function: §12; milestone: §13 M6.)

**Concurrency — one mutating operation at a time (global).** The single-worker queue is the
enforcement point: if a mutating job is running, a second submission is **rejected** with a clear
"busy" message naming the in-flight job (e.g. `scan D:\test started 12:03`). This prevents the
scan-in-one-terminal + cleanup-in-another hazard by construction — no lockfile, no crash-stale
lock (a daemon-side worker slot is in-memory and released if a job dies). Read-only queries
(`status`, `--status`, TUI stats) run anytime, concurrently. **Distinct from** the *persistent*
per-root review lock: a paused `dedup`/`cleanup` holds a `review_runs` row (DB), **not** a worker
slot — so the analyze job finishes, the queue frees for other work, and you can review in Explorer
for as long as you like; only same-root review ops are blocked meanwhile.

**Why a daemon (revised rationale).** The original reason ("keep CLIP/ffmpeg warm") is obsolete —
CLIP is opt-in and rare, ffmpeg is a per-file subprocess. The daemon now earns its place for
three concrete reasons: **(1)** jobs must outlive the launching terminal (Ctrl-C-safe scans);
**(2)** a single in-memory serialization point gives the "one mutating op at a time" guarantee
cleanly; **(3)** the TUI needs a live source of job progress/state to display. None of these is
served by a plain CLI-only design.

**Windows packaging** — v1 auto-spawns the daemon as a detached console process on first use; a
tray app / Windows Service wrapper is a later nicety.

---

## 4. Data model (SQLite)

```sql
roots(
  id, path /* unique */, name /* unique, case-insensitive; leaf name or --name */,
  kind /* library|trash */, enabled, ignore_globs, last_full_scan_at)
  -- (per-root scan interval deferred with scheduled scans → M8; not settable in v1)

assets(
  id, content_hash /* blake3, unique */, media_type /* photo|video */,
  size, width, height, duration_s, captured_at /* from EXIF/ffprobe */,
  status /* active|trashed  -- no 'missing': forgotten assets are deleted */,
  added_at, trashed_at, trash_reason)

file_instances(   -- presence = row existence; a gone file has its row deleted (no 'present' flag)
  id, asset_id, root_id, path, filename, size, mtime, last_seen_at)

phash(   asset_id, algo /* phash|pdq */, bits /* blob */ )     -- written by scan
vphash(  asset_id, frame_index, t_offset_s, phash_bits )      -- video frame hashes; by scan
embeddings( asset_id, model, vector /* float32 blob, e.g. 512d */ )  -- only if scan --embed

similarity_edges(   -- pairwise near-dups (distance ≤ T_match); written by `dedup`, NOT scan
  asset_a, asset_b, media_type, distance,
  algo /* pdq|video */, created_at )   -- unique(asset_a, asset_b)

review_runs(   -- one stateful review lifecycle (dedup OR perceptual-cleanup) per target root
  id, root_id, run_type /* dedup|cleanup-perceptual */,
  status /* pending|completed|cancelled */, created_at, confirmed_at )
  -- partial UNIQUE(root_id) WHERE status='pending'  → at most one open review run per folder
  -- (dedup, perceptual-cleanup, and in-flight merge are mutually exclusive on a root)

review_actions(   -- the persisted, crash-safe plan for a review_run
  id, run_id, folder /* will_be_deleted|grouped|perceptually_identified_trash */,
  kind /* exact|perceptual */, reason /* exact-internal|exact-external|perceptual|cleanup-perceptual */,
  default_action /* delete|keep */,
  asset_id, instance_id, path,           -- the file this action targets
  survivor_instance_id,                  -- the copy being kept (exact); NULL otherwise
  group_no, member_no, is_external,      -- dedup grouping only
  matched_trashed_asset_id, distance,    -- cleanup-perceptual only (which trashed asset, PDQ dist)
  shortcut_name )

-- tags(...) omitted for now — tagging/classification schema is TBD (§7)

jobs(    id, type, status, total, done, started_at, finished_at, error, params_json )
```

Notes
- **Two asset states only (`active`/`trashed`), presence = row existence.** When a file is found
  gone, its `file_instances` row is deleted. Then: if the asset is `active` and now has **zero**
  instances → **delete the asset and all its dependent rows** (`phash`, `vphash`, `embeddings`,
  `similarity_edges`) — we forget it entirely, because a plain filesystem delete must not be
  remembered as trash (§6). If the asset is `trashed`, it is kept at zero instances (its
  fingerprint is the trash memory). Enforce dependent-row cleanup with `ON DELETE CASCADE`.
- `assets` rows with `status='trashed'` **retain their fingerprints forever** — this is the
  trash memory used to exclude re-appearing junk. The physical file may be long gone.
- **Unreachable-root / incomplete-listing guard:** deletion-detection (removing gone instances)
  only runs for roots that were **fully and cleanly enumerated** this pass. If a root is offline
  (unplugged drive, missing share) *or* any directory listing errored/timed out mid-scan (common
  on SMB — see §10.1), its instances are left untouched — incomplete data must never be read as
  "all files deleted," which would wrongly forget a whole root's fingerprints.
- Vector search: start with a memory-mapped numpy matrix (100K × 512 float32 ≈ 200 MB;
  brute-force cosine is milliseconds). Upgrade to `hnswlib`/`sqlite-vec` only if needed.
- Perceptual candidate search: brute-force Hamming in numpy, or a BK-tree if it gets slow.

---

## 5. Fingerprints & how duplicates are decided

This section defines the fingerprints packrat stores per asset and the two separate notions of
"duplicate" built on them. It is reference material; the operations that *act* on it are §8
(`scan`, `dedup`, `merge`) and §6 (`cleanup`).

### 5.1 The fingerprints

Three fingerprints, all produced by **`scan`** (§8 A2) and stored in the DB. Computing them is
scan's job; every other operation reads them.

- **Content hash — BLAKE3 of the file bytes.** The identity key: same bytes ⇒ same asset. Cheap,
  exact, format-agnostic (works even on files that won't decode).
- **Perceptual signature — robust to recompression/resize/re-encode.**
  - Photo: **PDQ (256-bit, primary)** plus pHash as a corroborating check.
  - Video: **duration** + a sequence of frame pHashes sampled at fixed fractions of the timeline.
- **Semantic embedding — CLIP vector.** Computed **only** on an explicit `scan --embed`, stored
  for future semantic search / trash-tagging (§7). **It never participates in any duplicate
  decision** — semantic similarity is not duplicate-ness (two different receipts, or two beach
  photos, score high on CLIP yet are distinct assets you want to keep). A plain scan computes
  none, and its absence or failure changes no dedup/merge/cleanup result.

### 5.2 Two kinds of duplicate

Everything downstream rests on this distinction:

- **Exact duplicate — identical bytes** (same content hash). This is *identity*, not a judgment
  call: two files with the same hash are simply two `file_instances` of one asset. Resolved
  automatically wherever files are seen — by `scan` (attach a new instance), `merge` (skip/collapse
  on ingest), and `cleanup` (delete library copies of trashed content). Zero false positives.
- **Perceptual near-duplicate — different bytes, visually the same** (recompressed, resized,
  re-encoded, cropped…). These are **distinct assets** joined by a recorded similarity edge, never
  silently collapsed — because both files genuinely exist. Deciding what to do about them needs
  human review, so this is **only** ever surfaced by `dedup` (§8 B) and `cleanup --perceptual`
  (§6.2), which stage candidates in Explorer for the user.

Exact resolution is cheap and safe enough to run inline anywhere; perceptual matching is a
deliberate, reviewed, opt-in operation.

### 5.3 The perceptual matching engine

A single scope-agnostic matcher, run only by `dedup` and `cleanup --perceptual`. It uses the
perceptual signature alone (never CLIP), over fingerprints already in the DB — pure hash math, no
file I/O.

- **Photo:** primary signal is **PDQ Hamming distance**; pHash corroborates. PDQ at a sane
  threshold is precise on the recompress/resize/format-conversion case — essentially the entire
  iPhone-re-export reality — so one robust signal is both sufficient and higher-recall than
  gating two signals together.
- **Video:** durations within a tolerance **and** a majority of sampled frame pHashes match
  within threshold. Matching **pre-filters by duration** (bucket by length ±tolerance, compare
  only within a bucket) to avoid the naïve frame-by-frame blowup.

**Single match threshold `T_match`** (configurable and logged): a pair is a near-dup iff PDQ
distance ≤ `T_match` (video: duration + majority-frame test). One threshold is enough because
**every** perceptual match is surfaced for human review — nothing is auto-acted-on — so there is
no need for a second "auto vs. borderline" cutoff. Set `T_match` high enough to catch what
PDQ/pHash *structurally* can (recompression, resize, format conversion) plus the harder cases you
want a look at (crops, rotations, borders/watermarks, heavy re-encodes); every hit lands in the
review folder either way, so a permissive threshold just means more candidates to eyeball, never a
silent deletion. The operation (§8 B / §6.2) decides how matches are staged.

**Comparison set depends on the caller:**
- **`dedup`** compares a folder's assets against **active assets only** — trashed assets are
  excluded (its model is "collapse redundant copies, keep one survivor," which a trashed asset —
  usually zero instances, nothing to keep, opposite intended action — cannot fit).
- **`cleanup --perceptual`** compares a folder's active assets against the **trashed** set (find
  recompressed copies of things you trashed).
- **`merge`** does **not** use this engine at all — it decides purely by exact hash, including its
  trash check.

### 5.4 Cost & caching

The first full `scan` of 100K assets is a one-time multi-hour cost (video decode dominates over
SMB); it is checkpointed per file and resumes after interruption. Later scans are cheap via the
fast-path (§8 A2), which skips re-fingerprinting unchanged files. The perceptual matcher runs on
the stored signatures (a few MB in the DB) — seconds of CPU, no I/O (§ performance analysis).
Embeddings, if ever wanted, are a separate opt-in `scan --embed` pass and not part of this
baseline cost.

---

## 6. Trash model

Two distinct ways content leaves the collection — treated very differently:

1. **Deleted directly in Explorer** (not via a trash folder): next scan deletes the gone
   `file_instances` row; if no instances remain, the (active) asset is **forgotten entirely** —
   the asset and all its fingerprints are deleted. It is **not** blocklisted — if it reappears in
   a future export it will be treated as new. This matches "a plain Explorer delete does not mean
   trash," and is exactly why we keep no `missing` state: a forgotten asset leaves no trace to
   compare against.

2. **Trashed by the user via a trash folder** — the primary way to trash content: the user
   manually moves or copies the file into a **registered trash folder** (a root with
   `kind='trash'`). A registered trash folder is a transient **inbox**: the user drops junk in,
   and *refreshing the trash collection* (below) absorbs it into the permanent trashed-hash memory
   and empties the folder. Trashed fingerprints are kept **forever**, so future merges exclude
   anything matching them — this is what stops junk that still lives on the iPhone from being
   re-merged even after you emptied the trash folder.

   (Content can also become `trashed` via **dedup** — when the user discards a perceptual
   near-duplicate during a dedup run, that asset is marked `trashed` with the same
   fingerprints-kept-forever semantics; see §8 B. The trash-folder route above is the general,
   explicit path.)

**Multiple trash roots are allowed.** Any number of roots may be `kind='trash'` (e.g. one per
drive). They are all consulted together as one logical trashed set.

### 6.1 Refresh the trash collection (shared procedure)

This is the step that turns files-sitting-in-a-trash-folder into permanent trashed fingerprints.
It is invoked automatically at the start of **`cleanup`** and **`merge`** (and exposed directly
as `packrat trash refresh`). Steps:

1. For **every** registered `kind='trash'` root, enumerate its files (same allowlist/ignore rules
   as scan). For each file:
   - Compute BLAKE3 + perceptual signature (PDQ/pHash; video frame hashes). **No embedding.**
   - Resolve against `assets.content_hash`:
     - **New content** → create an asset with `status='trashed'`, `trashed_at`,
       `trash_reason='trash-folder'`, and persist its `phash`/`vphash` (so perceptual trash
       exclusion works in merge).
     - **Matches an existing `active` asset** → flip it to `status='trashed'` (the user is telling
       us this content is junk); retain its fingerprints. Its library-folder instances remain on
       disk until a `cleanup` removes them.
     - **Matches an existing `trashed` asset** → already remembered; nothing to add.
2. **Physically remove all files from every trash root** (to Recycle Bin). Their fingerprints now
   live forever in the trashed set, so the actual files are no longer needed — the folder is
   emptied, ready for the next drop.
   - **Crash-safety ordering (required):** step 1 (record the hash → DB, committed) must complete
     **before** step 2 deletes that file. Never delete first — a crash between would lose the
     trashed fingerprint. Because recording is idempotent (re-hashing the same file yields the
     same asset), a crash mid-refresh just re-processes survivors on the next run; nothing is lost.
   - **Undeletable file** (locked / permission denied): its fingerprint is already recorded
     (harmless), so leave the file in place and report it — it will be re-processed (a no-op for
     the DB) and retried for deletion next refresh. Never block the whole refresh on one stuck file.
3. Trashed assets legitimately have **zero file instances** afterward (the trash files are gone);
   this is the one case where an asset persists with no instances (§4).

> ⚠️ **Refresh always absorbs and empties — even under `--dry-run`.** This procedure is
> **never a no-op**: any file in a trash root is fingerprinted, its hash recorded to the trashed
> set forever, and the file moved to the Recycle Bin. There is **no dry-run variant of refresh** —
> callers that support `--dry-run` (`cleanup`, `merge`) skip only their *own* destructive step
> (deleting library files / copying), but refresh runs for real first. This is intentional: putting
> a file in a trash folder **is** the act of trashing it, so absorbing + emptying it is expected
> regardless of what the surrounding command does or previews. Do not use a trash folder as
> scratch space — anything left there when `refresh`/`cleanup`/`merge`/`trash refresh` runs is
> consumed. (Recoverable from the Recycle Bin if truly needed.)

**`scan` never touches trash roots** — indexing a trash folder is only ever done here (see §8 A2
validation). This keeps the "inbox that gets emptied" semantics from colliding with scan's
"index and keep" semantics.

### 6.2 `packrat cleanup <folder>` — remove trashed content from a library folder

From the user's perspective: **delete every file in `<folder>` whose content matches something
you've trashed.** Use case: a photo you trashed still lives on the iPhone and got re-pasted into
a library backup folder; `cleanup` removes those re-appearances.

Two modes:
- **Default (exact only):** one-shot. Byte-identical matches to trashed content are deleted after
  a typed count confirmation — no per-file review (exact-hash matching is false-positive-free).
- **`--perceptual`:** stateful (analyze → pause → `--confirm`). Adds *perceptual* trash matches
  (recompressed/resized copies of trashed content), staged as shortcuts for Explorer review since
  perceptual matching can misfire. Exact matches are **not** deleted inline in this mode — both
  exact and reviewed-perceptual deletions apply together at `--confirm`.

**Shared validation & lock (both modes):** `<folder>` must be a registered **library** root —
reject a `kind='trash'` root (its files are consumed by refresh, not cleaned). Take the same
per-root exclusion as dedup: reject if a `pending` dedup run, a `pending` perceptual-cleanup run,
or an in-flight merge targets this root (they may stage `.lnk`s pointing at files cleanup would
delete, leaving broken shortcuts / a stale plan); likewise dedup/merge reject a root with a
cleanup in progress. Recommend a fresh `scan <folder>` first so newly-arrived files are indexed;
cleanup operates on indexed instances.

#### Default mode — `packrat cleanup <folder>`
1. **Refresh the trash collection** (§6.1), so the trashed set is fully current.
2. In `<folder>`, find every `file_instances` row whose asset has `status='trashed'`, matched by
   **exact content hash only**.
3. **Print the count** and require typed confirmation — a sanity check, **no staging folder**.
4. On confirm, move each matched file to the **Recycle Bin** and delete its `file_instances` row.
   The asset stays `trashed` (fingerprints retained). Report deleted count.

#### Perceptual mode — `packrat cleanup <folder> --perceptual` (analyze → `--confirm`)
Analyze:
1. **Refresh the trash collection** (§6.1); open a persisted `pending` cleanup run for this root.
2. **Exact matches:** find library instances whose asset is `trashed` (exact hash), as in default
   mode — but **do not delete yet**; record them in the plan.
3. **Perceptual matches:** run the §5 matcher for `<folder>`'s active-asset instances against the
   **trashed** set (PDQ / video-frame; duration pre-filter). Each library file within `T_match`
   of a trashed asset is a perceptual-trash candidate.
4. **Stage for review** at `<root>\_packrat_review\_perceptually_identified_trash\`: one `.lnk`
   per perceptual candidate (stat-before-create, so no broken `.lnk`; §8 B Phase 4 rules), plus a
   `manifest.csv` (shortcut → target path → matched trashed asset → distance). Write a
   `proposed.json` audit record (§8.1 style).
5. **Report** the exact-match count (will delete on confirm) and perceptual-candidate count
   (staged for review), print the `--confirm` / `--cancel` commands, and **pause**.

Review convention (**delete-default**, like `_will_be_deleted` — *opposite* of dedup's grouping
folder): a staged file is treated as trash and **will be deleted**; **remove its shortcut to
spare** the file (mark it "not trash" for this run). Renames count as removal (strict, per §8 B).

`packrat cleanup <folder> --confirm`:
6. Re-verify liveness per file (lazy stat, as §8 B Phase 6). Require typed confirmation of the
   combined delete set. Then, to the **Recycle Bin**:
   - **Exact matches** → delete the `file_instances` row; asset stays `trashed`.
   - **Perceptual matches still staged** (shortcut present) → delete the file **and mark its own
     asset `status='trashed'`**, `trash_reason='cleanup-perceptual'`, fingerprints retained — so
     this near-dup won't re-appear via merge (consistent with dedup's perceptual-deletion).
   - **Perceptual matches spared** (shortcut removed) → left untouched; not trashed.
7. Delete the `_perceptually_identified_trash\` staging folder, write `applied.json`, mark the run
   `completed`. `--cancel` discards staging and deletes nothing.

**`--dry-run`** (both modes) **still refreshes-and-empties the trash collection** (the refresh
runs for real), then reports the count/list of library files that *would* be deleted (and, with
`--perceptual`, would be staged) without deleting or staging anything. This is a deliberate
exception to "dry-run changes nothing": refresh (§6.1) is a shared, idempotent procedure whose
no-op variant isn't worth building, and it is non-destructive to your *library* (it only absorbs
hashes and empties the transient trash inbox — which is what trashing already means). Dry-run's
guarantee is scoped precisely: **it never deletes from the library folder being cleaned**; it may
still empty the trash inboxes.

---

## 7. Semantic embeddings (infrastructure; tagging TBD)

This section covers **only the embedding infrastructure** — computing and storing a semantic
vector per asset. The concrete tagging/classification behavior built on top (junk detection,
the double-check-trash flow, categories, thresholds) is **not yet designed** and is deferred;
the `tags` table is intentionally omitted from the schema (§4) until then.

> **CLIP lives here and only here.** The embedding is a *semantic* signal (for future search /
> tagging), never a dedup signal (see §5). Dedup is decided entirely by content hash + perceptual
> signature.

- **Engine**: CLIP (open_clip, ViT-L/14 on the RTX). Produces a fixed-length float32 vector per
  photo; for video, per sampled frame (aggregated).
- **When computed**: only on an explicit **`scan --embed`** (or a future tagging pass) — never by
  a plain scan. Fully decoupled from dedup/merge: skipping it or having it fail changes no
  dedup/merge result. Backfillable at any time.
- **Storage**: one `embeddings(asset_id, model, vector)` row per asset (§4). Search over them
  starts as brute-force cosine on a memory-mapped numpy matrix (§4 Notes).
- **What it unlocks later (design TBD)**: semantic search ("find beach photos"); zero-shot
  junk-flagging (screenshots, receipts, documents) with an Explorer-based human review; possible
  OCR corroboration. None of this is specified yet — only the vectors are.
- Embeddings are **not** used for near-dup confirmation — semantic similarity ≠ duplicate-ness.

---

## 8. Core workflows (detailed)

This section specifies three behaviors, step by step, so the logic can be reviewed
for correctness:

- **A. Add a folder to the collection** — catalog an existing on-disk folder (`register` +
  `scan`). Pure indexing; it never moves, renames, copies, or deletes any file. Only the
  database changes. Scan writes **all per-asset fingerprint data** to the DB.
- **B. Dedup a single registered folder** — from the DB fingerprints (plus a liveness check),
  find the target folder's duplicates against the whole collection, stage removable copies as
  Explorer **shortcuts** inside the folder, and — after the user reviews and confirms — delete
  them (to Recycle Bin). One pending run per folder; pending → completed.
- **C. Merge a folder into an existing folder** — discard trash and copy into a destination only
  the files new to the *whole* collection, decided by **exact hash only** (no near-dup matching,
  no review). Read-only on the source; copy-only on the destination.

**Division of labor (important):** `scan` (A) produces *per-asset* data only — hash, metadata,
perceptual signatures. The *pairwise near-dup* matching (which asset is visually a near-dup of
which) is done **only** by `dedup` (B), over DB assets. `merge` (C) does **not** do near-dup
matching at all — it classifies incoming files purely by exact `content_hash` (dup-in-source /
trashed / exact-known / new), collapsing byte-identical duplicates but leaving recompressed
near-dups for `dedup`. The one kind of duplicate scan *does* resolve is **exact byte-identical**
files — that is identity assignment (a second `file_instance` on the same asset), enforced by
the `content_hash` unique index, not near-dup dedup.

All three rely on the **asset / file-instance** split and the identity rules below.

### Identity rules (used by all three workflows)
- **Exact identity** = BLAKE3 content hash. Files with the *same bytes* are the **same asset**
  with multiple **file instances** (e.g. the same photo living in two folders). Adding such a
  file never creates a second asset — it just adds a file-instance row pointing at the
  existing asset.
- **Near-duplicate** = *different bytes, visually the same* (recompressed / resized /
  re-encoded). These are **distinct assets** linked by a recorded **similarity edge**, never
  silently collapsed. Near-dup relationships are found and acted on **only by `dedup`** (§8 B);
  `merge` does not consider them.
- **Trashed-hash exclusion** applies during **merge** (discard incoming exact-hash trash matches)
  and **cleanup** (delete library exact-hash trash matches). Trashed assets keep their
  fingerprints forever (physical file may be gone); this is what excludes re-appearing junk. Merge
  matches trash by **exact hash only** — a recompressed copy of trashed content is caught later by
  `dedup`, not by merge.

---

### A. Add a folder to the collection — two separate operations

Adding a folder is deliberately split into two commands so a cheap bookkeeping action is never
coupled to a multi-hour fingerprinting job:

- **`register`** — record the folder as a root. Metadata-only, instantaneous, touches no files.
- **`scan`** — walk a registered root and fingerprint its contents. This is the resumable,
  long-running indexing job. **It does not compute CLIP embeddings unless `--embed` is passed**
  — dedup never needs them, so the default scan stays lean.

Both are non-destructive: files are read-only, the only writes are to the packrat database.

---

#### A1. `register` — declare a folder as a root (metadata-only)

```
packrat register "D:\Backup\iPhone"           # default kind: library
packrat register "D:\Backup\iPhone" --scan    # register, then immediately kick off a scan
```

1. Resolve the path to an absolute, long-path-safe form; require it to exist, be a directory,
   and be readable.
2. **Overlap check:** reject if the path is already a root, or is nested inside / contains an
   existing root (prevents double-indexing the same bytes under two roots).
3. **Unique-name check:** the folder's **leaf name** (the last path component, e.g. `iPhone`)
   must be globally unique across all roots, compared case-insensitively. So with
   `D:\Backup\iPhone` already registered, `D:\test\iPhone` is **rejected** even though it is a
   different path — the leaf `iPhone` collides. Rationale: the leaf name is used as the human-
   facing handle for a root, so it must be unambiguous. The error suggests either picking a
   differently-named folder or passing an explicit `--name <label>` to override the handle (the
   label, not the path, is what must be unique).
4. Insert a `roots` row: `path`, `name` (leaf name or `--name`), `kind=library`, `enabled=1`,
   `last_full_scan_at=NULL`. Bind the **ignore set** to the root (see below).
5. Report the root id/name and that it is registered but **not yet scanned** — nothing is
   walked or fingerprinted here. The root contributes nothing to dedup/merge until a `scan`
   completes. With `--scan`, immediately enqueue a `scan` job for this root (equivalent to
   running `packrat scan <path>` next) and stream its progress; `--scan --embed` also runs the
   embedding pass.

**What the ignore set is (and what "bind" means):** the ignore set is the filter that decides
which files a later `scan` will even *look at* — matched files are skipped entirely (never
hashed, fingerprinted, or turned into assets). It has two parts:
- **Junk/system exclusions** — `Thumbs.db`, `desktop.ini`, `.DS_Store`, hidden/system-attribute
  files, zero-byte files, and packrat's own staging area `_packrat_review\` (which contains
  dedup's `_will_be_deleted\` / `_grouped_by_similarity\`) plus `.lnk` shortcuts.
- **Media extension allowlist** — only these become assets. The **default** is a fixed, closed
  set (case-insensitive), defined once here and reused everywhere:
  - **Photo:** `jpg jpeg jfif png gif bmp tif tiff webp avif heic heif`
  - **Video:** `mp4 m4v mov avi mkv webm wmv flv mpg mpeg m2ts mts 3gp`

  Anything else (`.txt`, `.zip`, `.pdf`, sidecars like `.aae`, etc.) is ignored. The set lives
  in config and can be edited, but the shipped default is exactly the two lists above — no
  open-ended "…".

  **Optional RAW group (off by default):** `dng cr2 cr3 nef arw raf orf rw2 pef srw`. Enable via
  config (`allowlist.raw = true`) when you want camera RAW files catalogued. It is opt-in
  because RAW needs a separate decode path (`rawpy`) for metadata/perceptual hashing, and many
  workflows keep RAW+JPEG pairs where you may not want both indexed.

There is a **global default** ignore set from config; "bind" simply records, on the root, which
set applies (the default, optionally extended with per-root patterns via `--ignore <glob>`). It
is stored at register time so every scan of that root reuses the same rules deterministically.

Note the two mechanisms differ in form: the **allowlist** is a set of file *extensions* (what
qualifies as media at all), while **`--ignore` patterns are gitignore-style path globs** (e.g.
`**/cache/**`, `*.tmp`, `Screenshots/`), not a comma-separated extension list. A file is scanned
only if its extension is in the allowlist AND it matches none of the ignore patterns.

Registering alone leaves the collection unchanged in content terms; it just tells packrat this
folder exists and how to treat it. Follow with `scan` (or use `register --scan`).

---

#### A2. `scan` — walk a registered root and fingerprint it (the indexing job)

```
packrat scan "D:\Backup\iPhone"     # incremental; fingerprint new/changed files. No embeddings.
packrat scan --all                  # scan every enabled root
packrat scan "D:\Backup\iPhone" --embed   # also compute CLIP embeddings for tagging (§7)
```

Scan is purely per-asset: it fills in every fingerprint column for each file but computes **no
near-dup relationships** — those are the `dedup` operation's job (§B). The one exception is
exact byte-identity, which scan must resolve because it decides asset identity. Each step below
notes exactly what it writes.

**Phase 1 — Enumerate**
1. Resolve the target to a registered root (error if it isn't one — `register` it first).
   **Reject `kind='trash'` roots** — trash folders are transient inboxes indexed only by "refresh
   the trash collection" (§6.1), never by `scan` (whose "index and keep" semantics would fight the
   "index then empty" model). → reads `roots` (match `path`); no write.
2. Recursively walk the root, applying the ignore set, to build the candidate worklist.
   → no DB write (in-memory worklist).
3. Open a job row. → **write** `jobs`: `type='scan'`, `status='running'`, `total`=file count,
   `done`=0, `started_at`, `params_json`={root_id, full, embed}.

**Phase 2 — Per-file pipeline** (worker pool; checkpointed after each file)
For every candidate file:
4. **Fast-path skip (tolerant-mtime key).** If a `file_instances` row exists at this exact `path`,
   its `size` matches exactly, its `mtime` matches within a small **tolerance**
   (`fastpath.mtime_tolerance_s`, default 2 s), and its asset is fully fingerprinted → **write**
   `file_instances.last_seen_at` (now) only; skip the rest. `--full` ignores the fast-path and
   re-fingerprints unconditionally.
   - **Why exact `size` but tolerant `mtime`:** size is high-entropy for media (two different
     photos/videos almost never share a byte count), so it is the strong change signal; mtime is
     a *weaker corroborator* whose exact value is unreliable across SMB/exFAT (2 s FAT rounding,
     SMB precision differences, NAS-side tools rewriting timestamps). A real in-place edit moves
     mtime by far more than the tolerance, so it still trips re-fingerprinting; the tolerance only
     absorbs jitter, avoiding needless re-reads (expensive over the network — see §10.1).
   - **Residual blind spot (accepted):** a same-`path`, same-`size`, byte-different file whose
     mtime also lands within tolerance is skipped and its stored fingerprint goes stale. This is
     rare for media and is the reason the periodic **`--full` scan** (which re-hashes everything)
     exists as the backstop. Setting `mtime_tolerance_s=0` restores strict `path+size+mtime`.
5. **Content hash** — BLAKE3, streamed. → no write yet (value held for step 6).
6. **Exact-dup resolution.** Look up `assets.content_hash`.
   - **Hit** → this is another copy of a known asset: **write** a `file_instances` row
     (`asset_id`=existing, `root_id`, `path`, `filename`, `size`, `mtime`, `last_seen_at`) and
     **stop** (no metadata/perceptual work). If the hit asset was `trashed`, this is a re-appeared
     trashed fingerprint — see Phase 4. This is how identical bytes in two folders become one
     asset with two instances (enforced by the `content_hash` unique index).
   - **Miss** → continue; create the asset in step 9.
7. **Metadata** — decode/probe for dimensions, duration, capture time, codec (exiftool /
   ffprobe). → values held for step 9 (→ `assets.width/height/duration_s/captured_at`,
   `media_type`, `size`).
8. **Perceptual signature** — photo: PDQ (primary) + pHash; video: duration + sampled frame
   pHashes. → values held for step 9 (→ `phash` / `vphash` rows). *No near-dup comparison here.*
9. **Persist the new asset (single transaction).** → **write**:
   - `assets`: `content_hash`, `media_type`, `size`, `width`, `height`, `duration_s`,
     `captured_at`, `status='active'`, `added_at`.
   - `file_instances`: `asset_id`, `root_id`, `path`, `filename`, `size`, `mtime`,
     `last_seen_at`.
   - `phash`: one row per algo — (`asset_id`, `algo='pdq'`, `bits`), (`asset_id`,
     `algo='phash'`, `bits`).
   - `vphash` (video only): one row per sampled frame — (`asset_id`, `frame_index`,
     `t_offset_s`, `phash_bits`).
   Then **write** `jobs.done += 1` (checkpoint).

*(Near-dup linking is intentionally absent — it is the `dedup` operation, §B, which writes the
`similarity_edges` table from this data. Scan never writes similarity edges.)*

**Phase 3 — Embeddings (only if `--embed`)**
10. **By default skipped entirely — no embeddings computed, no `embeddings` rows written.** With
    `--embed`, assets with no current `embeddings` row for the active model are queued for a
    batched CLIP pass → **write** `embeddings`: (`asset_id`, `model`, `vector`). Fully decoupled:
    skipping or failing this leaves every dedup/merge result identical; backfillable later.
11. **Deletion detection (every completed scan of a reachable root — not just `--full`).**
    Reconcile files removed from disk since last scan. This needs **no re-hashing**: enumeration
    (Phase 1 step 2) walks the whole tree on *every* scan, and every present file has its
    `file_instances.last_seen_at` bumped this pass (step 4 fast-path or step 9). So gone files are
    simply the rows this scan never touched:
    - `DELETE FROM file_instances WHERE root_id=? AND last_seen_at < <this scan's start time>`
      — i.e. any instance under this root not seen this pass → **delete the row**.
    - Then for each affected asset: if it is `active` and now has **zero** instances anywhere →
      **delete the asset** (cascading `phash`/`vphash`/`embeddings`/`similarity_edges`) — it is
      forgotten, not remembered as missing (§6: a plain filesystem delete is not trash). A
      `trashed` asset at zero instances is left intact (trash memory).
    On a `--full` scan, additionally **write** `roots.last_full_scan_at`.
    (`--full` governs re-*hashing* via the fast-path bypass; it does **not** govern deletion
    detection, which keys off enumeration + `last_seen_at` and therefore runs on incremental scans
    too.)
    **Guard:** run this only if the root was fully and cleanly enumerated this pass; skip entirely
    for an offline/unreadable root or any directory whose listing errored/timed out (§10.1), so
    incomplete data is never mistaken for "files deleted."
12. Close the job → **write** `jobs.status='done'`, `finished_at` (or `status='error'`, `error`).

**Phase 4 — Trashed-fingerprint handling**
13. If a file's `content_hash` matches an asset already `status='trashed'`, step 6 attached the
    new `file_instances` row to that **trashed** asset — it does **not** flip to `active` (the
    user trashed this content; re-appearing on disk doesn't un-trash it). The file physically
    exists but the collection still treats the content as trash. → counted as `matches-trashed`
    in the report; no status change. Remove these re-appearances with **`packrat cleanup <folder>`**
    (§6.2), which deletes library files whose content is trashed.

**Phase 5 — Report**
14. Summarize: new assets, files that were exact-dups of a known asset (new instance only),
    non-media skipped, undecodable/corrupt errors, `matches-trashed` count, embeddings computed
    (`--embed`) or deferred. **No near-dup clustering here** — that is reported by `dedup`.
    **Nothing on disk changed.**

**Idempotency & resume:** re-running `scan` on the same root is a no-op except for genuinely
new/changed files (fast-path skips the rest). An interrupted job resumes from its last
`jobs.done` checkpoint. Re-running `register` on an existing root is rejected by the overlap check.

---

### B. Dedup a single registered folder

`dedup` **targets one registered folder** (root) at a time and stages its removable duplicates
as **Windows shortcuts** inside that folder, for the user to review in Explorer and then
confirm. It works from the fingerprints scan already stored in the DB (hashes, `phash`/`vphash`).
Comparison spans all **active** assets across the whole collection: an asset in the target folder
is judged against active copies in *external* registered folders too. **Trashed assets are
excluded** — dedup only collapses copies of things you're keeping; trash exclusion is `merge`/
`cleanup`'s job (§6).

**Liveness is verified lazily, not eagerly.** Stale DB rows (a file moved/deleted in Explorer
since the last scan) are rare, and `stat()`-ing every candidate up front — especially external
copies on a cold/sleeping drive — is mostly wasted work. So there is **no eager stat at the
start**; instead liveness is checked at exactly two points, each stat'ing only the files it is
about to act on:
- **At shortcut creation (Phase 4):** stat each planned target right before writing its `.lnk`,
  so **no broken shortcut is ever created** — a target that has since vanished is skipped and its
  DB row lazily cleaned. This keeps the Explorer review clean (every `.lnk` resolves + previews).
- **At delete (Phase 6, on `--confirm`):** re-stat immediately before the irreversible move, the
  authoritative gate (a file the user then deletes may have changed again since staging).

This is safe because any divergence resolves toward **sparing**: a file that is already gone is
simply not staged / not deleted; if an external survivor turns out gone, the internal copies are
spared. The pipeline can only ever act on *fewer* files than the DB preview implied, never more.
*(edge case 5)*

```
packrat dedup "D:\Backup\iPhone"            # analyze → stage shortcuts → pause (status: pending)
packrat dedup "D:\Backup\iPhone" --confirm  # apply the user's review → delete → status: completed
packrat dedup "D:\Backup\iPhone" --cancel   # discard staging, delete nothing
packrat dedup --status                      # show each root's dedup state (pending/completed)
```

Terminology: **target folder** = the root passed to `dedup`. **External folder** = any *other*
registered root. **Survivor** = the one file instance of an asset that dedup keeps.

#### Dedup state machine (one run per folder)
- A `review_runs` row tracks state per root: `pending` (staged, awaiting the user) →
  `completed` (confirmed & applied) or `cancelled`. A **partial unique index enforces at most
  one `pending` run per `root_id`** — you cannot start a second dedup on a folder that already
  has one open. `dedup <folder>` on a folder with a pending run errors and tells you to
  `--confirm` or `--cancel` it first.
- The full plan is persisted to `review_actions` at analyze time, so `--confirm` is deterministic
  and crash-safe (it never re-decides; it only reads which shortcuts the user kept/removed).

---

#### B1. `packrat dedup <folder>` — analyze & stage (produces `pending`)

**Phase 0 — Validate & lock**
1. Resolve `<folder>` to a registered root (error if not). → **read** `roots`.
2. Reject if a `pending` `review_runs` row already exists for this root. Also reject if a merge
   into this root is mid-flight (an open merge plan targeting it) — both write under this root's
   `_packrat_review\`, so they must not overlap. Otherwise **write** a `review_runs` row
   (`root_id`, `status='pending'`, `created_at`) and open a `jobs` row (`type='dedup'`).

**Phase 1 — Build from the DB (no eager stat)** *(edge case 5)*
Analyze builds the plan directly from existing `file_instances` rows; it does **not** stat files.
Two cheap, non-`stat` guards keep the preview honest without walking cold drives:
3. **Recommend a fresh `scan <folder>` first.** Scan already walks and stats the target folder as
   part of indexing, so running it beforehand makes internal liveness current for free. Dedup
   prints a note if the root's `last_full_scan_at` is old, but does not force it.
4. **Trust the DB for external instances.** External copies are not stat'd at analyze time; the
   plan assumes their `file_instances` rows are live. If an external survivor is actually gone,
   the shortcut-creation and confirm-time checks catch it and spare the internal copies — the
   worst case is a preview that offers to delete slightly more than confirm ultimately will.
   → No writes and no stats in this phase; liveness-driven DB cleanup happens lazily in Phase 4
   (at shortcut creation) and Phase 6 (at delete), as broken targets are actually encountered.

**Phase 2 — Exact-duplicate resolution** *(byte-identical = same asset)*
For each asset with ≥1 live instance **in the target folder**:
5. **Exact dup with an external folder** → the external copy is byte-identical, so **all** of the
   target folder's instances of this asset are redundant. Plan every target-folder instance for
   deletion (`kind='exact'`, `reason='exact-external'`, survivor = the external instance). Keep
   nothing locally.
6. **Else, exact dups within the target folder** (asset has ≥2 live instances, all in this root)
   → keep the instance with the **oldest `mtime`** (tiebreak: stable by path), plan the rest for
   deletion (`kind='exact'`, `reason='exact-internal'`, survivor = the kept instance).
7. **Else** (single live instance, no external copy) → it is a survivor; nothing to delete.
   → All planned deletions are **written** to `review_actions` (`folder='will_be_deleted'`,
   `default_action='delete'`, target instance/asset/path, survivor reference).

**Phase 3 — Perceptual grouping** *(near-dup = distinct assets; edge-case-6 guard)*
8. **Compute similarity edges first (the §5 matching engine).** Before grouping, run the §5 PDQ /
   video-frame matcher for the target folder's pure-survivor assets against all **active** assets
   in the collection (**trashed assets excluded** — see §5), and **upsert** the results into
   `similarity_edges` (this is the operation that populates that table — see §4/§8 division of
   labor). Pure DB + fingerprint math, no file I/O: it reads `phash`/`vphash` rows and writes
   edges. Video matching **pre-filters by duration** (bucket by length ±tolerance; only compare
   within a bucket) to avoid the naïve frame-by-frame blowup. `--reuse-edges` skips this and uses
   whatever edges already exist (faster re-runs when nothing changed since the last dedup).
9. Build clusters from `similarity_edges` (§5 near-dup relationships) among **pure-survivor
   assets only** — assets with **no** instance planned for deletion in Phase 2. An asset touched
   by exact resolution (i.e. any instance of it is in `_will_be_deleted`) is **excluded from
   grouping entirely** — even if its content survives in an external folder. It is represented in
   exactly one place: `_will_be_deleted`. Each remaining (pure-survivor) asset is represented by
   its single surviving instance (target-folder if present, else external).
   - *Example:* asset X exists internally + byte-identically external → its internal copy goes to
     `_will_be_deleted` (exact-external). A near-dup Y of X is **not** grouped with X this run,
     because X is excluded; if that leaves Y with no other cluster partner, Y is simply not staged
     this run.
   - **Deferral (consequence):** a near-dup entangled with an exact-deleted asset surfaces on a
     **later** dedup run — once the exact copy is gone and the asset is a pure survivor, it groups
     normally. So re-running `dedup <folder>` after a confirm can reveal near-dups that the first
     run deferred. This is intentional: each run does exact cleanup *or* near-dup review for a
     given asset, never both at once.
   - **Invariant (asserted, asset-level):** no **asset** appears in both `_will_be_deleted` and
     `_grouped_by_similarity` (strictly stronger than instance-level disjointness). Exact
     resolution always wins: any asset with a planned exact deletion is removed from all
     perceptual clusters. If the assertion ever fails, the run aborts with a logged error for
     investigation. *(edge case 6)*
10. For each cluster of size ≥2, assign a 4-digit `group_no`; for each member a 4-digit
    `member_no`. Plan a shortcut named `group{NNNN}_{MMMM}.lnk`, with an `_external` suffix
    (`group{NNNN}_{MMMM}_external.lnk`) when the member's live file is in an external folder.
    → **write** `review_actions` (`folder='grouped'`, `kind='perceptual'`, `default_action='keep'`,
    `group_no`, `member_no`, target instance/asset/path, `is_external`).

**Phase 4 — Materialize staging folders (shortcuts, no copies)** *(edge case 5)*
Create both dedup staging folders under the target root's **`_packrat_review\`** parent (the same
packrat-owned review area merge uses):
`<root>\_packrat_review\_will_be_deleted\` (one `.lnk` per Phase-2 deletion) and
`<root>\_packrat_review\_grouped_by_similarity\` (one `.lnk` per Phase-3 member, per the naming
above). Shortcuts (not copies) mean **no extra disk** and live thumbnail preview in Explorer.
(`_packrat_review\` is already in the ignore set, so scan never indexes it or the `.lnk`s.)
11. **Stat-before-create — never emit a broken `.lnk`.** For each planned action, `stat()` its
    real target *at the instant of creating the shortcut*:
    - **Target present** → create the `.lnk` pointing at the real file. (This is also where the
      `is_external` flag / `_external` suffix is finalized from the live path.)
    - **Target gone** → **skip the shortcut** and lazily clean the DB: **delete** the gone
      `file_instances` row; if an `active` asset now has zero instances → **delete the asset**
      (cascading fingerprints). Also drop or mark the corresponding `review_actions` row
      (`default_action='skip'`, `reason+=':target-gone'`) so `--confirm` won't look for a
      shortcut that was never made. Count as "skipped-at-staging".
    - **Survivor-gone special case (exact deletions):** if a target is present but its planned
      **survivor** (internal or external) has vanished, do **not** stage the target for deletion —
      **promote it to survivor** (redirect the asset's other exact deletions at it) and skip its
      `_will_be_deleted` shortcut; lazily delete the vanished survivor's `file_instances` row.
      Same sparing/promotion logic as the Phase-6 gate (step 19b), applied early so no
      soon-to-be-spared file gets a shortcut.
    Net result: **every `.lnk` that ends up in either folder resolves to a real file** and previews
    correctly. The single-target stat here is cheap and only touches files we're actually staging.
12. Write a **`manifest.csv`** in each staging folder — a flat, human- and machine-readable
    export of this run's `review_actions` for that folder, so the opaque `.lnk`s are legible in
    Explorer/Excel (each `.lnk` hides its target path in a binary blob). It is a *documentation*
    sidecar: `--confirm` reads shortcut presence, **not** the manifest (see strict semantics
    below). Columns:
    - `_will_be_deleted\manifest.csv`:
      `shortcut, target_path, asset_id, reason /* exact-internal|exact-external */, survivor_path`
    - `_grouped_by_similarity\manifest.csv`:
      `shortcut, target_path, asset_id, group_no, member_no, is_external, distance`
    Example (`_will_be_deleted`):
    ```csv
    shortcut,target_path,asset_id,reason,survivor_path
    001.lnk,D:\Backup\iPhone\2019\IMG_4471.jpg,8842,exact-internal,D:\Backup\iPhone\2021\IMG_4471.jpg
    002.lnk,D:\Backup\iPhone\photo.png,9105,exact-external,E:\Photos\photo.png
    ```
    Its main value is Explorer-readability — especially seeing when a flagged file (or, for
    perceptual groups, an `_external` member) lives in *another* root. It duplicates data that
    also lives in `review_actions`; that redundancy is intentional (visible without the DB).
13. **Audit trail (capture point 1 — the proposed plan).** Write an immutable
    `proposed.json` into this run's audit directory (see §8.1). It records the full plan *as
    calculated* — every action with its target path, reason, survivor, group/member, distance, and
    the counts of skipped-at-staging/spared files — plus the threshold and config in effect.
    This is the durable "what dedup proposed" record; unlike the in-folder `manifest.csv` (which
    is deleted at finalize), it lives outside the folder and is never modified.
14. Open both folders in Explorer (or their `_packrat_review\` parent), print the
    `--confirm` / `--cancel` commands, and **pause** (`review_runs.status` stays `pending`). Being
    under `_packrat_review\`, they are already ignored by scan. If staging skipped everything
    (all targets gone), the
    run auto-completes with an "already clean" report instead of pausing.

**The two review conventions are OPPOSITE — read carefully:**
| Folder | Default if you do nothing | To change a file's fate |
|---|---|---|
| `_will_be_deleted\` | the real file **is deleted** | **remove** its shortcut to **spare** the file |
| `_grouped_by_similarity\` | the real file **is kept** | **remove** its shortcut to **delete** the file |

Rationale: exact dups are objectively redundant (default-delete, veto to keep); near-dups need
human judgment (default-keep, remove to delete). The `--confirm` step prints an explicit
per-folder summary and requires typed confirmation, so the inversion can't cause a silent
mistake.

**Reviewing = deleting shortcuts, not renaming them.** Matching is strict on the planned
filename, so a *renamed* shortcut counts as removed (see Phase 5). In `_grouped_by_similarity`
that means an accidental rename would delete the target — the typed `--confirm` summary lists
every such file (per root) precisely so this can't happen silently.

---

#### B2. `packrat dedup <folder> --confirm` — apply the review (→ `completed`)

**Phase 5 — Read the user's edits**
15. Load the `pending` run and its `review_actions`. If there is **no pending run** for this root
    → error ("nothing to confirm; run `dedup <folder>` first"); same for `--cancel`. If a run is
    already `completed`/`cancelled`, it is terminal — re-running `--confirm` is a no-op error.
16. **Safety guard:** if an entire staging folder is *missing* (user deleted the whole folder),
    **abort** — do not interpret "folder gone" as "delete all" (which for `_grouped` would be
    mass data loss). Require the folders to exist to be read.
17. For each planned action, check whether a file with **its exact planned shortcut name** still
    exists in the folder (strict, filename-only match — the manifest is not consulted here):
    - `_will_be_deleted`: named shortcut **present** → intend delete; **absent** → spare (vetoed).
    - `_grouped_by_similarity`: named shortcut **absent** → intend delete the target; **present** → keep.
    **Strict rename semantics:** a shortcut that has been *renamed* no longer matches its planned
    name, so it counts exactly as **removed/absent** — there is no attempt to recover the original
    action from a renamed file via the manifest. Consequence, made explicit to the user in the
    `--confirm` summary and the folder conventions: in `_will_be_deleted` a rename **spares** the
    file (safe); in `_grouped_by_similarity` a rename **deletes** the target (as if you'd removed
    it). So the intended review gesture in both folders is **delete the shortcut**, not rename it;
    renaming is treated as removal. Extra files the user drops in are ignored (only planned
    shortcut names are consulted). This yields the *intended* delete set; liveness is applied
    per-file in Phase 6.

**Phase 6 — Authoritative liveness + apply deletions** (backup DB first) *(edge case 5)*
The **authoritative** liveness gate (Phase 4 already stat'd once at staging, but a file may have
changed again in the interim). Done lazily — one target at a time, right before the irreversible
move — so cold external drives are touched only for files actually being deleted.
18. Print a summary grouped by target root — **including any external-folder files** a perceptual
    shortcut removal would delete — and require typed confirmation.
19. For each file in the intended delete set, at the moment of deletion:
    a. **`stat()` the target file.**
       - **Gone already** → nothing to delete. Lazily clean the DB: **delete** the gone
         `file_instances` row; if an `active` asset now has zero instances → **delete the asset**
         (cascading fingerprints) — forgotten, not remembered. Count as "already-gone". Continue.
       - **Present** → proceed.
    b. **For every exact deletion (internal *and* external), verify the survivor is still live**
       before deleting — this guarantees an asset never loses its last copy:
       - `stat()` the action's `survivor_instance_id` path.
       - **Survivor live** → proceed to delete (the target is genuinely redundant).
       - **Survivor gone** → the target is no longer redundant. **Spare it** (do not delete), and
         **promote it to survivor**: update the asset's remaining planned exact deletions to point
         at this now-surviving instance. Lazily delete the vanished survivor's `file_instances`
         row. Log "spared: survivor vanished (promoted)". Because a spared file becomes the new
         survivor, the asset's *other* redundant copies (if any) still delete correctly against it
         — exactly one instance is always kept. (Covers the internal case where the user deleted
         the kept oldest-mtime copy in Explorer after staging, and the external case where the
         external copy vanished.)
    c. Move the (still-present, still-redundant) file to the **Recycle Bin** (recoverable), then
       update the DB:
       - **Exact deletions** → delete that redundant `file_instances` row. The asset keeps its
         survivor instance, so it **stays `active`** — never trashed (we still have the content).
         No re-appearance concern.
       - **Perceptual deletions** → the user deliberately discarded a near-dup. Delete its
         `file_instances` row; if the asset now has zero instances → **write**
         `assets.status='trashed'`, `trashed_at`, `trash_reason='dedup-perceptual'`, and **retain
         its fingerprints** (this is the one path where an asset survives at zero instances) so a
         future merge/dedup excludes this near-dup from re-appearing (§6 trash memory). *(This is
         a deliberate choice: a confirmed perceptual deletion is remembered as trash so it won't
         re-enter via merge — unlike a plain filesystem delete, which is forgotten.)*

**Phase 7 — Finalize**
20. **Audit trail (capture point 2 — the applied outcome).** *Before* deleting the staging
    folders, write an immutable `applied.json` into the run's audit directory (§8.1): the final
    disposition of every action — `deleted` / `spared` (vetoed) / `kept` / `already-gone` /
    `survivor-vanished`, with each file's path, root, asset_id, and its Recycle-Bin destination
    for deleted items — plus totals and `confirmed_at`. Paired with `proposed.json`, this gives a
    complete before/after record of the run. (`--cancel` writes `applied.json` with every action
    marked `cancelled`.)
21. Delete the `_packrat_review\_will_be_deleted\` and `_packrat_review\_grouped_by_similarity\`
    folders (shortcuts + manifests); leave the shared `_packrat_review\` parent in place.
22. → **write** `review_runs.status='completed'`, `confirmed_at`; close the `jobs` row. Report:
    exact deleted, perceptual deleted, spared/kept (vetoes), external files deleted, plus the
    lazily-cleaned stale rows — **skipped-at-staging** (Phase 4), **already-gone** and
    **survivor-vanished spares** (Phase 6) — and the audit path. `--cancel` instead deletes the
    staging folders, marks the run `cancelled`, and deletes nothing (but still writes
    `applied.json` as above).

**Cross-folder note:** a perceptual group member can still live in an external folder
(`_external` shortcut) — e.g. a pure-survivor asset that physically resides only in another root
but is a near-dup of something in the target folder. Removing that shortcut deletes a file in
*another* root. (Note this is **not** produced by exact-external resolution anymore — those
assets are excluded from grouping per Phase 3.) This cross-root reach is powerful and intended,
but the Phase 6 typed-confirm summary (step 18) calls it out per-root so it is never accidental.

**Why dedup is DB-first with lazy liveness:** dedup compares collection assets that are all in the
DB, so the *decision* work is pure DB comparison — no eager whole-pool stat. It stats a file only
twice, and only for files it is about to touch: once when creating that file's shortcut (Phase 4,
to guarantee no broken `.lnk`) and once immediately before deleting it (Phase 6, the authoritative
gate). **Merge (§C)** is unrelated to this near-dup machinery: it hashes the transient source
files and classifies them by exact hash against the DB — no perceptual signatures, no
`similarity_edges`, no shortcuts.

#### 8.1 Review-run audit trail (dedup & perceptual-cleanup)

Every stateful review run — `dedup` **and** `cleanup --perceptual` — leaves a permanent,
append-only record outside the collection, so you can always answer "what did it propose, and
what did it actually delete" long after the staging folders (and their `manifest.csv`s) are gone.
Deleting a whole registered folder never erases this history.

**Location:** one directory per run under
`%APPDATA%\packrat\audit\{run_type}\{root_name}\{run_id}\` (`run_type` ∈ `dedup`,
`cleanup-perceptual`), containing:
- **`proposed.json`** — written at Phase 4 (capture point 1): the complete calculated plan
  before any user review — every action (target path, root, asset_id, kind/reason, survivor,
  group/member, distance, `is_external`), the counts of skipped-at-staging/spared items, and the
  active threshold/config. Immutable once written.
- **`applied.json`** — written at Phase 7 (capture point 2): the final disposition of each
  action (`deleted` / `spared` / `kept` / `already-gone` / `survivor-vanished` / `cancelled`),
  with Recycle-Bin destinations for deleted files, totals, and `confirmed_at`. Written even on
  `--cancel`.

**Properties:**
- **Immutable & additive:** files are written once, never edited; a re-run of dedup on the same
  root gets a *new* `run_id` directory. This mirrors `review_runs`/`review_actions` in the DB, but
  survives DB loss/rebuild and is trivially greppable.
- **JSON (not CSV):** richer/nested and stable for tooling; the in-folder `manifest.csv` stays
  CSV for Explorer/Excel legibility. Different audiences, different formats.
- **Retention:** kept indefinitely by default (small text files); a future `packrat config` knob
  can prune audits older than N days. Flagged in §14.
- These files are **records, not inputs** — `--confirm` never reads them to make decisions (it
  reads shortcut presence + the DB plan); they exist purely for audit/forensics.

---

### C. Merge a folder into an existing folder

The headline use case: export the whole iPhone to a temp folder, then copy only the
genuinely-new items into the backup folder.

**Merge is deliberately simple: `merge = discard trash + copy what's new`, decided entirely by
exact content hash.** No perceptual/near-dup matching, no CLIP, no review folder, no interactive
pause. It does collapse **byte-identical** duplicates (within the source and against the
collection), but *recompressed* near-dup cleanup is a separate concern handled by `dedup` (§8 B)
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
   so that copied files automatically become catalogued members of the collection. If `dest`
   is under no library root → error (offer to `register` it first). Reject if `source` and
   `dest` overlap.
3. **Refresh the trash collection** (§6.1) — absorb any files sitting in the registered trash
   roots into the trashed-hash set and empty those folders. Merge discards incoming files that
   match a trashed hash, so the trashed set must be current first. (Runs for real even under
   `--dry-run` — see below.)
4. Opportunistically fast-path-scan the `dest` root so the comparison set is current; warn if
   the collection index is stale.
5. Open a `jobs` row (`type='merge'`) and a **persisted plan** listing every source file and its
   classification, so an interrupted copy can resume without re-hashing. This plan is internal
   crash-safety only — merge does not pause for the user.

**Phase 1 — Fingerprint source** (read-only, **no DB writes**)
6. Enumerate source media files (same allowlist/ignore rules as scan).
7. For each: **BLAKE3 + metadata only** — no perceptual signature, no embedding (merge decides by
   exact hash alone). **Nothing is written to the DB in this phase** — source files are not
   collection members; their hashes live in the merge *plan* only. DB rows (`assets`,
   `file_instances`, `phash`/`vphash`) are written solely for files actually copied, in Phase 3.

**Phase 2 — Classify each source file by exact hash**
8. **Collapse exact-within-source duplicates first.** Group source files by `content_hash`; for
   any hash appearing more than once, keep a single **representative** (tiebreak: **oldest
   `mtime`**, then stable by path) and mark the rest `dup-in-source` → not copied. This is cheap
   (the hashes are already computed in Phase 1) and prevents merge from copying two byte-identical
   files into the destination as redundant instances of one asset.
9. Classify each **representative** by exact `content_hash` against the DB — no perceptual
   comparison:

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
      atomic rename into place. (Guarantees no partial/corrupt files.)
11. **Register** each copied file → **write** `assets` (`status='active'`, hash + metadata from
    Phase 1) and `file_instances` (pointing at the copied `dest` path). Perceptual signatures are
    **not** computed here; a later `scan`/`dedup` of `dest` fills in `phash`/`vphash` (and
    `scan --embed` the embedding). It is now a collection member, so a future merge recognizes it.

**Phase 4 — Report**
12. Copied: `new` N. Skipped: `exact-known` X, `trashed` Z, `dup-in-source` W. Collisions renamed
    R. Errors E. **Source unchanged.** Suggest running `scan <dest>` then `dedup <dest>` to
    fingerprint the new files and clean up any recompressed near-dups merge let through.

**Safety & resume:**
- A DB backup is taken before the Phase 3 copy.
- Crash mid-copy → per-file atomic rename means no partial files; re-running the same `merge`
  resumes from the persisted plan and copies only the remainder.
- `--dry-run` runs Phases 1–2 and prints the classification counts / would-copy list without
  copying or writing asset rows. **But Phase 0's "refresh the trash collection" still runs for
  real** — trash folders are absorbed and emptied even in dry-run (§6.1); only the copy is skipped.
- Merge is copy-only (non-destructive), so it proceeds without a typed confirmation; use
  `--dry-run` first to preview.

**Live Photos:** a paired `.HEIC` + `.MOV` is judged per file by hash. If you previously merged
one half, only the other half is `new` and copies — no special pairing logic in v1 (a
`--keep-pairs` option is a possible later addition; see §14 #2).

---

## 9. Tech stack

| Concern            | Choice |
|--------------------|--------|
| Language           | Python 3.11+ |
| Packaging / deps   | **uv** (project + venv + lockfile; `uv run` / `uv sync`) |
| Daemon API         | FastAPI + uvicorn (127.0.0.1 + token); single-worker job queue |
| CLI                | Typer (thin client: submit job, stream progress, Ctrl-C detaches) |
| TUI                | Textual (`packrat` no-args: logo + stats + live jobs + menu; later milestone) |
| DB                 | SQLite (WAL); SQLAlchemy Core or light SQL layer |
| Vector search      | numpy brute-force → hnswlib / sqlite-vec if needed |
| Content hash       | blake3 |
| Perceptual hash    | imagehash (pHash) + pdqhash |
| Image decode       | Pillow + **pillow-heif** (HEIC/AVIF), OpenCV where handy; **rawpy** for the opt-in RAW group |
| Video              | ffmpeg / **PyAV** (frame sampling), ffprobe (metadata) |
| Metadata           | exiftool via pyexiftool |
| Embeddings (opt-in) | torch (CUDA) + open_clip — only on `scan --embed` (§7); OCR (PaddleOCR/Tesseract) is speculative/TBD |
| Scheduling         | APScheduler (in daemon) |
| Job cancellation   | cooperative — jobs poll a cancel flag at their existing checkpoints |
| Locking            | in-daemon single-worker queue (mutating ops); `review_runs` row (per-root review) |
| Optional watch     | watchdog (real-time; not required for v1) |

**iPhone specifics called out**: photos are often **HEIC** and videos **HEVC/H.265** — HEIC
decode via `pillow-heif`, HEVC via ffmpeg. Handle Live Photos (paired .HEIC + .MOV) as two
assets. Handle long paths, Unicode, and Explorer "skip duplicates" semantics ourselves.

### 9.1 Format coverage — "decode is the gate"

**Principle that makes this tractable:** only *decode* is format-sensitive. Everything else in
the pipeline operates on the decode output, not the file format:
- **Content hash (BLAKE3)** hashes raw bytes → format-agnostic; works on every format above,
  including files we can't decode or don't recognize.
- **Perceptual hash** — `imagehash`/pHash takes a PIL image; `pdqhash`/PDQ takes an RGB numpy
  array. Both are format-agnostic *given a decoded image*.
- **CLIP embedding** takes a decoded RGB frame; it never sees the container/codec.
- **Metadata** (`exiftool`) is an independent reader with the widest format support in the stack.

So the only thing to verify is: **every photo format decodes to one RGB still, and every video
format decodes to sampled RGB frames.** Everything downstream then follows automatically.

| Format group | Decode path | Bytes hash | Perceptual | Embedding | Metadata |
|---|---|---|---|---|---|
| jpg jpeg jfif png gif bmp tif tiff webp | Pillow (native; libwebp bundled) | ✅ | ✅ | ✅ | ✅ |
| heic heif | `pillow-heif` (libheif) | ✅ | ✅ | ✅ | ✅ |
| avif | Pillow ≥11.3 native, else `pillow-heif` | ✅ | ✅ | ✅ | ✅ | ⚠ POC |
| RAW: dng cr2 cr3 nef arw raf orf rw2 pef srw | `rawpy` (LibRaw ≥0.20 for cr3) → embedded preview or postprocess | ✅ | ✅ | ✅ | ✅ | ⚠ POC |
| mp4 m4v mov avi mkv webm wmv flv mpg mpeg m2ts mts 3gp | PyAV/ffmpeg (H.264/HEVC/VP9/AV1/MPEG-2/VC-1…) → sampled frames | ✅ | ✅ | ✅ | ✅ (ffprobe) |

**Decode-stage notes:**
- **Perceptual + embedding both gate on decode.** There is no separate per-format work for
  hashing or CLIP — if a frame decodes, PDQ/pHash and CLIP just run on the pixel array. This is
  why the matrix's last three columns mirror the decode column.
- **AVIF (⚠):** covered either by recent Pillow (native `AvifImagePlugin`, ~11.3+) or by
  `pillow-heif`'s AVIF opener. Both rely on the AV1 decoder being present in the bundled
  libheif/Pillow wheel — confirm on the Windows wheel with a real `.avif` in the smoke test.
- **RAW (⚠, opt-in):** LibRaw covers all listed extensions (cr3 since 0.20). **Decision:** for
  dedup we hash the RAW's **embedded JPEG preview** (fast, consistent, matches what a viewer
  shows) rather than a full demosaic (slow, and render params drift). Full postprocess is a
  fallback when no preview is embedded. Same preview feeds CLIP.
- **Animated GIF / multi-page TIFF:** decode the **first frame** for the perceptual hash and
  embedding (still treated as one asset).
- **Video codecs:** ffmpeg (via PyAV) decodes every codec these containers realistically carry
  (H.264, HEVC, VP8/9, AV1, MPEG-2/4, VC-1/WMV3). `m2ts`/`mts` are AVCHD/MPEG-TS. The only real
  risk is an exotic/ancient codec, which is negligible for a personal collection.
- **Graceful failure is mandatory:** a file whose bytes hash fine but *won't decode* is still
  recorded as an asset (identity is the hash) but flagged `undecodable` — no perceptual sig, no
  embedding, no near-dup matching for it. Scan never crashes on a bad file; it logs and moves on.
- **Windows install:** `Pillow`, `pillow-heif`, `PyAV` (bundles ffmpeg), `rawpy`, `blake3`, and
  `pyexiftool` all ship prebuilt Windows wheels — no compiler needed. `pdqhash` is a C++ binding
  and may need a wheel-availability check (⚠ POC); fall back to a pure-Python PDQ or the
  reference build if no wheel exists for the target Python version.

**Smoke test (do this before M1 in earnest):** assemble one real sample of *every* extension in
the allowlist (plus the RAW group) and run the decode→hash→perceptual→embed path over all of
them. This is the only check that truly "makes sure" — a doc/version claim can't guarantee a
given Windows wheel decodes *your* camera's CR3 or *that* AVIF encoder's output. The ⚠ cells
above are exactly what this test resolves.

---

## 10. Performance & safety

**Performance (100K+)**
- First full scan: hours (video decode bound); checkpointed & resumable. Embeddings excluded
  unless `--embed`.
- Incremental scans: seconds–minutes via the tolerant `path`+`size`+near-`mtime` fast-path.
- CLIP batched on the RTX handles thousands of images/sec; video is the cost center — sample
  few frames, cache aggressively.
- All fingerprints cached; nothing is recomputed unless the file changed (per the fast-path key).

**Safety**
- Merge never writes to or deletes from the temp source; copies are hash-verified after write;
  destination name collisions get a numeric suffix.
- Destructive ops support `--dry-run` and require confirmation; deletes prefer Recycle Bin.
- DB is the crown jewel: WAL mode, periodic `VACUUM`/integrity check, and an automatic
  backup of the DB before every merge/trash-commit.

---

## 10.1 SMB / NAS performance (most roots on a Synology NAS)

Most registered folders live on SMB shares served by a Synology NAS, so packrat must be tuned
for SMB's cost model, which differs sharply from local NTFS:

- **Metadata is latency-bound.** A bare per-file `stat()` round-trip is ~0.3–2 ms on a LAN
  (vs. microseconds locally). Individually trivial, but ×100K done serially = minutes of pure
  waiting.
- **File *data* is bandwidth-bound.** Reading bytes runs at link speed — gigabit ≈ 110 MB/s,
  2.5GbE ≈ 280 MB/s.

Mapping this onto packrat's operations:

| Operation | Dominant SMB cost | Verdict |
|---|---|---|
| `register` | none | trivial |
| **First full `scan`** | transferring **every byte** to BLAKE3 + decode | the real cost; hours, bandwidth-bound |
| Incremental `scan` | directory enumeration (size+mtime) | seconds–minutes *if enumerated, not per-file stat'd* |
| dedup Phase 4/6 stats | a few hundred/thousand round-trips, deferred + concurrent | sub-second to seconds |

**Rules the implementation must follow:**

1. **Enumerate directories; never per-file `stat` for the fast-path.** Use `os.scandir()` /
   `FindFirstFile`/`FindNextFile`, whose SMB2 *query-directory* response returns name + size +
   mtime **in one batched round-trip per directory** (Python's `DirEntry` caches these on
   Windows). An incremental scan that changes nothing then costs ~one enumeration per directory,
   not 100K stats. This is the single most important SMB detail — and it is exactly why the
   fast-path key is `path`+`size`+near-`mtime` (all available from enumeration, no extra I/O).
2. **Parallelize the byte-bound work.** SMB services concurrent requests happily, so multiple
   hashing/decoding streams hide latency and saturate the link. Cap concurrency
   (`smb.scan_workers`, default e.g. 4–8) so the NAS/array isn't thrashed.
3. **Keep the connection warm.** The daemon holds the share mounted; never remount per file.
   Expect the *first* access after HDD spin-down to pay a one-time array wake (seconds).
4. **Lean on incrementals.** Only the first full scan pays the byte-transfer cost; afterward only
   new/changed files are hashed. This is why the tolerant-mtime fast-path matters — it prevents
   spurious re-reads (each wrongly-invalidated file is a full byte transfer over the wire).

**SMB-specific correctness hardening (matters more than raw speed):**

- **Enumeration errors must never be read as deletions.** A NAS blip, timeout, or partial
  listing mid-scan could make files *look* absent → deletion-detection would wrongly forget
  fingerprints (§4). Rule: **any enumeration error/timeout for a directory aborts
  deletion-detection for that root** (fail-safe — never delete-and-forget on incomplete data).
  This extends the §4 unreachable-root guard from "root fully offline" to "any incomplete listing."
- **mtime stability.** The fast-path already tolerates small mtime jitter (§8 A2 step 4). A
  NAS-side reindex or an rsync that rewrites timestamps by more than the tolerance will force
  re-fingerprinting of those files — correct but costly; note it if you run such tools.

---

## 11. CLI surface (the core commands)

Adding a folder is two commands (`register` then `scan`); `dedup` de-duplicates one folder via
Explorer shortcuts (analyze → `--confirm`); `merge` copies new files in (exact-hash, one shot);
trash is handled by `cleanup` and `trash refresh` (§6).

**Shared client semantics** (all job-submitting commands — `scan`, `dedup`, `merge`, `cleanup`,
`trash refresh`, `scan --embed`): each **submits a job to the daemon** and streams its progress.
- **Ctrl-C detaches the view; the job keeps running in the daemon.** Re-attach or stop it via the
  `packrat` TUI, or from another terminal.
- **`--detach`** submits the job and returns immediately without streaming.
- If a mutating job is already running, submission is **rejected** ("busy: `<job>` started `<time>`")
  — one mutating operation at a time, globally (§3). Read-only commands are never blocked.
- `packrat` with **no arguments** opens the TUI (logo, stats, live/recent jobs, operation menu).

### `packrat register`
Declare an on-disk folder as a root. Metadata-only and instantaneous — walks nothing,
fingerprints nothing. The root contributes to dedup/merge only after a `scan`. The folder's
leaf name must be globally unique across roots (case-insensitive); override with `--name`.

```
packrat register <path> [options]

Arguments
  <path>                 Folder to register as a root (absolute or relative).

Options
  --scan                 After registering, immediately enqueue and run a scan of this root.
  --embed                With --scan, also run the CLIP embedding pass (implies --scan).
  --name <label>         Root handle; must be globally unique. Defaults to the folder's leaf
                         name. Use this to resolve a leaf-name collision without renaming.
  --kind library|trash   Root kind (default: library).
  --ignore <glob>        Extra ignore pattern for this root (repeatable), added to the global
                         set. A gitignore-style path glob, NOT a comma-separated extension list.
                         Matched relative to the root, case-insensitive, `/` as separator.
                         Wildcards: `*` (within a segment), `**` (across segments), `?`, `[abc]`.
                         A trailing `/` matches directories only. Examples:
                           --ignore "*.tmp"            skip all .tmp files
                           --ignore "**/cache/**"      skip anything under any cache folder
                           --ignore "Screenshots/"     skip that top-level dir
                           --ignore "IMG_*.AAE"        skip iPhone edit sidecars
                         Pass the flag multiple times for multiple patterns:
                           --ignore "*.tmp" --ignore "**/thumbs/**"
  --json                 Machine-readable result.

Errors: path missing/unreadable, overlaps an existing root, or leaf name (or --name) already
in use.

Exit: prints the new root id/name and that it is registered but not yet scanned (or streams
scan progress with --scan).
```

### `packrat scan`
Walk a registered root and fingerprint new/changed files. The resumable indexing job.
Non-destructive — reads files, writes only the database. **Computes no CLIP embeddings unless
`--embed` is given** (dedup never needs them).

```
packrat scan [<path>] [options]

Arguments
  <path>                 A registered root to scan. Omit with --all to scan every root.

Options
  --all                  Scan every enabled root.
  --full                 Ignore the fast-path; re-fingerprint every file (integrity pass);
                         stamps last_full_scan_at on completion.
  --embed                Also compute CLIP embeddings for tagging/search (§7). Off by default.
                         Only affects trash tagging and semantic search; dedup is identical
                         either way. Backfillable later via `scan --embed` or the tagging pass.
  --dry-run              Enumerate and report what would be indexed; write nothing.
  --json                 Machine-readable report.

Exit: prints the report (new assets, exact-dup instances, skipped non-media, errors,
matches-trashed, embeddings computed vs deferred). Near-dup clustering is `dedup`'s job, not
scan's. Resumable if interrupted.
```

### `packrat dedup`
Dedup **one registered folder**. Analyze stages removable duplicates as Explorer shortcuts under
`<root>\_packrat_review\` (`_will_be_deleted\`, `_grouped_by_similarity\`) and pauses in
`pending`; `--confirm` applies the user's review and deletes (to Recycle Bin). Compares the
target folder against all **active** assets across the collection (internal + external roots;
trashed excluded). At most one `pending` run per folder.

```
packrat dedup <folder>              # analyze → stage shortcuts → pending
packrat dedup <folder> --confirm    # apply review, delete confirmed dups → completed
packrat dedup <folder> --cancel     # discard staging, delete nothing → cancelled
packrat dedup --status              # per-root dedup state (pending/completed/cancelled)

Arguments
  <folder>               A registered root to dedup (path or --name handle).

Options
  --confirm              Apply the pending run for <folder>: read which shortcuts remain, delete
                         accordingly (typed confirmation; DB backup first).
  --cancel               Discard the pending run's staging folders; delete nothing.
  --status               List each root's current dedup state; no analysis.
  --dry-run              Analyze and print the plan (counts, would-stage list) without creating
                         staging folders or shortcuts.
  --json                 Machine-readable plan/report.

Conventions (OPPOSITE per folder): in `_will_be_deleted\`, remove a shortcut to SPARE the file
(default = delete); in `_grouped_by_similarity\`, remove a shortcut to DELETE the file
(default = keep). Exact dups: keep oldest-mtime internally / drop all when an external copy
exists. Perceptual near-dups: grouped for manual review.
```

### `packrat merge`
Copy into a destination folder only the files that are new to the whole collection (by exact
hash), discarding any that match a trashed hash. Source is read-only; destination is copy-only.
No near-dup detection and no interactive review — that is `dedup`'s job, run afterward.

```
packrat merge <source> --into <dest> [options]

Arguments
  <source>               Transient temp folder to merge from (never modified).

Options
  --into <dest>          Destination folder; must resolve inside a library root. Required.
  --dry-run              Print classification counts / would-copy list; copy nothing, write no
                         asset rows. NOTE: still refreshes-and-empties the trash collection
                         (§6.1) — that step always runs.
  --json                 Machine-readable report.

Flow: refresh trash collection (§6.1) → classify each source file by exact hash into
dup-in-source / trashed / exact-known / new → copy the `new` files (verified per file), mirroring
the source's folder structure under <dest>, and register them as assets. One shot; resumable from
its plan on crash. Source is left untouched. Follow with `scan <dest>` + `dedup <dest>` to
fingerprint the new files and clean recompressed near-dups.
```

### `packrat cleanup`
Remove from a library folder every file whose content matches something you've **trashed**.
Default: **exact hash**, one-shot (refresh → count-confirm → delete to Recycle Bin; no staging).
`--perceptual`: also catch *recompressed* trash copies, staged for Explorer review (stateful:
analyze → `--confirm`). See §6.2.

```
packrat cleanup <folder> [options]          # default: exact only, one-shot
packrat cleanup <folder> --perceptual       # analyze: delete-nothing-yet, stage perceptual → pending
packrat cleanup <folder> --confirm          # apply exact + reviewed perceptual deletions
packrat cleanup <folder> --cancel           # discard the pending perceptual run; delete nothing

Arguments
  <folder>               A registered library root to clean (a trash root is rejected).

Options
  --perceptual           Also match recompressed/resized copies of trashed content (§5 matcher,
                         active-vs-trashed). Stages them at
                         <root>\_packrat_review\_perceptually_identified_trash\ for review, and
                         defers ALL deletions (exact + perceptual) to --confirm.
  --confirm              Apply a pending --perceptual run: delete exact matches + still-staged
                         perceptual matches (typed confirmation; DB backup first). Confirmed
                         perceptual deletions mark their asset `trashed`.
  --cancel               Discard the pending --perceptual run's staging; delete nothing.
  --dry-run              Report the count/list that would be deleted (and, with --perceptual,
                         staged) without deleting or staging. NOTE: still refreshes-and-empties
                         the trash collection (§6.1) — that step always runs (see §6.2).
  --json                 Machine-readable report.

Review convention (--perceptual, delete-default): a staged shortcut = "will delete"; remove it to
spare the file. Opposite of dedup's `_grouped_by_similarity` (keep-default).
```

### `packrat trash refresh`
Absorb whatever is sitting in the registered trash folders into the permanent trashed-hash set,
then empty those folders (to Recycle Bin). Runs automatically inside `cleanup` and `merge`;
exposed standalone for when you've just dropped junk into a trash folder (§6.1).

```
packrat trash refresh [--json]

Options
  --json                 Machine-readable report of what was absorbed/emptied.

**No `--dry-run`.** Unlike `cleanup`/`merge` (whose `--dry-run` skips *their own* destructive
step while refresh still runs), `trash refresh` *is* the refresh procedure — there is nothing
left to skip. Per §6.1 refresh is never a no-op: a "dry" refresh would either contradict that
rule or be a `--dry-run` that isn't dry, so the flag is deliberately omitted. To see what is in
the trash folders without consuming them, browse them in Explorer before running this. (A true
preview-then-absorb mode would need the real refresh run inside a DB transaction and rolled back —
rejected for v1 as needless complexity, since refresh is non-destructive to the library.)

Flow: for every kind=trash root → fingerprint files (hash + perceptual, no embed) → record/flip
assets to `trashed` → delete the files (Recycle Bin). Reports new trashed fingerprints added and
files emptied.
```

---

## 12. TUI (`packrat` with no arguments)

Typing `packrat` alone opens a full-screen terminal UI (Textual). It is the **default face** of
the tool and, because jobs live in the daemon, a **live window onto work started from any
terminal** — open it anytime to watch progress or stop a running job. It never *owns* a job; it
submits, observes, and cancels, exactly like the CLI.

### Layout

```
┌─ packrat ──────────────────────────────────────────────── v0.1 · daemon ● up ─┐
│                                                                                │
│       ___                                                                      │
│      (o.o)      p a c k r a t                                                  │
│      (>♦<)      "hoards everything, keeps a system"                            │
│      /   \      · 124,803 assets hoarded ·                                     │
│                                                                                │
│  ┌─ Collection ─────────────────┐  ┌─ Roots ──────────────────────────────┐  │
│  │ Assets      124,803          │  │ iPhone     D:\Backup\iPhone   98,412  │  │
│  │  photos     111,240          │  │ Camera     E:\Photos          26,150  │  │
│  │  videos      13,563          │  │ Downloads  D:\dump               241  │  │
│  │ Trashed       3,904          │  │ _Trash     D:\Backup\_Trash  (trash)  │  │
│  │ Duplicates*     612 (est)    │  │ …                                     │  │
│  │ Last scan   2h ago           │  │  ● scanned recently  ○ stale/never    │  │
│  └───────────────────────────────┘  └───────────────────────────────────────┘  │
│                                                                                │
│  ┌─ Jobs ─────────────────────────────────────────────────────────────────┐  │
│  │ ▶ scan  D:\Backup\iPhone   ██████████░░░░░  67%  8,912/13,204  ETA 4m   │  │
│  │   dedup D:\Photos          done · 2 clusters staged · awaiting review    │  │
│  │   merge E:\dump→iPhone     done 11:02 · 240 copied, 1 trashed skipped    │  │
│  │   [c] cancel running   [l] logs   [Enter] details                        │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                                │
│  What do you want to do?                                                       │
│   [s] Scan a folder        [d] Dedup a folder      [m] Merge into a folder     │
│   [t] Refresh / cleanup trash   [r] Manage roots   [q] Quit                    │
└────────────────────────────────────────────────────────────────────────────────┘
```

Three stacked regions under the logo: **stats**, **jobs**, **menu**. The header shows version and
daemon health (auto-spawns it if down).

### Panels

- **Logo + tagline** — the packrat mascot (ASCII art of a rat clutching a `♦` — its hoard),
  the tagline "hoards everything, keeps a system", and a **live "· N assets hoarded ·" line** that
  reflects the current total-asset count (updates as scans/merges add assets). Cosmetic + a small
  at-a-glance stat; sets the tone.
- **Collection stats** — read-only DB rollups: total assets (photo/video split), trashed count,
  an estimated duplicate count (from `similarity_edges`, marked `*` as "since last dedup"), and
  last-scan recency. Refreshes live while jobs run.
- **Roots** — each registered root with path, asset count, and a freshness dot (scanned recently
  vs. stale/never); trash roots labelled. This is the read view; **[r] Manage roots** opens
  add/remove/rename (the `register` operations).
- **Jobs** — the heart of the TUI. Lists the **running** job (live progress bar, counts, ETA) and
  **recent** finished/paused jobs (from the `jobs` / `review_runs` tables). A paused dedup/cleanup
  shows **"awaiting review"** with a shortcut to open its `_packrat_review\` folder in Explorer and
  to run `--confirm` / `--cancel`. `[c]` cancels the running job (cooperative stop at its next
  checkpoint, §3); `[l]` tails its log; `[Enter]` opens a details view.
- **Menu** — single-key actions that launch the operations. Because only one mutating job runs at
  a time, launching while busy shows the "busy" state rather than starting a second (§3). Each
  action collects its target (a folder picker / path prompt) then submits the job and drops you
  onto the Jobs panel to watch it.

### Behavior & scope

- **Observe-and-control, not a file manager.** The TUI never previews or edits media — that is
  Explorer's job (design tenet §1). For dedup/cleanup review it just *links out* to the staging
  folder in Explorer and waits; the actual keep/delete decisions are made by adding/removing
  shortcuts there, then confirmed from the TUI or CLI.
- **Live.** Panels poll the daemon (or subscribe to a progress stream) so a scan started in
  another terminal appears here with a moving bar; cancelling here stops it there.
- **Keyboard-first**, mouse optional (Textual supports both). All actions reachable by single
  keys shown in brackets.
- **Read-safe.** Everything the TUI does maps to an existing CLI verb — it issues no privileged
  operation of its own, so CLI and TUI stay behaviorally identical.
- **Later milestone** (§13 M6): the CLI + daemon job runtime are the prerequisite; the TUI is a
  presentation layer on top and can land once jobs are observable.

---

## 13. Build milestones (each independently useful)

- **M0 — Skeleton + job runtime + decode smoke test**: repo layout, config, core library,
  SQLite schema; auto-spawned daemon with the **single-worker job queue** (submit / stream
  progress / cooperative-cancel / "busy" rejection), CLI client with **Ctrl-C-detaches** and
  `--detach`, `daemon start/stop/status`. **Plus the §9.1 smoke test** — one real sample of every
  allowlisted extension (and the RAW group) run through decode→hash→perceptual→embed to resolve
  the ⚠ cells (AVIF, RAW/cr3, `pdqhash` Windows wheel) before building on them.
- **M1 — Register + scan (exact identity)**: `register` (metadata-only root creation), then the
  `scan` job — walker, fast-path, BLAKE3, metadata, asset/file-instance model, exact byte-identity
  resolution (attach instances), deletion detection — plus `status`. No embeddings, no perceptual.
  Now the collection is known by exact hash.
- **M2 — Perceptual signatures (scan)**: photo PDQ (primary) + pHash + video frame-hash
  signatures written to `phash`/`vphash` during scan. No pairwise matching yet — just the
  inputs. No GPU/CLIP.
- **M3 — Dedup operation**: single-folder `dedup` — §5 matching engine over DB fingerprints +
  liveness check, `similarity_edges`/`review_runs`/`review_actions` tables, exact-dup resolution
  (oldest-mtime internal / drop-on-external), perceptual grouping, Windows-shortcut staging
  (`_will_be_deleted\`, `_grouped_by_similarity\`), the pending→confirmed state machine,
  `--confirm`/`--cancel`/`--status`, and the §8.1 audit trail (`proposed.json` + `applied.json`
  in APPDATA). Builds the §5 perceptual matching engine (also reused by `cleanup --perceptual`).
- **M4 — Trash model**: multiple `kind='trash'` roots, "refresh the trash collection" (§6.1 —
  index trash-folder files → record/flip assets to `trashed` → empty the folders), scan's refusal
  to index trash roots, `packrat cleanup` (default exact-hash removal with count-confirm; and
  `--perceptual` stateful mode staging recompressed-trash matches for review — reuses the M3
  engine), and `trash refresh`. Comes before merge because merge's headline value is excluding
  trashed-but-still-on-device content.
- **M5 — Merge workflow**: `merge` — refresh-trash-first, exact-hash classification
  (dup-in-source / trashed / exact-known / new; byte-identical collapse only), copy-only ingest
  of `new` files with hash-verify + register. No perceptual matching or review folder — simple and
  one-shot (resumable from its plan).
- **M6 — TUI (`packrat` no-args)**: Textual app — packrat logo, global stats (total indexed
  assets, per-root counts, trashed count), **live + recent job runs with progress**, cancel a
  running job, and a menu to launch operations. The default entrypoint; a window onto daemon jobs
  started from any terminal. (Depends only on the M0 job runtime, so could land earlier.)
- **M7 — Semantic embeddings**: opt-in `scan --embed` CLIP pass writing the `embeddings` table;
  brute-force cosine search scaffold. Tagging/classification behavior on top is **TBD** (§7).
- **M8 — Hardening**: scheduled interval-scan triggers (APScheduler wiring in the daemon),
  DB backup, resumability polish, larger-scale perf (hnswlib), SMB tuning (§10.1), optional
  watchdog real-time mode.

---

## 14. Open questions / risks

1. **Near-dup threshold** `T_match` needs empirical tuning on your real data (burst shots and
   edited copies are the hard cases). Plan a small labeled sample to calibrate it.
2. **Live Photos & sidecars** (.AAE edits, paired .MOV): decide grouping rules.
3. **Video near-dup** is genuinely hard for heavy re-encodes; frame-hash sampling is a
   pragmatic start — consider TMK+PDQF later if recall is insufficient.
4. **Shortcut creation mechanism:** `.lnk` files need creating without a copy — via `pywin32`
   (`win32com` Shell.CreateShortcut) or `winshell`. Confirm thumbnail preview works for `.lnk`
   targets in Explorer (it does for real files; verify in the M3 spike). Fallback if `.lnk`
   previews disappoint: NTFS hardlinks (same volume only) or symlinks (needs privilege).
5. **Audit-trail retention (§8.1):** `proposed.json`/`applied.json` are kept indefinitely by
   default (small text files). Confirm whether you want an auto-prune knob (e.g. delete audits
   older than N days) or truly-forever retention. Also: should `merge` get the same audit trail
   for symmetry? (Currently only dedup does.)
6. **Recompressed-trash on merge (accepted):** `merge` excludes trashed content by **exact hash
   only** — a *recompressed* copy of trashed content slips through as `new` on ingest. This is the
   accepted cost of keeping merge simple/one-shot; it is caught afterward by
   `cleanup <dest> --perceptual` (§6.2), which stages recompressed-trash matches for review.
   (`dedup` still excludes trashed assets from grouping — §5 — so cleanup is the dedicated path.)
```
