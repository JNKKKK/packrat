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
   Recycle Bin, and originals are removed only after explicit confirmation. **Caveat (§10):**
   the Recycle Bin exists only for local volumes — on NAS/SMB roots (most of the collection,
   §10.1) deletion is **permanent**, so typed confirmation + `--dry-run` are the real safety net
   there, and the confirm prompt warns when the delete set includes network-path files.
4. **Idempotent & resumable.** Any index/merge/tag job can be interrupted and re-run.
5. **Lazy when safe, thorough on schedule.** Skip re-fingerprinting when `path` + exact `size` +
   near-`mtime` (tolerant) are unchanged; do full sweeps on a fixed interval as the backstop.
6. **TUI is the default face; the CLI is the complete surface.** The expected way to drive
   packrat is the **TUI** (`packrat` no-args — the primary, discoverable entrypoint, §12). But the
   TUI is only a *presentation layer*: **every action it offers must also be a first-class CLI
   command** (§11), and the TUI issues no privileged operation of its own — each of its actions maps
   onto an existing CLI verb, so the two stay behaviorally identical (§12 "Read-safe"). This is a
   hard rule, not a courtesy: it keeps packrat fully scriptable/automatable and headless-usable
   (SSH, cron, CI), guarantees the TUI can never diverge from or outrun the CLI, and means the CLI
   is the authoritative contract the TUI (and any future client) is built *on top of* — both are
   thin clients over the same daemon API (§3). Consequence for every new capability: **add the CLI
   verb first (or together), never TUI-only.**

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
     - Photo: PDQ (256-bit) — the single photo signal.
     - Video: duration + sequence of per-frame PDQ hashes sampled across the timeline.
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
- a **persisted job queue** running **one mutating job at a time**, the rest waiting in a durable
  FIFO backlog that survives restart (§3 guarantee 1); each job is cooperatively cancellable and
  checkpointed/resumable (per §8);
- the **scheduler** for interval scans (submits scan jobs like any client);
- the review-run state (`review_runs`) and audit trail.
Exposes a small HTTP API on `127.0.0.1` with a local token (`%APPDATA%\packrat\token`). Reads
tunable settings from `%APPDATA%\packrat\config.toml` (§9.2), reloaded at each job start.

**Progress transport — server-sent events (SSE), not polling.** A client that submits (or attaches
to) a job holds an SSE stream on the HTTP API; the daemon pushes progress/state events (bar, counts,
ETA, completion). The TUI uses the same stream for the running job. Read-only *snapshots*
(`status`, `roots`, TUI stat panels) are plain request/response — polled on a light timer for the
"recent jobs" list. SSE is chosen over polling for the live progress path so a moving bar doesn't
require a busy-loop of requests; it degrades gracefully (a dropped stream just reconnects, since job
state is durable in the `jobs` table).

**Auto-spawn handshake (race-free).** Auto-spawn on first client use must tolerate *two* clients
racing to start the daemon at once. The client does **bind-or-connect**, not check-then-spawn: it
tries to connect to the API port; on failure it attempts to become the daemon by acquiring a
single-instance lock (an exclusively-created lockfile / a bind on the fixed loopback port — whoever
wins is the daemon) and writing the `token`; a loser that fails the lock simply retries the connect
against the winner. So concurrent first-uses converge on **one** daemon, never two. The token file
is written by the winner before it accepts requests, so clients authenticate against a live server.

**Startup reconciliation (crash / kill / power-loss recovery).** The daemon owns the worker slot
*in memory*, so if it dies mid-job the `jobs` row is orphaned — still `status='running'` though no
worker exists. On **every** daemon start, before serving any request, it reconciles:
- **Orphaned `running` jobs → `interrupted`.** Any `running` job row is stale by definition (a live
  daemon has at most one, in *this* process, which just started). Mark each `interrupted`,
  `finished_at`=now, `error='daemon restarted'`. The daemon **does not auto-resume or re-enqueue**
  the work (per §11-recovery decision): the durable per-op plan is intact, so the user re-runs the
  command to continue. This avoids a **crash-loop** (a file/bug that killed the daemon would
  re-kill it on boot) and never resumes a *destructive* apply (`dedup`/`cleanup --confirm`) with
  nobody watching. Resume paths per type (all already specified — this step only flips the stale
  status flag so the machinery can re-engage):
  - **scan** → re-run `scan`; the **fast-path** (path+size+mtime skip, §8 A2 step 4) makes already-
    fingerprinted files no-ops, so it effectively continues where it left off — `jobs.done` is just
    the progress number, not the resume key. Deletion-detection is naturally safe (it keys off this
    pass's enumeration, §8 A2 step 11).
  - **merge** → its `merge_runs` row is still open (`planning`/`copying`); re-running `merge`
    silently auto-resumes from the frozen plan (§8 C). *(A crash in Phase 1 before the plan was
    committed leaves no open `merge_runs` → re-run just starts fresh.)*
  - **trash refresh** → idempotent by construction (record-then-delete, §6.1); re-run re-processes
    only the trash files still present.
  - **untrash** → idempotent (hash → forget/reactivate, §6.3); re-run is a no-op on already-handled
    files.
  - **dedup/cleanup analyze** interrupted mid-staging → the crash left a `pending` review_run with
    **half-built staging**. Reconciliation **rolls it back**: delete the partial
    `_packrat_review\` staging folders and mark that review_run `cancelled` (record it as
    `interrupted-analyze` in the audit `applied.json`, §8.1). This clears the way for a clean
    re-run — otherwise the pending row would reject a fresh `dedup`, and `--confirm` on partial
    staging would apply a wrong plan. *(A **completed** analyze — paused, fully staged, awaiting the
    user — has no `running` job row, so it is untouched: its `pending` review_run and staging remain
    exactly as left, ready for `--confirm`/`--cancel`.)*
  - **dedup/cleanup `--confirm`** interrupted mid-apply → the review_run is still `pending` and the
    plan (`review_actions`) records intended deletions; re-running `--confirm` re-reads shortcut
    presence and re-applies via the per-file lazy-liveness gate (§8 B Phase 6), which is idempotent
    (already-deleted files → "already-gone"). The DB backup taken before apply is the backstop.
- **Durable `queued` backlog → drained, with one carve-out.** Because the backlog is persisted
  (§3 guarantee 1, §4 `jobs.status='queued'`), jobs that were merely *waiting* — never started, so
  nothing on disk or in the DB was touched — are **not** stale and are **kept**: after the running
  row is reconciled, the daemon resumes draining them in `enqueued_at` order like normal. This is the
  point of a durable queue — an auto-appended `roots register --scan` (§8 A1) still runs after a
  crash/restart. **Carve-out (matches the running-job stance):** a queued **destructive apply**
  (`dedup`/`cleanup --confirm`) is flipped to `interrupted` instead of auto-run — a delete-set must
  never apply with nobody watching (same reason the daemon won't auto-resume a *running* `--confirm`),
  so the user re-issues it deliberately. Non-destructive queued jobs (scan, merge, analyze,
  trash-refresh, untrash) drain automatically.
- **Idempotency is what makes "just re-run" safe** for every case above — each op either resumes
  from a committed checkpoint/plan or re-derives a no-op for work already done. Reconciliation only
  *unblocks* re-running by clearing stale `running`/half-staged state, and drains the intact durable
  backlog; it performs no file I/O except the analyze-rollback staging cleanup.

**Clean shutdown (`daemon stop`) is a resumable interruption, not a cancel.** A graceful stop
signals the running job to checkpoint, then exits; its `jobs` row becomes `interrupted` (same as a
crash — resumable), **not** `cancelled`. Cancelling is a distinct, explicit user action (TUI `[c]`
/ another terminal, §9/§12) that *does* set `cancelled` (terminal) and, for merge/review, discards
the resumable plan. So "stop the daemon" never loses in-flight progress; only an explicit cancel
does.

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

**Concurrency — two independent guarantees.** packrat serializes work at two levels; a mutating
job must clear **both** to start.

1. **Global: one mutating job runs at a time, the rest wait in a durable queue.** The single-worker
   queue is the enforcement point: exactly one mutating job is ever *running*. **Every** mutating
   submission is **enqueued** — a `jobs` row with `status='queued'` (§4) — *never rejected at
   submit*; nothing is turned away. The backlog is **persisted** (durable `jobs` rows, not an
   in-memory list), so queued work survives a daemon restart and drains on the next start (§ startup
   reconciliation — with one safety carve-out: a queued destructive apply is *not* auto-run
   unattended). This is what lets one command line up work behind another (and, later, lets `roots
   register --scan` append a scan behind whatever is running — §8 A1). The worker *slot* is still
   in-memory (a live daemon runs at most one job in *this* process, which is what makes reconciliation
   correct — a `running` row at boot is stale); the **backlog** is durable. No lockfile, no
   crash-stale lock. Read-only queries (`status`, `roots`, TUI stats) run anytime, concurrently, and
   never queue.
   - **Dequeue picks the first *runnable* job in FIFO order — the queue waits on the worker, never on
     a human.** When the worker frees, it scans the backlog oldest-first (`enqueued_at`, ties by `id`)
     and runs the first job whose **owned root is free** (or that owns no root). A job whose owned
     root is currently held by a **pending review / open merge** (guarantee 2) is **skipped, left
     `queued`, and retried on a later pump** — not failed, not run. So FIFO holds *among runnable
     jobs* and *among jobs contending for the same root*, but a runnable job legitimately passes a
     blocked one ahead of it (the "recent jobs" list is therefore ordered by *start* time, not submit
     time — intended). This is a small runnable-first scheduler, not strict FIFO.
   - **What wakes a blocked job is just the next pump.** The ops that free a root — `dedup`/`cleanup
     --confirm` (completes the review), `--cancel`, a resuming `merge` — are **themselves jobs**
     (§8 B/§6.2: `--confirm`/`--cancel` are separate `jobs` rows of the same type, dispatched by
     params). So "root freed → re-examine the backlog" needs no separate signal: the queue is pumped
     after **every** job finishes (which you need anyway to start the next one), and the confirm/cancel
     job's completion *is* that pump. **Invariant to preserve:** the queue must be pumped whenever a
     root-holder is released; today that is always a job completion, so pump-on-finish suffices — if a
     confirm/cancel ever became a non-job API mutation, it too must pump.
   - **No deadlock; at worst starvation, and it's visible.** A `queued` job holds **no** root until it
     *runs* (an analyze opens its `review_runs` row only on execution), so a blocked queued job holds
     nothing and can't be half of a cycle. The only holders are already-pending reviews/open merges,
     cleared by a human decision or a resume-job. So skip-and-retry cannot deadlock — it can only
     *starve* a job whose root stays pending indefinitely, which is acceptable because the TUI/CLI
     show that job as **`blocked: root R has a pending <run> — confirm/cancel to unblock`** (per-job
     reason, §12), and you can cancel it out of the backlog anytime.
   - **`--detach` returns the queued job's id immediately**; a foreground CLI submit streams from the
     moment it's enqueued (showing `queued · waiting for worker`, or `queued · blocked: …` when its
     root is held, then live progress once it starts). Cancelling a still-`queued` job drops it from
     the backlog (`cancelled`, never ran) — distinct from cancelling the running one (§ cancellation).

2. **Per-root: one *active* operation owns a root at a time** (running **or** pending). This is the
   general invariant that the per-operation validations (§8 B Phase 0, §6.2, §8 C Phase 0) and the
   DB's partial-unique indexes (§4: one pending `review_runs` per root; one open `merge_runs` per
   dest root) all enforce as special cases. State it once here so no pair is missed (the previous
   text enumerated dedup/cleanup/merge pairwise and **omitted scan** — the gap this closes):

   > **A root has at most one active operation.** An operation is *active* on the root it **owns**
   > — the root it targets and stages/plans/mutates against: `scan R` owns `R`; `dedup R`/`cleanup R`
   > own `R` (running **or** while their `review_runs` stays `pending`); `merge … --into D` owns the
   > library root containing `D` (running **or** while its `merge_runs` is `planning`/`copying`).
   > This is enforced **at dequeue, not at submit** (guarantee 1): an op whose owned root is already
   > held is enqueued like any other, then **held in the backlog and skipped** until the holder clears
   > — the TUI shows it `blocked: … — confirm/cancel to unblock`. Ownership is only ever *acquired*
   > when a job actually runs, so two same-root ops can sit in the queue but never run at once, and the
   > partial-unique indexes are never violated (the second analyze opens its `review_runs` row only
   > after the first is confirmed/cancelled). **`scan` is included:** a `scan R` behind a pending
   > review/open merge waits in the backlog rather than churning the plan's rows (§8 A2 step 1a); a
   > scheduled / `--all` scan still **skips that root and logs it** (it iterates roots rather than
   > owning one, so it must not park the whole sweep on one under-review root).

   **Owned vs. referenced — what is *not* locked.** Exclusivity is on the **owned** root only, not
   on roots an op merely reads or can reach into: `dedup` compares against **all active assets
   collection-wide** and may delete an *external* survivor in another root (§8 B cross-folder note);
   `merge` reads every root's hashes. Locking those "referenced" roots would serialize nearly
   everything (dedup touches almost all of them), so we don't. Cross-root reach stays safe by a
   different mechanism — the **lazy-liveness gates**: confirm/apply re-`stat()`s each file by its
   stored **path** right before acting and spares/promotes if it moved, so a legitimate scan of a
   *referenced* root that forgets a now-gone asset never harms an in-flight plan (the plan keys off
   path, tolerates a dangling `asset_id`/`instance_id`, and resolves toward sparing). Per-root
   exclusivity handles the *owned* root; lazy liveness handles *referenced* reach.

**Why the queue slot and the review lock are distinct.** A paused `dedup`/`cleanup` holds its
`review_runs` row (guarantee 2, DB, no time limit) but **not** a worker slot (guarantee 1): the
analyze job finishes, the global queue frees for other work, and you can review in Explorer for as
long as you like — only operations that would *own the same root* are blocked meanwhile.

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
  id, content_hash /* blake3, unique */, media_type /* photo|video (by extension) */,
  size, width, height, duration_s, captured_at /* from EXIF/ffprobe */,
  status /* active|trashed  -- no 'missing': forgotten assets are deleted */,
  undecodable /* 0|1, default 0: bytes hashed OK but the decoder rejected the pixels — set by scan;
                 such an asset has NO phash/vphash/embedding and is excluded from perceptual work.
                 Orthogonal to status (an undecodable asset is still active/trashed). Distinct from
                 a merge-created asset that is simply not-yet-fingerprinted (undecodable=0, no phash
                 yet) — see the "fully fingerprinted" predicate in §8 A2 step 4. */,
  decode_error /* nullable text: last decoder failure detail, for debugging POC-format wheels (§9.1) */,
  /* (a `detail_score` column existed here through schema v5 — a retained-detail estimate for the
      photo keep-lead — but was RETIRED in v6: it cost ~40% of scan CPU and, once banded to tame its
      high-quality-JPEG noise, only ever agreed with file `size` within a format, so the photo
      keep-lead now ranks resolution → format rank → size (§8 B). A fresh DB omits the column; an
      existing DB keeps it as harmless dead data, since there is no DROP-column migration.) */
  codec /* nullable text, VIDEO only: codec name (h264|hevc|av1|vp9|…) from the decode probe (§8 A2
           step 8). Feeds the video stage-2 keep-lead's codec-efficiency weight (§8 B match.codec_weights).
           NULL for photo/undecodable. NOT recomputed by `scan --full` (which skips re-decoding a
           byte-unchanged, fully-fingerprinted hit — §8 A2 step 6), so a pre-existing NULL persists. */,
  added_at, trashed_at, trash_reason)

file_instances(   -- presence = row existence; a gone file has its row deleted (no 'present' flag)
  id, asset_id, root_id, path, filename, size, mtime, last_seen_at)
  -- UNIQUE(root_id, path): one row per physical file. Scan's exact-dup "hit" upserts on this key
  --   (§8 A2 step 6), so re-encountering a known file (--full re-hash, mtime drift, merge-created
  --   backfill) reuses its row instead of creating a duplicate instance. `path` is stored in the
  --   canonical long-path-safe form (§8 A1 step 1) so equality is well-defined.

phash(   asset_id, algo /* always 'pdq' — photo pHash dropped (§5.3); column kept for a stable
                          shape and to leave room for a future algo without a migration */,
         bits /* blob, 256-bit PDQ */,
         quality /* 0-100 PDQ quality */ )                    -- one row per photo asset; by scan
vphash(  asset_id, frame_index, t_offset_s,
         pdq_bits /* blob, 256-bit PDQ of the sampled frame — same algo as photos (§5.3), NOT
                     pHash anymore */,
         quality /* 0-100 PDQ quality; frames below video.min_frame_quality are excluded from
                     matching but still stored/flagged (§5.3) */ )   -- one row per sampled frame; by scan
embeddings( asset_id, model, vector /* float32 blob, e.g. 512d */ )  -- only if scan --embed

similarity_edges(   -- pairwise near-dups; written by `dedup`, NOT scan. `distance` = PDQ Hamming
                    --   for photo (≤ t_photo_edit), or the video match score (§5.3); the medium's
                    --   own cutoff decides the edge — t_photo_edit (photo) / T_match_video per-frame
                    --   + frame_match_fraction vote (video). dedup bands the photo distance into its
                    --   review stages via t_photo_recompress (§8 B); the edge itself is unbanded.
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
                 next stage not yet staged) — the apply-then-advance crash marker (§8 B Phase 7) */,
  created_at, confirmed_at )
  -- partial UNIQUE(root_id) WHERE status='pending'  → at most one open review run per folder.
  --   ONE row spans dedup's whole 3-stage sequence; `stage`/`stage_phase` track progress within it,
  --   `status` stays 'pending' until the LAST non-empty stage applies (§8 B).
  -- One facet of the §3 per-root exclusivity invariant: dedup, perceptual-cleanup, in-flight
  --   merge, AND scan are mutually exclusive on a root (scan is blocked by §8 A2 step 1a, not by
  --   this index, since scan opens no review_runs row).

review_actions(   -- the persisted, crash-safe plan for a review_run
  id, run_id,
  stage /* dedup 1|2|3 — which stage this action belongs to (--confirm applies WHERE stage=cursor);
            NULL for cleanup, which is single-stage */,
  folder /* exact_dup_to_delete|suspect_recompression|with_minor_edits|perceptually_identified_trash */,
  kind /* exact|perceptual */, reason /* exact-internal|exact-external|perceptual|cleanup-perceptual */,
  default_action /* delete|keep */,
  asset_id, instance_id, path,           -- the file this action targets
  survivor_instance_id,                  -- the copy being kept (stage-1 exact); NULL otherwise
  group_no, member_no, is_external,      -- perceptual grouping only (stages 2/3, cleanup)
  matched_trashed_asset_id, distance,    -- cleanup-perceptual only (which trashed asset, PDQ dist)
  shortcut_name )
  -- `path` is the AUTHORITATIVE target: --confirm re-stats it (§8 B Phase 6) and never trusts the
  --   DB row's liveness. So `asset_id`/`instance_id`/`survivor_instance_id` are recorded for
  --   reporting/reference and MUST tolerate becoming dangling: a legitimate scan of a *referenced*
  --   (external) root can forget a now-gone asset mid-review (§3 owned-vs-referenced). These FKs are
  --   therefore NOT part of any ON DELETE CASCADE — deleting an asset/instance must NOT delete
  --   review_actions rows (they'd be nulled/left dangling); confirm resolves a dangling ref toward
  --   sparing via the path stat. (The owned root can't be churned mid-review — per-root exclusivity
  --   §3 blocks scan on it — so only external references can dangle.)

merge_runs(   -- one merge lifecycle (§8 C); the frozen plan header + cross-op guard
  id, job_id, source_path, dest_path, dest_root_id,
  status /* planning|copying|done|cancelled|error */, created_at, finished_at )
  -- partial UNIQUE(dest_root_id) WHERE status IN ('planning','copying')
  --   → at most one open merge per dest root; this is the "in-flight merge" marker that
  --     dedup (§8 B Phase 0) and cleanup (§6.2) check to refuse an overlapping run.
  -- completed runs are retained (queryable merge history; see §8.1 note / §14 #5).

merge_plan_items(   -- the persisted, crash-safe, FROZEN per-source-file plan for a merge_run
  id, run_id,
  source_rel_path,                 -- path relative to source; dest mirrors it (§8 C Phase 3)
  size, mtime, content_hash,       -- from Phase 1; hash lets resume SKIP re-hashing + verify collisions
  classification /* dup-in-source|trashed|exact-known|new */,
  rep_of_hash,                     -- dup-in-source only: the sibling hash whose rep this defers to
  dest_path,                       -- final dest path incl. any numeric-suffix collision rename; NULL until copied
  progress /* pending|copied|registered|copied-unindexed|skipped|error */, error )
  --   copied         = file written+verified, DB register still pending (the crash gap; resume finishes it)
  --   registered     = terminal: file on disk AND catalogued
  --   copied-unindexed = terminal: file written to an IGNORED dest path, deliberately NOT registered
  --                      (would otherwise be forgotten by the next scan's deletion-detection — §8 C Phase 3)
  -- NOTE: no metadata columns — dimensions/duration/captured_at are probed just-in-time in
  --   Phase 3 for `new` reps only (classification needs the hash alone), so they never persist.

-- tags(...) omitted for now — tagging/classification schema is TBD (§7)

jobs(    id, type,
         root_id /* nullable: the single root this job concerns — `scan <root>`, `dedup`, `cleanup`,
                    `merge`→dest. NULL for multi-root (`scan --all`) and root-less (`untrash`,
                    `trash refresh`) jobs. The TUI's per-root job list keys off this column (plus
                    `scan_results` for the per-root rows a `--all` scan writes); see §12. */,
         status /* queued|running|done|error|cancelled|interrupted */,
         total, done, enqueued_at, started_at, finished_at, error,
         result_json /* nullable: a compact, uniform, human-showable OUTCOME summary written at
                        terminal time by EVERY job, whatever its type or terminal status — scan
                        banner counts, dedup/cleanup staged/applied tallies, merge copied/skipped,
                        trash-refresh absorbed/emptied, untrash untrashed/forgotten. This is the
                        single surface the TUI renders as a job's "result card" (§12) WITHOUT joining
                        per-op tables; the richer per-op tables (scan_results/review_runs/merge_runs +
                        the §8.1 audit) stay authoritative for deep forensics ([Enter] details). A job
                        that died before finishing may carry a partial or NULL result_json — its
                        `status` (+ `error`) still records the outcome, so every job is show-able. */,
         params_json )
  -- `enqueued_at` = when the row was created (as `queued`); `started_at` = when the worker actually
  --   BEGAN running it (NULL while still queued); `finished_at` = terminal time. FIFO order =
  --   enqueued_at (ties by id). A job submitted while the worker is free is enqueued and started in
  --   the same breath (both stamps ~together); one submitted while busy waits with started_at NULL
  --   in the durable backlog until it runs (§3 guarantee 1).
  -- `total`/`done` are a PROGRESS-DISPLAY counter only (work units finished / total, drives the
  --   bar + ETA). They are NOT the resume mechanism: on re-run each op recovers from its own
  --   authoritative durable state — scan from the fast-path (path+size+mtime skip) + last_seen_at
  --   (§8 A2), merge from per-item merge_plan_items.progress (§8 C), review from review_actions
  --   (§8 B). `done` may be stale after a crash (last increment uncommitted) and that's harmless,
  --   because the authoritative state — not `done` — decides what re-runs.
  -- `queued` = submitted while a mutating job was running; waits in the durable FIFO backlog (§3
  --   guarantee 1). Retained across a daemon restart and drained in order — EXCEPT a queued
  --   destructive apply (`dedup`/`cleanup --confirm`) is flipped to `interrupted` on restart, never
  --   auto-run with nobody watching (§3 reconciliation). Cancelling a queued job just drops it from
  --   the backlog (`cancelled`, never ran).
  -- `interrupted` = the daemon died while this job was `running` (crash/kill/power loss); set by
  --   startup reconciliation (§3), NOT by the worker. It means "the process vanished, the durable
  --   per-op plan is intact, re-run the command to resume." A clean `daemon stop` also lands here
  --   (interrupted, resumable) — distinct from a user cancel, which is `cancelled` (terminal, §3).

scan_results(   -- persisted scan report; one row per (completed scan job, root) so `status <root>`
                --   (and the M6 TUI) can re-render a past scan (§8 A2 Phase 5, §11).
  job_id, root_id, root_name,
  full, embed, profiled,                 -- the flags that produced this scan
  candidates, new, exact_dup, backfilled, matches_trashed, skipped_fastpath,
  undecodable, errors, deleted_instances, forgotten_assets, root_offline,   -- the §8 A2 banner counts
  profile_json /* ScanProfiler snapshot, NULL unless --profile */, created_at )
  -- PRIMARY KEY (job_id, root_id). A `--all` scan writes one row PER library root under a single
  --   job_id; re-scanning a root APPENDS a new row (new job_id) — the table is a growing per-root
  --   HISTORY, kept indefinitely (retention deferred — §14 #10). `status <root>` reads the newest
  --   (job_id DESC). ONLY a *completed* scan writes rows — dry-run/cancel/interrupt/error write none
  --   (persist runs after the per-root loop); resuming an interrupted scan re-runs and writes then.
  -- CRUCIAL: `undecodable` (and the scan_problem_files below) are RE-DERIVED FROM THE CATALOG at
  --   scan end (assets.undecodable=1 with a live instance in the root), NOT counted per-pass — because
  --   a resume/incremental re-run FAST-PATH-SKIPS undecodables (they're "fully fingerprinted", §8 A2
  --   step 4), so a per-pass count would wrongly empty out on re-run. So this row describes the ROOT's
  --   current state, not just what this pass touched. (The other counts ARE per-pass activity.)

scan_problem_files(   -- the undecodable / unreadable files behind scan_results' counts, so the exact
                      --   paths + reasons are retrievable (not just counted). Keyed to the scan job.
  id, job_id, root_id, path, media_type,
  problem /* undecodable|read-error */,
  content_hash /* NULL for read-error — bytes never read */, detail /* decode_error or OSError text */ )
  -- `undecodable` rows are re-derived from the catalog each scan (see scan_results) → the same set
  --   re-appears on every scan of the root (grows per-scan, not per-distinct-problem — §14 #10).
  -- `read-error` rows are per-pass: an unreadable file has no asset to re-derive, and leaves no row
  --   to fast-path-skip, so it is re-detected on every pass anyway.
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
- **Unreachable-root / incomplete-listing guard (per-directory):** deletion-detection (removing
  gone instances) reconciles an instance only if its **containing directory was fully and cleanly
  enumerated** this pass. If a directory listing errored/timed out mid-scan (common on SMB — see
  §10.1), instances under *that subtree* are left untouched; a fully offline root (unplugged drive,
  missing share) skips everything. Incomplete data must never be read as "files deleted," which
  would wrongly forget fingerprints — but the scope is the affected subtree, not the whole root, so
  one flaky folder doesn't stall reconciliation collection-wide (§8 A2 step 11, §10.1).
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
  - Photo: **PDQ (256-bit)** + its quality score — the single photo signal. (pHash is deliberately
    *not* stored for photos; see §5.3 for why one signal is both sufficient and higher-recall.)
    Photo quality is **stored and surfaced as a confidence hint, but never gates** a photo out of
    matching — see §5.3 (asymmetry with video).
  - Video: **duration** + a sequence of **per-frame PDQ** hashes (+ quality) sampled at fixed
    fractions of the timeline (§5.3). Frames use the *same* PDQ as photos — after §7 dropped photo
    pHash, nothing in packrat uses pHash anymore.
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
  human review, so this is **only** ever surfaced by `dedup` (§8 B) and `cleanup --trash-perceptual`
  (§6.2), which stage candidates in Explorer for the user.

Exact resolution is cheap and safe enough to run inline anywhere; perceptual matching is a
deliberate, reviewed, opt-in operation.

### 5.3 The perceptual matching engine

A single scope-agnostic matcher, run only by `dedup` and `cleanup --trash-perceptual`. It uses the
perceptual signature alone (never CLIP), over fingerprints already in the DB — pure hash math, no
file I/O.

- **Photo:** the **only** signal is **PDQ Hamming distance**. PDQ at a sane threshold is precise
  on the recompress/resize/format-conversion case — essentially the entire iPhone-re-export
  reality — so one robust signal is both sufficient and higher-recall than gating two signals
  together (which is why pHash is not computed or stored for photos at all — decided in §7 gap
  review; a single signal, no dead data). The matcher itself reports the raw PDQ distance for a
  matched pair; **`dedup` bands that distance into two review stages** with two cutoffs
  `t_photo_recompress < t_photo_edit` (§8 B / §9.2): `d ≤ t_photo_recompress` = a recompression
  (stage 2, near-certain), `t_photo_recompress < d ≤ t_photo_edit` = a minor edit/crop (stage 3,
  scrutinize). The engine's own match cutoff is the wider `t_photo_edit`; the tighter cutoff only
  splits already-matched pairs into stages, so it is a review-ergonomics band, not a second recall
  gate. (`cleanup --trash-perceptual` uses the single wider cutoff, no banding — §6.2.)
  - **Photo quality — annotate, never gate (asymmetric with video).** PDQ's 0–100 quality is
    *stored* per photo but **does not exclude** a photo from matching. This is deliberate and
    differs from video (`video.min_frame_quality`): a video has ~12 frames, so dropping a bad one
    still leaves plenty to vote; a photo has **exactly one** PDQ, so gating it out would make the
    asset **silently invisible to dedup** — a recall loss the user can't see, against the plan's
    recall-first tenet. Instead quality is used two safe ways:
    1. **Confidence hint in review.** PDQ on flat/near-black/letterboxed/low-detail images yields
       hashes that spuriously collide, flooding review with junk pairs. So every staged photo
       near-dup carries its (and its partner's) quality in the `manifest.csv` / `proposed.json`
       (§8 B), and a pair where *either* photo is below `review.low_quality_hint` (default **50**,
       same scale as video) is **flagged low-confidence** — a visual cue to skip it fast, not a
       removal. Nothing is hidden; noisy matches are just easy to dismiss.
    2. **Future gate, no re-scan.** Because quality is already stored, a `min_photo_quality` *gate*
       (if calibration on real data shows the collision flood is worse than the recall cost) can be
       switched on later **without re-decoding the collection**. Off by default in v1.
- **Video:** durations within a tolerance **and** at least a configured fraction
  (`video.frame_match_fraction`, default 0.60 — see table) of sampled frame descriptors match
  within threshold. **Frame descriptor is PDQ** — the same 256-bit hash used for photos, run on
  each sampled RGB frame (a decoded frame is just an image; per-frame PDQ is exactly what Meta's
  TMK+PDQF does — §14 #3). This **unifies photo and video on one algorithm and drops the
  `imagehash` dependency entirely** (after §7 removed photo pHash, video frames were its only
  remaining use). Matching **pre-filters by duration** (compare only clips within ±tolerance) to
  avoid the naïve all-pairs blowup, then compares the two clips **frame-index-aligned** (frame *k*
  of A vs frame *k* of B), which is valid because both are sampled at the *same relative timeline
  positions* and the duration pre-filter keeps their lengths close enough to stay aligned.

  **Video match parameters (concrete defaults; canonical values live in `config.toml` §9.2, logged
  with each run).** These were previously unspecified — pinned here so §8 B / §6.2 are
  implementable:

  | Param | Default | Meaning |
  |---|---|---|
  | `video.sample_frames` | **12** | Frames sampled per video, at the **midpoints of N equal segments**: `t_k = duration·(k+0.5)/N`, `k=0..N-1`. Proportional positions ⇒ same-content clips align frame-to-frame. Short clips (e.g. a 3 s Live-Photo `.MOV`) still get all 12. |
  | `video.duration_tol_s` | **1.0 s** | Absolute floor of the duration pre-filter. |
  | `video.duration_tol_pct` | **5.0 %** | Relative part. Two videos pass the pre-filter iff `|d₁−d₂| ≤ max(duration_tol_s, duration_tol_pct%·min(d₁,d₂))` — so a 3 s clip tolerates ~1 s drift, a 2 h movie ~6 min. |
  | `T_match_video` (per-frame distance) | **e.g. 90** | A frame-pair *matches* iff its PDQ Hamming distance ≤ `T_match_video`. **Separate from the photo cutoffs `t_photo_recompress`/`t_photo_edit`** and typically **more permissive**: video frames carry inter-frame-compression / motion-blur / keyframe-drift noise a still doesn't, and the frame-fraction vote below reclaims the precision a looser per-frame cutoff spends. (Same 0–255 PDQ Hamming scale as photo, different tuned value.) A video near-dup is a single frame-vote match — it is **not** banded into recompress/edit stages; all video matches go to dedup stage 2 (§8 B). |
  | `video.frame_match_fraction` | **0.60** | The two videos are a near-dup iff **≥ 60 %** of *comparable* frame-pairs (see quality gate) match within `T_match_video`. This vote is video's *second* precision control — the one photos lack — which is exactly why the two cutoffs need not (and should not) be equal. |
  | `video.min_frame_quality` | **50** | PDQ emits a 0–100 quality per frame; dark/blurry/transition frames score low and hash unreliably. A frame below this is **excluded** from comparison (stored, but flagged). A frame-pair is *comparable* only if **both** frames clear the gate. |
  | `video.min_comparable_frames` | **5** | If fewer than this many comparable frame-pairs remain after the quality gate, the pair is **not** matched — insufficient evidence beats a coin-flip. |

**Match-distance thresholds — `t_photo_recompress` / `t_photo_edit` (photo) and `T_match_video`
(video)** (all configurable and logged). Same 0–255 PDQ Hamming scale, tuned independently. For a
**photo** the single comparison *is* the decision: `t_photo_edit` is the engine's match cutoff (a
pair with `d ≤ t_photo_edit` is a near-dup), and `t_photo_recompress` (the tighter value) *bands*
matched pairs into dedup's two review stages (§8 B) — it is not a separate recall gate. For a
**video** the per-frame cutoff only feeds a **majority vote** (`video.frame_match_fraction`), a
second precision control, and frames are noisier — so `T_match_video` is typically the most
permissive. A pair is a near-dup iff:
- **photo:** PDQ Hamming distance ≤ `t_photo_edit` (then banded: `≤ t_photo_recompress` → recompress,
  else → minor-edit);
- **video:** the two clips pass the duration pre-filter **and** ≥ `video.frame_match_fraction` of
  their *comparable* (quality-gated) frame-pairs are each within `T_match_video` **and** at least
  `video.min_comparable_frames` comparable pairs exist (table above).

No *second, per-medium* "auto vs. borderline" cutoff is needed on top of these, because **every**
perceptual match is surfaced for human review — nothing is auto-acted-on. (The other `video.*`
knobs are *structure* parameters — how many frames, how close in length, how many must agree — not
distance cutoffs.) Set each threshold high enough to catch what PDQ *structurally* can
(recompression, resize, format conversion) plus the harder cases you want a look at (crops,
rotations, borders/watermarks, heavy re-encodes); every hit lands in the review folder either way,
so a permissive threshold just means more candidates to eyeball, never a silent deletion. The
operation (§8 B / §6.2) decides how matches are staged. **All three cutoffs
(`t_photo_recompress`, `t_photo_edit`, `T_match_video`) and every `video.*` knob need calibration
on real data — §14 #1.**

**Comparison set depends on the caller:**
- **`dedup`** compares a folder's assets against **active assets only** — trashed assets are
  excluded (its model is "collapse redundant copies, keep one survivor," which a trashed asset —
  usually zero instances, nothing to keep, opposite intended action — cannot fit).
- **`cleanup --trash-perceptual`** compares a folder's active assets against the **trashed** set (find
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
   and empties the folder. Trashed fingerprints are kept **indefinitely**, so future merges exclude
   anything matching them — this is what stops junk that still lives on the iPhone from being
   re-merged even after you emptied the trash folder. (Not *irreversibly*: an accidental trash can
   be undone with **`packrat untrash`** — §6.3 — which forgets a fingerprint from trash memory.)

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
   - Compute BLAKE3 + perceptual signature (photo PDQ; video per-frame PDQ). **No embedding.**
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
> consumed. (Recoverable from the Recycle Bin **only if the trash root is on a local volume**; a
> trash root on a NAS/SMB share is emptied **permanently** — §10. Since refresh has no confirm
> gate, treat a network trash folder as one-way: whatever you drop in is gone once it runs.)

**`scan` never touches trash roots** — indexing a trash folder is only ever done here (see §8 A2
validation). This keeps the "inbox that gets emptied" semantics from colliding with scan's
"index and keep" semantics.

### 6.2 `packrat cleanup <folder>` — cull trashed / undecodable files from a library folder

From the user's perspective: **delete junk from `<folder>`.** Use case: a photo you trashed still
lives on the iPhone and got re-pasted into a library backup folder; `cleanup` removes those
re-appearances — and, separately, culls files that won't decode at all.

**`cleanup` requires exactly one mode** (there is no bare default — the command errors without one):
- **`--trash-exact`:** one-shot. Byte-identical matches to trashed content are deleted after a typed
  count confirmation — no per-file review (exact-hash matching is false-positive-free).
- **`--trash-perceptual`:** stateful (analyze → pause → `--confirm`). Adds *perceptual* trash matches
  (recompressed/resized copies of trashed content), staged as shortcuts for Explorer review since
  perceptual matching can misfire. Exact matches are **not** deleted inline in this mode — both
  exact and reviewed-perceptual deletions apply together at `--confirm`.
- **`--undecodable`:** one-shot. Deletes the folder's `undecodable=1` files (§9.1 — bytes hashed but
  the decoder rejected the pixels, so they carry no perceptual signature and can never dedup) after a
  typed count confirmation, **and marks each deleted asset `trashed`** (`trash_reason=
  'cleanup-undecodable'`) so a re-import of the same corrupt bytes is excluded from a future merge.
  Unlike the trash modes it does **not** refresh the trash collection — it targets the folder's own
  undecodables, independent of the trashed set. (`status <root>` lists exactly this set as the
  undecodable problem files, §11.)

**Shared validation & lock (all modes):** `<folder>` must be a registered **library** root —
reject a `kind='trash'` root (its files are consumed by refresh, not cleaned). Then the
**§3 per-root exclusivity invariant** applies: if this root already has an active operation — a
`pending` dedup run, a `pending` perceptual-cleanup run, or an in-flight merge (an open
`merge_runs` row with `dest_root_id` = this root, §4) — the cleanup job is **held in the queue
(dequeue gate, §3) until that holder clears**, rather than running against it, since a live plan may
stage `.lnk`s pointing at files cleanup would delete (broken shortcuts / a stale plan); conversely,
once a `--trash-perceptual` cleanup opens its own `pending` `review_runs` row it *owns* the root, so
dedup, merge, **and scan** (via §8 A2 step 1a) queue-and-wait on it until confirm/cancel. Recommend a
fresh `scan <folder>` first so newly-arrived files are indexed (submit it *before* cleanup — once the
pending run opens, a scan just waits behind it); cleanup operates on indexed instances.

#### Exact mode — `packrat cleanup <folder> --trash-exact`
1. **Refresh the trash collection** (§6.1), so the trashed set is fully current.
2. In `<folder>`, find every `file_instances` row whose asset has `status='trashed'`, matched by
   **exact content hash only**.
3. **Print the count** and require typed confirmation — a sanity check, **no staging folder**. If
   `<folder>` is on a network/SMB root, warn that deletion is **permanent** (no Recycle Bin — §10).
4. On confirm, move each matched file to the **Recycle Bin** (permanent on NAS/SMB — §10) and
   delete its `file_instances` row. The asset stays `trashed` (fingerprints retained). Report
   deleted count.

#### Undecodable mode — `packrat cleanup <folder> --undecodable`
1. **No trash refresh** — this mode is independent of the trashed set.
2. In `<folder>`, find every `file_instances` row whose asset is `undecodable=1` **and** `active`
   (§9.1). (An already-`trashed` undecodable is left for the exact mode.)
3. **Print the count** and require typed confirmation (network-path permanent-delete warning as above).
4. On confirm, move each file to the **Recycle Bin**, delete its `file_instances` row, and — if the
   asset now has zero instances — **mark the asset `trashed`** (`trash_reason='cleanup-undecodable'`,
   fingerprints = the hash retained), so a re-import of the same corrupt bytes is excluded from a
   future merge. Report deleted count.

#### Perceptual mode — `packrat cleanup <folder> --trash-perceptual` (analyze → `--confirm`)
Analyze:
1. **Refresh the trash collection** (§6.1); open a persisted `pending` cleanup run for this root.
2. **Exact matches:** find library instances whose asset is `trashed` (exact hash), as in default
   mode — but **do not delete yet**; record them in the plan.
3. **Perceptual matches:** run the §5 matcher for `<folder>`'s active-asset instances against the
   **trashed** set (photo PDQ ≤ `t_photo_edit` / video per-frame ≤ `T_match_video` + frame vote;
   duration pre-filter). Cleanup uses the single wider photo cutoff — **no recompress/edit banding**
   (that stage split is dedup's review ergonomics, §8 B; here every trash match is one folder). Each
   library file matching a trashed asset per §5.3 is a perceptual-trash candidate.
4. **Stage for review** at `<root>\_packrat_review\_perceptually_identified_trash\`: one `.lnk`
   per perceptual candidate (stat-before-create, so no broken `.lnk`; §8 B Phase 4 rules), plus a
   `manifest.csv` (shortcut → target path → matched trashed asset → distance → `quality` →
   `low_confidence`, same photo-quality confidence hint as dedup — §5.3). Write a `proposed.json`
   audit record (§8.1 style).
5. **Report** the exact-match count (will delete on confirm) and perceptual-candidate count
   (staged for review), print the `--confirm` / `--cancel` commands, and **pause**.

Review convention (**delete-default**, like dedup's `_exact_dup_to_delete\` — *opposite* of dedup's
perceptual keep-default stages): a staged file is treated as trash and **will be deleted**; **remove
its shortcut to spare** the file (mark it "not trash" for this run). Renames count as removal (strict,
per §8 B).

`packrat cleanup <folder> --confirm`:
6. Re-verify liveness per file (lazy stat, as §8 B Phase 6). Require typed confirmation of the
   combined delete set; if `<folder>` is on a network/SMB root, warn that deletion is **permanent**
   (no Recycle Bin — §10). Then, to the **Recycle Bin** (permanent on NAS/SMB — §10):
   - **Exact matches** → delete the `file_instances` row; asset stays `trashed`.
   - **Perceptual matches still staged** (shortcut present) → delete the file **and mark its own
     asset `status='trashed'`**, `trash_reason='cleanup-perceptual'`, fingerprints retained — so
     this near-dup won't re-appear via merge (consistent with dedup's perceptual-deletion).
   - **Perceptual matches spared** (shortcut removed) → left untouched; not trashed.
7. Delete the `_perceptually_identified_trash\` staging folder, write `applied.json`, mark the run
   `completed`. `--cancel` discards staging and deletes nothing.

**`--dry-run`** reports the count/list of library files that *would* be deleted (and, with
`--trash-perceptual`, would be staged) without deleting or staging anything. For the two **trash
modes** it **still refreshes-and-empties the trash collection** (the refresh runs for real) — a
deliberate exception to "dry-run changes nothing": refresh (§6.1) is a shared, idempotent procedure
whose no-op variant isn't worth building, and it is non-destructive to your *library* (it only
absorbs hashes and empties the transient trash inbox — which is what trashing already means).
**`--undecodable --dry-run` changes nothing at all** — that mode never refreshes. Dry-run's
guarantee is scoped precisely: **it never deletes from the library folder being cleaned**; the trash
modes may still empty the trash inboxes.

### 6.3 `packrat untrash <path>` — forget content from trash memory

The reversal for an accidental trash (a file dropped in the wrong folder, a `dedup`/`cleanup`
perceptual discard you regret). Its job is narrow and precise: **remove a fingerprint from the
permanent trashed-hash set** so the content is no longer excluded from future merges.

**What untrash is NOT — it does not restore bytes.** A trashed asset stores only *fingerprints*
(hash, PDQ), never pixels — so packrat cannot reconstruct, preview, or recover the file itself.
Getting the *file* back is the Recycle Bin's job (where one exists — §10), entirely separate. So
untrash never previews and never writes to disk; it only edits DB rows. This is why identification
is **by presenting the file**, not by browsing a gallery of ghosts:

```
packrat untrash "R:\recovered\IMG_4471.jpg"     # one file
packrat untrash "R:\recovered\2019\"            # every media file under a folder (recursive)
packrat untrash "…" --dry-run                   # report what would be forgotten; change nothing
```

**The path is just bytes to hash — it need NOT be a registered root, and untrash does not
catalog it.** This is the key difference from `scan`/`cleanup` (which operate on the catalog):
untrash reads arbitrary files off disk *purely to compute their BLAKE3* for a trash-memory lookup.
The file you're holding (pulled from the Recycle Bin, still on the iPhone, recovered anywhere) *is*
the identifier — the real thing stands in for a preview packrat can't produce. It's fine — expected,
even — for `<path>` to point outside every root.

**Procedure:**
1. Resolve `<path>`: a file, or a folder walked recursively with the **same allowlist/ignore rules
   as scan** (§8 A1) so non-media is skipped. Error if the path doesn't exist / isn't readable.
   (No root resolution, no overlap check — the location is irrelevant.)
2. For each file, compute **BLAKE3** (no metadata, no perceptual — exact-hash match only, chosen in
   the §10 gap review: false-positive-free, like `cleanup`'s default mode) and look it up in
   `assets.content_hash`:
   - **Matches a `trashed` asset** → untrash it (per-asset rule below). Count as `untrashed`.
   - **Matches an `active` asset** → already not trash; no-op, count as `already-active`.
   - **No match** → packrat never knew this content (or already forgot it); no-op, count as
     `unknown`. (Untrash **never creates** an asset — presenting a novel file just does nothing.)
3. **Per-asset untrash rule** (mirrors §4's forget/keep logic, inverted):
   - **Trashed asset still has ≥1 live `file_instances` row** (e.g. refresh flipped a library
     folder to `trashed` but no `cleanup` has deleted the files yet) → flip **`status` back to
     `active`**, clear `trashed_at`/`trash_reason`, **retain fingerprints** (they're valid). It
     simply rejoins the collection in place — nothing was lost.
   - **Trashed asset with zero instances** (the physical copies were emptied/deleted) → **forget it
     entirely**: delete the asset and its dependent rows (`phash`/`vphash`/`embeddings`/
     `similarity_edges`, via `ON DELETE CASCADE`). There is nothing to reactivate — the bytes are
     gone — so we drop the blocklist entry and let the content be treated as **brand-new** if it
     ever reappears in a future merge/scan (exactly the plain-Explorer-delete "forget" model, §6
     case 1). This is the case that resolves the gap: the *hash* stops excluding re-imports.
4. **Report:** `untrashed` (reactivated in place), `forgotten` (zero-instance, blocklist entry
   dropped), `already-active`, `unknown`. **Nothing on disk changed.**

**Safety & interactions:**
- **Non-destructive to files by construction** — untrash only reads (to hash) and writes DB rows;
  it moves/deletes nothing. No typed confirmation needed for the file/dry-run path. *(A future
  batch mode — `--since`/`--reason` — that forgets many entries without presenting files would want
  a count-confirm; deferred, §14.)*
- **Per-root exclusivity (§3):** untrash is a mutating job (takes a global worker slot), but it
  targets *no* root, so it acquires **no** per-root ownership and is never blocked by / never blocks
  a pending review or merge. It touches only `assets`/fingerprint rows by hash.
- **`--dry-run`** reports the same counts without modifying the DB. (Unlike `cleanup`/`merge`,
  untrash does **not** call refresh, so its dry-run truly changes nothing — §6.1's
  always-absorb rule doesn't apply here.)

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

- **A. Add a folder to the collection** — catalog an existing on-disk folder (`roots register` +
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

- **`roots register`** — record the folder as a root. Metadata-only, instantaneous, touches no files.
- **`scan`** — walk a registered root and fingerprint its contents. This is the resumable,
  long-running indexing job. **It does not compute CLIP embeddings unless `--embed` is passed**
  — dedup never needs them, so the default scan stays lean.

Both are non-destructive: files are read-only, the only writes are to the packrat database.
(`roots register` is grouped under the `roots` command — the noun for root lifecycle/metadata —
alongside `roots list`; `scan` stays a flat top-level verb because it is a *job run against* a
root, not root bookkeeping. See §11.)

---

#### A1. `roots register` — declare a folder as a root (metadata-only)

```
packrat roots register "D:\Backup\iPhone"           # default kind: library
packrat roots register "D:\Backup\iPhone" --scan    # register, then immediately kick off a scan
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
  files, zero-byte files, and packrat's own staging area `_packrat_review\` (which contains dedup's
  per-stage folders `_exact_dup_to_delete\` / `_suspect_recompression\` / `_with_minor_edits\` and
  cleanup's `_perceptually_identified_trash\`) plus `.lnk` shortcuts.
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
folder exists and how to treat it. Follow with `scan` (or use `roots register --scan`).

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
1. Resolve the target to a registered root (error if it isn't one — `roots register` it first).
   **Reject `kind='trash'` roots** — trash folders are transient inboxes indexed only by "refresh
   the trash collection" (§6.1), never by `scan` (whose "index and keep" semantics would fight the
   "index then empty" model). → reads `roots` (match `path`); no write.
1a. **Per-root exclusivity check (§3 guarantee 2).** If this root has an active operation — a
   `pending` `review_runs` row (dedup or cleanup) or an open `merge_runs` row
   (`status IN ('planning','copying')`) with this root as `dest_root_id` — **do not scan it**, so
   scan's deletion-detection (Phase 3 step 11) can never churn the `file_instances`/assets that an
   open review plan references. This is the **dequeue gate**, not a submit-time reject (§3
   guarantee 1): a **manual `scan <root>`** submitted against a held root is *enqueued* and then
   **held in the backlog** — skipped on each pump, shown as `blocked: root R has a pending <run> —
   confirm/cancel to unblock`, and run automatically once the holder clears (the confirm/cancel/merge
   job's completion pumps the queue). It does **not** error at submit. A **`--all` or scheduled**
   scan is different — it owns no single root (it iterates), so it can't sit blocked without stalling
   the whole sweep: it **skips this root at run time and logs the skip**, listing the skipped root in
   the report. → reads `review_runs`/`merge_runs`; no write.
2. Recursively walk the root, applying the ignore set, to build the candidate worklist.
   → no DB write (in-memory worklist).
3. Open a job row. → **write** `jobs`: `type='scan'`, `status='running'`, `total`=file count,
   `done`=0, `started_at`, `params_json`={root_id, full, embed}.

**Phase 2 — Per-file pipeline** (worker pool; checkpointed after each file)
For every candidate file:
4. **Fast-path skip (tolerant-mtime key).** If a `file_instances` row exists at this exact `path`,
   its `size` matches exactly, its `mtime` matches within a small **tolerance**
   (`fastpath.mtime_tolerance_s`, default 2 s), and its asset is **fully fingerprinted** (defined
   below) → **write** `file_instances.last_seen_at` (now) only; skip the rest. `--full` ignores the
   fast-path and re-fingerprints unconditionally.
   - **"Fully fingerprinted" — the predicate (authoritative; used here, in step 6's backfill
     exception, and by the undecodable-retry rule).** An asset is fully fingerprinted iff **either**:
     - **`undecodable=1`** — it is as fingerprinted as it will ever be: hash-only identity, no
       perceptual data *by design* (§9.1). Treated as complete so a plain scan doesn't re-decode a
       known-bad file every pass; only **`scan --full`** retries it (§8 A2 step 8 retry note). **Or**
     - **`undecodable=0` AND its perceptual rows for its `media_type` are present:**
       - **photo** → the asset's single `phash` (PDQ) row exists. (Written in the *same*
         transaction as the asset in step 9, so it's all-or-nothing — a partial perceptual write
         is impossible.)
       - **video** → a `vphash` row exists for the asset. (Likewise written atomically in step 9,
         so any frame row present ⇒ the full sampled set is present.)
     **Embeddings are deliberately NOT part of this predicate** — they are opt-in and decoupled
     (§5/§7). Requiring them would force every non-`--embed` scan to re-process every asset. The
     `--embed` pass has its own "no `embeddings` row yet" gate (Phase 3 step 10), independent of
     the fast-path.
   - **Consequence for merge-created assets.** A file copied by `merge` gets an `assets` row with
     `undecodable=0` and **no** `phash`/`vphash` yet (§8 C step 11), so it is **not** fully
     fingerprinted → the fast-path won't skip it → the next `scan <dest>` hashes it, hits the
     existing asset (step 6), and takes the **backfill exception** to fill perceptual data
     in place. This is exactly why the predicate must distinguish "no perceptual rows because
     not-yet-attempted" (fill it) from "no perceptual rows because undecodable" (leave it).
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
   - **Hit** → this is another copy of a known asset: **upsert** a `file_instances` row
     **keyed by (`root_id`, `path`)** — insert if no row exists at this path, else update the
     existing row's `asset_id`/`size`/`mtime`/`last_seen_at` in place — then normally **stop** (no
     metadata/perceptual work). Upsert-by-path (not blind insert) makes re-encountering a
     known file idempotent: a `--full` re-hash, an mtime-drift re-hash, or a merge-created file's
     first backfill scan (case (a) below) all already have a row at this path and must reuse it,
     never create a second instance of the same physical file. If the hit asset was `trashed`,
     this is a re-appeared trashed fingerprint — see Phase 4. This is how identical bytes in two
     *different* paths become one asset with two instances (enforced by the `content_hash` unique
     index on assets; `file_instances` is unique on (`root_id`,`path`)).
     - **Backfill exception (a hit that should still (re)compute perceptual data).** After
       attaching the instance, **continue to steps 7–8** and in step 9 **update the existing asset
       in place** (write/replace `phash`/`vphash`, refresh metadata, set/clear
       `undecodable`/`decode_error`) — *not* insert a new asset — when the hit asset is either:
       - **(a) not-yet-fingerprinted:** `undecodable=0` with **no** perceptual rows — characteristically
         a merge-created asset (§8 C step 11). Fires on **any** scan (incremental or `--full`): such
         an asset fails the step-4 predicate, so an unchanged merge-created file reaches step 6 even
         on a plain incremental scan, and gets filled in here.
       - **(b) undecodable retry:** `undecodable=1` **and this is `--full`** — re-attempt decode
         after a decoder/library upgrade; on success clear `undecodable` and write phash, on failure
         leave `undecodable=1` with a refreshed `decode_error`. (A plain incremental scan does **not**
         retry undecodables — the step-4 predicate treats them as complete.)
       Otherwise the hit **stops early** (the normal case): a decodable, perceptually-complete asset
       has byte-identical content, so there is nothing to redo — true even under `--full`, whose job
       is to catch byte *changes*, which surface as a hash **miss** (or a hit on a *different* asset),
       never as a hit on this same asset.
   - **Miss** → continue; create the asset in step 9.
7. **Metadata** — decode/probe for dimensions, duration, capture time, codec (exiftool /
   ffprobe). → values held for step 9 (→ `assets.width/height/duration_s/captured_at`, `size`).
   (`media_type` is decided by **extension** via the allowlist — §8 A1 — not by decoding, so it is
   known even for files that won't decode.)
8. **Perceptual signature** — photo: PDQ + quality; video: duration + PDQ (with quality) of each of
   the `video.sample_frames` frames sampled at fixed timeline fractions (§5.3). → values held for
   step 9 (→ `phash` / `vphash` rows). *No near-dup comparison here.*
   - *(A photo `detail_score` was computed here through schema v5 — a retained-detail estimate for
     the keep-lead — but was retired in v6: it cost ~40% of scan CPU and, once banded to tame its
     high-quality-JPEG noise, only ever agreed with file `size` within a format. The photo keep-lead
     now ranks resolution → format rank → size (§8 B), needing nothing extra from scan. Scan no longer
     decodes anything solely for the keep-lead.)*
   - **Video `codec` (§8 B stage-2 keep-lead), same decode pass.** For a **video** that decodes,
     capture the video stream's `codec` name (`h264`/`hevc`/`av1`/…) from the already-open decoder —
     free, no extra work. → value held for step 9 (→ `assets.codec`). Feeds the video keep-lead's
     codec-efficiency weight (§8 B). **Photo and undecodable → NULL.**
   - **Decode failure (graceful, §9.1):** if the pixels/frames won't decode (corrupt file,
     unsupported codec, missing wheel), **do not crash and do not abort the asset** — the BLAKE3
     hash (step 5) already gives it identity. Record it in step 9 with **`undecodable=1`**, the
     `decode_error` detail, and **no `phash`/`vphash` rows**. Metadata (step 7) is best-effort:
     keep whatever `exiftool`/`ffprobe` returned (they often read headers of files Pillow/PyAV
     can't fully decode); leave the rest NULL. Log and move on.
9. **Persist the new asset (single transaction).** → **write**:
   - `assets`: `content_hash`, `media_type`, `size`, `width`, `height`, `duration_s`,
     `captured_at`, `status='active'`, `added_at`, `undecodable` (0 normally, 1 on step-8 decode
     failure), `decode_error` (NULL unless undecodable), `codec` (video only, from step 8).
   - `file_instances`: `asset_id`, `root_id`, `path`, `filename`, `size`, `mtime`,
     `last_seen_at`.
   - `phash` (photo only): the single PDQ row — (`asset_id`, `algo='pdq'`, `bits`, `quality`).
     **Omitted entirely if `undecodable=1`.**
   - `vphash` (video only): one row per sampled frame — (`asset_id`, `frame_index`,
     `t_offset_s`, `pdq_bits`, `quality`). **Omitted entirely if `undecodable=1`.** A video that
     decodes but yields **zero** usable frames (all failed to decode) is treated as undecodable.
   Then **write** `jobs.done += 1` (progress-bar counter — see §4; the *durable* record that this
   file is done is the committed `file_instances`/asset rows above, which the fast-path reads on
   re-run, not `done`).

   **Retrying undecodables:** an `undecodable=1` asset has no perceptual rows *permanently*, so the
   fast-path (step 4) treats it as "fully fingerprinted" and won't re-decode it every scan (§8 A2
   step 4 / gap-#3 predicate). To force a retry after a decoder/library upgrade (e.g. a new
   `pillow-heif` that now handles a format), run **`scan --full`**, which bypasses the fast-path,
   re-attempts decode, and **clears `undecodable`/`decode_error` and writes phash rows** if it now
   succeeds. A plain incremental scan never retries them.

*(Near-dup linking is intentionally absent — it is the `dedup` operation, §B, which writes the
`similarity_edges` table from this data. Scan never writes similarity edges.)*

**Phase 3 — Embeddings (only if `--embed`)**
10. **By default skipped entirely — no embeddings computed, no `embeddings` rows written.** With
    `--embed`, assets with no current `embeddings` row for the active model **and `undecodable=0`**
    are queued for a batched CLIP pass → **write** `embeddings`: (`asset_id`, `model`, `vector`).
    (Undecodable assets are skipped — CLIP needs a decoded frame, which is exactly what failed.)
    Fully decoupled: skipping or failing this leaves every dedup/merge result identical;
    backfillable later.
11. **Deletion detection (every completed scan of a reachable root — not just `--full`).**
    Reconcile files removed from disk since last scan. This needs **no re-hashing**: enumeration
    (Phase 1 step 2) walks the whole tree on *every* scan, and every present file has its
    `file_instances.last_seen_at` bumped this pass (step 4 fast-path or step 9). So gone files are
    simply the rows this scan never touched:
    - `DELETE FROM file_instances WHERE root_id=? AND last_seen_at < <this scan's start time>`
      **AND the instance's parent directory was cleanly enumerated this pass** (see guard) — i.e.
      any instance under a fully-listed directory not seen this pass → **delete the row**.
    - Then for each affected asset: if it is `active` and now has **zero** instances anywhere →
      **delete the asset** (cascading `phash`/`vphash`/`embeddings`/`similarity_edges`) — it is
      forgotten, not remembered as missing (§6: a plain filesystem delete is not trash). A
      `trashed` asset at zero instances is left intact (trash memory).
    On a `--full` scan, additionally **write** `roots.last_full_scan_at`.
    (`--full` governs re-*hashing* via the fast-path bypass; it does **not** govern deletion
    detection, which keys off enumeration + `last_seen_at` and therefore runs on incremental scans
    too.)
    **Guard (per-directory, §10.1):** reconcile an instance only if its **containing directory was
    fully and cleanly enumerated this pass**. Skip (leave untouched, report) instances under any
    directory whose listing errored/timed out, and under a fully offline/unreadable root skip
    everything — so incomplete data is never mistaken for "files deleted," and one flaky folder on a
    large NAS root no longer disables reconciliation for the whole root (only that subtree). Track
    the cleanly-enumerated directory set in Phase 1. (Separately, a root under an open review/merge
    never reaches this step at all — step 1a refuses to scan it — so deletion-detection cannot churn
    an active plan's referenced rows; that is a distinct reason to skip.)
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
    **Nothing on disk changed.** (The user-facing banner phrases these as `N new`, `N exact-dup
    instances`, `N filled in missing fingerprints`, `N identified trash`, `N undecodable`, etc.)
15. **Persist the report (§4 `scan_results` / `scan_problem_files`).** After the per-root loop (so
    a *completed* scan only — dry-run/cancel/interrupt/error persist nothing), write one
    `scan_results` row per root scanned: the banner counts + flags + (if `--profile`) the profiler
    snapshot, plus a `scan_problem_files` row per problematic file (path + reason). This lets
    `status <root>` and the M6 TUI re-render the scan later. **The undecodable set is re-derived
    from the catalog here, not taken from this pass's activity** (§4 scan_results note): a resume /
    incremental re-run fast-path-skips undecodables (step 4), so a per-pass count would wrongly read
    as zero on re-run — reading committed `assets.undecodable=1` (with a live instance in the root)
    instead makes the report describe the root's *current* state, stable across resumes. `read-error`
    files (unreadable bytes, no asset) stay per-pass. Persist tolerates a closed DB on shutdown (like
    the worker progress writes) so a stop at the finish line can't flip a `done` scan to `error`.

**Idempotency & resume:** re-running `scan` on the same root is a no-op except for genuinely
new/changed files — the **fast-path** (step 4) skips the rest, which is what makes an interrupted
scan effectively resume: already-persisted files are cheap no-ops on the next pass, so the work
picks up where it stopped without any explicit cursor. (`jobs.done` is only the progress number,
§4 — not the resume key.) If the daemon died mid-scan, startup reconciliation flips the stale
`running` row to `interrupted` (§3); the next `scan` (manual or scheduled) then continues via the
fast-path. Re-running `roots register` on an existing root is rejected by the overlap check.

---

### B. Dedup a single registered folder

`dedup` **targets one registered folder** (root) at a time and stages its removable duplicates
as **Windows shortcuts** inside that folder, for the user to review in Explorer and then confirm.
It works from the fingerprints scan already stored in the DB (hashes, `phash`/`vphash`). Comparison
spans all **active** assets across the whole collection: an asset in the target folder is judged
against active copies in *external* registered folders too. **Trashed assets are excluded** — dedup
only collapses copies of things you're keeping; trash exclusion is `merge`/`cleanup`'s job (§6).

**A dedup run is a fixed three-stage sequence, presented one stage at a time** so review stays
focused and each folder means one thing. Each stage stages its own kind of duplicate into its own
folder, you review it in Explorer, and `--confirm` applies *that stage* and then automatically
advances to the next non-empty stage. The stages, in order:

| # | Stage folder (`_packrat_review\…`) | What it stages | Default if you do nothing | To change a file's fate | Media |
|---|---|---|---|---|---|
| 1 | `_exact_dup_to_delete\` | byte-identical copies (an asset's redundant instances; a survivor is always kept) | **DELETE** | **remove** the shortcut to **spare** | photo + video |
| 2 | `_suspect_recompression\` | near-dups within the tight band — recompressed / resized / re-encoded copies (`d ≤ t_photo_recompress`; video: a matched frame-vote). packrat marks the least-compressed member `_suggested` as a keep-hint (photo *and* video, step 9) | **KEEP** | **remove** the shortcut to **delete** | photo + video |
| 3 | `_with_minor_edits\` | near-dups in the wider band — minor edits/crops/borders (`t_photo_recompress < d ≤ t_photo_edit`) | **KEEP** | **remove** the shortcut to **delete** | **photo only** |

The **naming carries the safety signal**: only the default-DELETE folder is named `…_to_delete`;
the two default-KEEP folders are content-named. Rationale: exact dups are objectively redundant
(default-delete, veto to keep); near-dups need human judgment (default-keep, remove to delete). Two
distance cutoffs (`t_photo_recompress < t_photo_edit`, §5.3/§9.2) split photo near-dups into the
two review bands so you can blaze through the near-certain recompressions in stage 2 and scrutinize
the genuine edits in stage 3. **Video near-dups are a single frame-vote match** (a match score, not
a recompress-vs-edit split), so they all land in **stage 2** and there is no stage-3 video.

**Why sequential stages (not the old two-folder-at-once design).** Presenting exact + perceptual
folders simultaneously forced a hard rule — an asset with a planned exact deletion was **excluded
from perceptual grouping** and its near-dup **deferred to a later `dedup` run** (so no asset could
appear in two opposite-convention folders at once). Sequencing dissolves that: stage 1 resolves
exact dups first (deleting only *redundant instances*, never removing an *asset*), so by stages 2–3
every asset still exists and can be matched perceptually **in the same run** — the deferral and its
edge-case-6 exclusion are gone. It also means **survivors exist only in stage 1**: stages 2–3 stage
*distinct assets* with no survivor concept (deleting a near-dup member never threatens another
asset's last copy), which is what makes their apply path simple.

**Liveness is verified lazily, not eagerly.** Stale DB rows (a file moved/deleted in Explorer since
the last scan) are rare, and `stat()`-ing every candidate up front — especially external copies on a
cold/sleeping drive — is mostly wasted work. So there is **no eager stat**; liveness is checked only
where a stage acts, stat'ing only the files it is about to touch:
- **At shortcut creation (per stage):** stat each planned target right before writing its `.lnk`, so
  **no broken shortcut is ever created** — a vanished target is skipped and its DB row lazily cleaned.
- **At delete (per stage `--confirm`):** re-stat immediately before the irreversible move — the
  authoritative gate (a file may have changed again since staging).

Any divergence resolves toward **sparing**: a gone file is not staged / not deleted; if an exact
survivor turns out gone, its redundant copies are spared (and one is promoted). The pipeline only
ever acts on *fewer* files than the DB preview implied, never more. *(edge case 5)*

```
packrat dedup "D:\Backup\iPhone"            # analyze → stage 1 → pause (pending, stage 1)
packrat dedup "D:\Backup\iPhone" --confirm  # apply the current stage, auto-advance to the next
                                            #   non-empty stage (pending); after stage 3 → completed
packrat dedup "D:\Backup\iPhone" --cancel   # discard the whole run's staging, delete nothing
packrat dedup "D:\Backup\iPhone" --dry-run  # compute all 3 stages read-only; stage/write nothing
# (per-root dedup/review state — including the current stage — is shown by `packrat status`, §11)
```

Terminology: **target folder** = the root passed to `dedup`. **External folder** = any *other*
registered root. **Survivor** = the one file instance of an asset that stage 1 keeps.

#### Dedup state machine (one run per folder, a stage cursor within it)
- **A single `review_runs` row spans the whole 3-stage sequence.** It carries `status`
  (`pending` → `completed`/`cancelled`), a **`stage`** cursor (1→3) and a **`stage_phase`**
  (`staged` = shortcuts written, awaiting the user; `applied` = this stage's deletions done, next
  stage not yet staged) — the two new columns (§4). It stays `pending` across all three stages; only
  applying the **last non-empty** stage flips it to `completed`. The **partial unique index still
  enforces at most one `pending` run per `root_id`**, so a second `dedup <folder>` while a run is
  open errors and tells you to `--confirm` through it or `--cancel` it.
- Each stage's plan is persisted to `review_actions` (tagged with its `stage`) **when that stage is
  staged**, so every `--confirm` is deterministic and crash-safe: it re-reads which shortcuts you
  kept/removed and never re-decides. Because the asset set is stable after stage 1, later stages are
  computed **lazily at the moment they're staged** (right after the prior stage applies), not all up
  front — except `--dry-run`, which computes all three read-only for the preview.

---

#### B1. `packrat dedup <folder>` — analyze & stage the first stage (produces `pending`)

**Phase 0 — Validate & lock**
1. Resolve `<folder>` to a registered root; it must be a **library** root (error otherwise). → **read** `roots`.
2. **Per-root exclusivity (§3 guarantee 2).** If this root already has an active operation — a
   `pending` `review_runs` row (another dedup or a cleanup), or an in-flight merge (an open
   `merge_runs` row (`status IN ('planning','copying')`) with `dest_root_id` = this root, §4) — this
   analyze does not run against it: it is **held in the queue (dequeue gate, §3) until the holder
   clears**, acquiring ownership only when it actually runs (opening its `review_runs` row below).
   (A concurrent *scan* can't coexist either: scan's step 1a holds it behind this run's `review_runs`
   row, and the global single-worker slot never runs a scan and this analyze at the same instant.)
   Then compute stage 1 (Phase 2). If the whole run would be empty (no stage has
   any candidate) it **auto-completes "already clean"** without leaving a `pending` row dangling.
   Otherwise **write** a `review_runs` row (`root_id`, `status='pending'`, `stage=1`,
   `stage_phase='staged'`, `created_at`) — which now *owns* the root until confirmed/cancelled — and
   open a `jobs` row (`type='dedup'`).

**Phase 1 — Build from the DB (no eager stat)** *(edge case 5)*
Analyze builds the plan directly from existing `file_instances`/`phash`/`vphash` rows; it does
**not** stat files. It recommends a fresh `scan <folder>` first if `last_full_scan_at` is old (scan
already stats the folder, making internal liveness current for free) but does not force it. External
copies are trusted as live; if one turns out gone, the per-stage shortcut-creation and confirm
checks catch it and spare the internal copies (worst case: the preview offers slightly more than
confirm deletes). → No writes/stats here; lazy DB cleanup happens as broken targets are encountered.

**Phase 2 — Stage 1: exact-duplicate resolution** *(byte-identical = same asset)*
For each **active** asset with ≥1 live instance **in the target folder**:
3. **Exact dup with an external folder** → the external copy is byte-identical, so **all** of the
   target folder's instances are redundant. Plan every target-folder instance for deletion
   (`kind='exact'`, `reason='exact-external'`, survivor = the external instance). Keep nothing locally.
4. **Else, exact dups within the target folder** (≥2 live instances, all in this root) → keep the
   **oldest `mtime`** (tiebreak: stable by path), plan the rest for deletion (`kind='exact'`,
   `reason='exact-internal'`, survivor = the kept instance).
5. **Else** (single live instance, no external copy) → a survivor; nothing to delete.
   → Stage-1 deletions are **written** to `review_actions` (`stage=1`, `folder='exact_dup_to_delete'`,
   `default_action='delete'`, target instance/asset/path, survivor reference).

**Phase 3 — The perceptual stages (2 and 3) — computed when each is staged**
Perceptual stages are the **§5 matching engine**: run the PDQ / video-frame matcher for the target
folder's assets against all **active** assets collection-wide (**trashed excluded** — §5), **upsert**
the results into `similarity_edges` (dedup is that table's writer — §4/§8 division of labor), and
build clusters from the edges. Pure DB + fingerprint math, no file I/O. Video matching **pre-filters
by duration** (`|d₁−d₂| ≤ max(duration_tol_s, duration_tol_pct%·min)`, §5.3) to avoid the all-pairs
blowup.
6. **Which edges belong to which stage** (photo, by PDQ distance `d`):
   - **Stage 2 (`_suspect_recompression`)** — `d ≤ t_photo_recompress` (the tight band), **plus every
     video near-dup match** (video is a single frame-vote match, so all video pairs go here).
   - **Stage 3 (`_with_minor_edits`)** — `t_photo_recompress < d ≤ t_photo_edit` (photo only). A pair
     already in stage 2's band is **not** re-shown in stage 3.
7. **No cross-stage exclusion, no deferral.** Because stage 1 deleted only *redundant instances* and
   never removed an *asset*, an asset can legitimately appear in stage 1 (a copy deleted) **and** a
   later stage (it's a near-dup of something else) — both in the same run. There is **no**
   edge-case-6 asset-level exclusion and **no** "run it again to see the group" deferral anymore.
   (Between staging stage 2 and stage 3, exclude any pair already offered in stage 2 — see step 6 —
   so a spared recompression isn't nagged again as an "edit".)
8. **Edges are always (re)computed for this run, never reused as complete input.** `similarity_edges`
   stores only *matches*, not "compared, no match," so it cannot distinguish "no near-dups" from
   "never compared" (e.g. an asset scanned/backfilled after the cache was built) — trusting it would
   **silently miss** those, a recall loss the user can't see (against the recall-first tenet, cf.
   §5.3). The matcher is pure DB/CPU and runs in seconds–low-minutes (§5.4), so recomputing is cheap
   and always correct. The upsert persists the run's edges as a queryable record (forensics / the
   §8.1 audit); it is deliberately **not** surfaced as a headline "duplicates (est)" TUI stat, since
   as a per-run cache it is 0 before any dedup and stale after later scans (§12 Collection stats).
9. For each cluster of size ≥2 in a stage, assign a 4-digit `group_no` and each member a 4-digit
   `member_no`; plan a shortcut `group{NNNN}_{MMMM}.lnk`, with an `_external` suffix when the
   member's live file is in an external folder. Each member is represented by its single surviving
   instance (target-folder if present, else external). → **write** `review_actions`
   (`stage=2|3`, `folder='suspect_recompression'|'with_minor_edits'`, `kind='perceptual'`,
   `default_action='keep'`, `group_no`, `member_no`, target instance/asset/path, `is_external`,
   `distance`). **Perceptual actions carry no survivor** (`survivor_instance_id` NULL) — near-dup
   members are distinct assets.
   - **Stage-2 keep-lead (annotate-only).** In **stage 2** the members of a group are essentially the
     same content at differing compression (the tight `t_photo_recompress` band / the video frame-vote
     → almost no visible difference), so packrat **suggests which copy to keep** — the least-compressed
     one — by marking the winner's shortcut **`_suggested`** (`group{NNNN}_{MMMM}_suggested.lnk`,
     combined with `_external` if applicable). A group is homogeneous (a photo never matches a video),
     so the group's medium picks the ranking key; both lead with **resolution** (`width·height`) — a
     downscaled re-export loses outright:
     - **Photo:** resolution → **format rank** → file `size` → stable path. **Format rank** is a
       3-level ordinal (best first): lossless/original (`png`/`tif`/`tiff`/`bmp`/RAW) >
       **efficient-lossy** (`heic`/`heif`/`avif`) > other-lossy (`jpg`/`webp`/`gif`/…). It is the
       primary quality signal after resolution: at equal resolution a lossless copy is the master,
       and among lossy copies a modern codec packs more real detail per byte than JPEG, so an iPhone
       HEIC original outranks its JPEG export. Then, **within one format**, the larger file `size`
       wins — at fixed resolution+format the encoder's output size *is* the quality dial, so size is a
       clean monotonic quality proxy there. `size` is used only *within* a format because it **lies
       across** formats (an efficient HEIC master is smaller than a bloated JPEG export) — which is
       exactly what the format rank above it handles. *Accepted cost:* a genuinely low-quality HEIC
       outranks a high-quality JPEG of the same scene, and a JPEG re-wrapped as HEIC beats its source
       — rare (HEIC is the original on iPhone), advisory-only (never deletes; overridable in review).
       *(An earlier residual-entropy `detail_score` signal was tried and dropped: it cost ~40% of
       scan CPU, and once banded to tame its high-quality-JPEG noise it only ever agreed with `size`
       within a format — so `size` alone is simpler and equivalent. See §14.)*
     - **Video:** resolution → **effective-bitrate band** → **codec-efficiency weight** → stable path.
       Effective bitrate = `size / duration_s × codec_weight` (`match.codec_weights`, §9.2): a
       more-efficient codec's bits are worth more, so an HEVC master beats an H.264 re-export at equal
       resolution+quality. Dividing by `duration_s` removes the length bias within the duration
       tolerance (a slightly-longer clip at equal quality has a bigger file, not more detail). Two
       effective bitrates within `match.video_bitrate_tie_pct` (default 10%) share a **log-scale band**
       (a "tie"), so the codec weight then the path decide — not a coin-flip on a noisy diff.
       *Accepted caveat:* bitrate lies **across codecs** (HEVC is ~2× H.264-efficient), which the
       weight *reduces* but doesn't cure — surfaced in the manifest (codec + bitrate shown) for
       hand-override, not solved. No `duration_s`/`codec` → falls back to raw size / weight 1.0.
     **This is a hint by default:** the stage stays default-**KEEP** (you still delete a member by
     removing its shortcut); the marker itself never deletes anything and never changes a default.
     **Stage 3 (minor edits) is deliberately NOT ranked** — the *edited* copy may be the one you want
     to keep. → `is_lead` + `lead_reason` (the decision level, below) recorded in the plan; surfaced in
     `manifest.csv` (`suggested_lead`, `suggested_reason`, `media_type`, `width`, `height`, `size`,
     `duration_s`, `codec`, `bitrate` columns) + `proposed.json`.
     - **Keep-lead pick stats (reported at staging).** When stage 2 is staged, the report logs *how*
       each group's lead was decided — a tally over the ranking key's decision levels
       (photo: `resolution` / `resolution + format` / `resolution + format + size`; video: the
       bitrate/codec analogues; `path tiebreak` when every key component tied). This exposes how much
       of the collection the lead rests on resolution alone vs. the finer format/size calls, so the
       suggestion's confidence is visible before you act on it.
     - **`--confirm --keep-suggested` (stage 2 only): act on the suggestion in bulk.** Instead of
       reviewing shortcut-by-shortcut, this **keeps only each group's `_suggested` lead and deletes
       every other member, ignoring your shortcut edits for the stage**. It is the "I trust packrat's
       pick" shortcut. **Safety:** a group with **no** suggested lead (an all-external group, or a lead
       whose `.lnk` failed to stage) is **fully spared** — it never deletes every copy of an asset
       because packrat couldn't name a keeper. Rejected on stage 1 / stage 3 (no leads there). Deleted
       non-leads follow the normal perceptual-deletion path (asset → `trashed`/`dedup-perceptual` at
       zero instances, §Phase 6). Only stage 2 is affected; the run then advances normally.

**Phase 4 — Materialize the current stage's staging folder** *(edge case 5)*
Create the current stage's folder under `<root>\_packrat_review\` (already in the ignore set, so
scan never indexes it or the `.lnk`s). Analyze materializes **stage 1**; `--confirm` materializes the
next stage after applying the current one (Phase 6). Per staged action:
10. **Stat-before-create — never emit a broken `.lnk`.** `stat()` the target at the instant of
    creating its shortcut:
    - **Target present** → create the `.lnk` (this also finalizes `is_external` / the `_external`
      suffix from the live path).
    - **Target gone** → **skip** the shortcut, lazily clean the DB (delete the gone `file_instances`
      row; if an `active` asset hits zero instances → delete the asset, cascading fingerprints), and
      **do not persist** a `review_actions` row for it — count "skipped-at-staging". *(Never persist a
      row whose shortcut isn't on disk: in a default-KEEP stage `--confirm` reads an absent shortcut as
      "delete", so a phantom row would silently delete an unreviewed file.)*
    - **Survivor-gone special case (stage 1 only):** if an exact target is present but its planned
      **survivor** has vanished, do **not** stage the target — **promote it to survivor** (redirect the
      asset's other exact deletions at it) and skip its shortcut. Same promotion as the Phase-6 gate
      (step 17b), applied early. *(Stages 2–3 have no survivors, so this case can't arise there.)*
    Net: **every `.lnk` that lands resolves to a real file** and previews correctly.
11. Write a **`manifest.csv`** in the stage folder — a flat export of that stage's `review_actions`
    so the opaque `.lnk`s are legible (a documentation sidecar; `--confirm` reads shortcut presence,
    **not** the manifest). Columns:
    - `_exact_dup_to_delete\manifest.csv`: `shortcut, target_path, asset_id, reason, survivor_path`
    - `_suspect_recompression\` / `_with_minor_edits\manifest.csv`:
      `shortcut, target_path, asset_id, group_no, member_no, suggested_lead, suggested_reason,
      media_type, width, height, size, duration_s, codec, bitrate, is_external, distance, quality,
      low_confidence` — `suggested_lead`=`1` on the keep-hint member (stage 2 only, step 9), and
      `suggested_reason` names *why that member won* (the ranking-key decision level — e.g.
      `resolution + format` — filled only on the lead row, blank otherwise); the
      `media_type`/`width`/`height`/`size`/`duration_s`/`codec`/`bitrate` columns are
      the ranking inputs (so a surprising lead is explainable at a glance — e.g. a HEIC-vs-JPEG or
      HEVC-vs-H.264 call); `quality` is the member's PDQ quality (0–100; video: min across comparable frames);
      `low_confidence`=`1` when this member or its partner is below `review.low_quality_hint` (a
      flat/near-black spurious-collision hint to skip fast, §5.3).
12. **Audit trail (capture point 1 — the proposed plan).** Write/append an immutable `proposed.json`
    in this run's audit dir (§8.1): the full plan **for every stage as calculated**, each action with
    its stage, target path, reason, survivor, group/member, distance, per-member `quality` and
    `low_confidence`, plus skipped/spared counts and the thresholds in effect (`t_photo_recompress`,
    `t_photo_edit`, `t_match_video`, the `video.*` knobs, `review.low_quality_hint`). During
    `--dry-run` this is the whole preview; during a live run it records the plan as each stage is
    computed. Immutable, outside the folder, unlike the in-folder `manifest.csv` (deleted at finalize).
13. Open the stage folder in Explorer (or its `_packrat_review\` parent), print the `--confirm` /
    `--cancel` commands **naming the current stage**, and **pause** (`review_runs.status='pending'`,
    `stage_phase='staged'`). If the current stage staged nothing (all targets gone), auto-advance to
    the next non-empty stage instead of pausing; if none remain, auto-complete "already clean".

**The conventions differ by stage — read carefully:**
| Stage folder | Default if you do nothing | To change a file's fate |
|---|---|---|
| `_exact_dup_to_delete\` | the real file **is deleted** | **remove** its shortcut to **spare** the file |
| `_suspect_recompression\` | the real file **is kept** | **remove** its shortcut to **delete** the file |
| `_with_minor_edits\` | the real file **is kept** | **remove** its shortcut to **delete** the file |

**Reviewing = deleting shortcuts, not renaming them.** Matching is strict on the planned filename, so
a *renamed* shortcut counts as removed (Phase 5). In the default-KEEP stages that means an accidental
rename would delete the target — the typed `--confirm` summary lists every such file (per root) so it
can't happen silently.

---

#### B2. `packrat dedup <folder> --confirm` — apply the current stage, advance (→ `completed` after stage 3)

**Phase 5 — Read the user's edits**
14. Load the `pending` run and its current stage's `review_actions`. **No pending run** → error
    ("nothing to confirm; run `dedup <folder>` first"); same for `--cancel`. A `completed`/`cancelled`
    run is terminal — re-`--confirm` is a no-op error.
15. **Safety guard:** if the current stage's staging folder is *missing* (user deleted the whole
    folder), **abort** — never read "folder gone" as "delete all" (mass data loss in a default-KEEP
    stage). Require the folder to exist to be read.
16. For each of the stage's planned actions, check whether a file with **its exact planned shortcut
    name** still exists in the folder (strict, filename-only — the manifest is not consulted):
    - `_exact_dup_to_delete`: shortcut **present** → intend delete; **absent/renamed** → spare (veto).
    - `_suspect_recompression` / `_with_minor_edits`: shortcut **absent/renamed** → intend delete the
      target; **present** → keep.
    A renamed shortcut counts as **removed**; extra files dropped in are ignored (only planned names
    consulted). This yields the *intended* delete set; liveness is applied per-file in Phase 6.
    - **`--keep-suggested` override (stage 2 only):** skip the shortcut-presence read entirely and
      derive the intended set from the plan — delete every member **except** each group's
      `_suggested` lead, regardless of what shortcuts the user added/removed. A group with no
      `_suggested` lead is spared whole (never delete every copy because no keeper was named).
      Rejected outside stage 2 (stages 1/3 have no leads). Phase 6 liveness still applies.

**Phase 6 — Authoritative liveness + apply this stage's deletions** (backup DB first) *(edge case 5)*
The authoritative gate — done lazily, one target at a time, right before the irreversible move.
17. Print a summary for **this stage** grouped by target root — **including any external-folder files**
    a default-KEEP-stage shortcut removal would delete — and require typed confirmation. **Flag
    non-recyclable paths:** count and call out files on network/SMB roots, deleted **permanently** (no
    Recycle Bin — §10), e.g. "K of N are on network shares → permanent."
18. For each file in the intended delete set, at the moment of deletion:
    a. **`stat()` the target.** Gone already → nothing to delete; lazily clean the DB (delete the gone
       `file_instances` row; an `active` asset at zero instances → delete the asset, cascading
       fingerprints) — count "already-gone". Present → proceed.
    b. **Stage 1 only — verify the survivor is still live** before deleting (guarantees an asset never
       loses its last copy): `stat()` the `survivor_instance_id` path. **Live** → delete the target.
       **Gone** → the target is no longer redundant: **spare it** and **promote it to survivor**
       (redirect the asset's remaining exact deletions at it), lazily delete the vanished survivor's
       row, log "spared: survivor vanished (promoted)". *(Stages 2–3 have no survivor step.)*
    c. Move the (still-present, still-redundant) file to the **Recycle Bin** (recoverable locally;
       **permanent on NAS/SMB** — §10), then update the DB:
       - **Stage 1 (exact)** → delete that redundant `file_instances` row. The asset keeps its survivor,
         so it **stays `active`** — never trashed. No re-appearance concern.
       - **Stages 2–3 (perceptual)** → the user deliberately discarded a near-dup. Delete its
         `file_instances` row; if the asset now has zero instances → **write** `assets.status='trashed'`,
         `trashed_at`, `trash_reason='dedup-perceptual'`, **retain its fingerprints** (the one path where
         an asset survives at zero instances) so a future merge/dedup excludes this near-dup (§6 trash
         memory).

**Phase 7 — Apply-then-advance / finalize** *(the one new crash window — resumable via `stage_phase`)*
`--confirm` applies the current stage and then stages the next, as **two committed steps** (like
merge's copied→registered gap):
19. **Commit "applied".** After Phase 6's deletions commit, set `review_runs.stage_phase='applied'`
    (still `pending`, same `stage`). *(A crash here leaves `applied` with nothing staged — reconcile
    must **not** roll this back, the deletions were correct; re-running `--confirm` sees `applied` and
    jumps straight to step 20.)*
20. **Stage the next non-empty stage** (§Phase 3–4): compute stage `stage+1` (then `+2` if it's
    empty), materialize its folder + `review_actions` + append to `proposed.json`, and commit
    `stage=<next>`, `stage_phase='staged'`. Then **pause** with the next stage's `--confirm`/`--cancel`
    prompt. If **no non-empty stage remains**, instead finalize:
21. **Audit + finalize.** Write the immutable `applied.json` (§8.1): the final disposition of every
    action across all applied stages — `deleted` / `spared` / `kept` / `already-gone` /
    `survivor-vanished`, with path, root, asset_id, Recycle-Bin destination, stage — plus totals and
    `confirmed_at`. Delete all of this run's stage folders (shortcuts + manifests), leaving the shared
    `_packrat_review\` parent. → **write** `review_runs.status='completed'`, `confirmed_at`; close the
    `jobs` row. Report per-stage: exact deleted, perceptual deleted (stage 2/3), spared/kept, external
    deleted, plus lazily-cleaned stale rows. **`--cancel`** (any stage) deletes **all** the run's stage
    folders, marks the run `cancelled`, deletes nothing, and still writes `applied.json` (every action
    `cancelled`). *(A run cancelled mid-sequence keeps whatever earlier stages already deleted — those
    were confirmed — and its `similarity_edges` rows, which are a cache never trusted as complete input.)*

**Cross-folder note:** a perceptual member can live in an external folder (`_external` shortcut) — a
near-dup of a target-folder asset that physically resides only in another root. Removing that shortcut
deletes a file in *another* root — the Phase 6 typed-confirm summary (step 17) calls this out per-root
so it is never accidental.

**Why dedup is DB-first with lazy liveness:** the *decision* work is pure DB comparison — no eager
whole-pool stat. It stats a file only when a stage acts on it: once creating that file's shortcut
(no broken `.lnk`) and once immediately before deleting it (the authoritative gate). **Merge (§C)** is
unrelated to this machinery: it hashes transient source files and classifies them by exact hash — no
perceptual signatures, no `similarity_edges`, no shortcuts.

#### 8.1 Review-run audit trail (dedup & perceptual-cleanup)

Every stateful review run — `dedup` **and** `cleanup --trash-perceptual` — leaves a permanent,
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
- **Retention:** governed by `audit.retention_days` (§9.2); default **0 = keep forever** (small
  text files). Setting it >0 prunes audits older than N days — the pruning *pass* itself is a
  deferred nicety (§14 #5), but the knob and its default live in `config.toml` now.
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
   so that copied files become catalogued members of the collection. If `dest` is under no library
   root → error (offer to `roots register` it first). Reject if `source` and `dest` overlap. **Ignored
   dest (warn, don't block):** if the resolved `dest` itself falls under the root's ignore rules
   (allowlist/`--ignore` globs, §8 A1), do **not** hard-error — files still copy, but they will be
   left *uncatalogued* (Phase 3 step 11) and merge warns per ignored subpath (Phase 4 step 13),
   because registering under an ignored path would let the next scan silently forget them. A plain
   note here is enough; the loud warning is at report time once the exact count is known.
2a. **Per-root exclusivity (§3 guarantee 2), on the dest root.** If the dest library root already
   has an active operation — a `pending` `review_runs` row (dedup/cleanup) or another open
   `merge_runs` (its own partial-unique index enforces the latter, §4) — the merge job is **held in
   the queue (dequeue gate, §3) until that holder clears**, not run against it: a merge stages/copies
   under this root and its step-4 opportunistic scan would churn it, so it must own the root cleanly
   before proceeding, and it acquires that ownership only when it actually runs (opening `merge_runs`
   at step 5). (A `--dry-run` merge opens no run and writes nothing — but it also then skips step 4's
   scan, see below.)
3. **Refresh the trash collection** (§6.1) — absorb any files sitting in the registered trash
   roots into the trashed-hash set and empty those folders. Merge discards incoming files that
   match a trashed hash, so the trashed set must be current first. (Runs for real even under
   `--dry-run` — see below.)
4. Opportunistically fast-path-scan the `dest` root so the comparison set is current; warn if
   the collection index is stale. (This runs under merge's ownership from step 2a — no other op can
   touch the root meanwhile. Skipped under `--dry-run`, which must not mutate the catalog.)
5. Open a `jobs` row (`type='merge'`) and a **`merge_runs`** header (`status='planning'`,
   `dest_root_id`). The `merge_runs` row is the durable **cross-op guard**: its
   partial-unique `(dest_root_id) WHERE status IN ('planning','copying')` is exactly the
   "in-flight merge plan targeting this root" that dedup (§8 B Phase 0) and cleanup (§6.2) wait
   behind (dequeue gate, §3). **Dry-run opens neither `merge_runs` nor `merge_plan_items`** — it must
   not trip that guard and has no resume need. This plan is internal crash-safety only — merge does not pause
   for the user.

**Phase 1 — Fingerprint source** (read-only w.r.t. source; writes only the frozen plan)
6. Enumerate source media files (same allowlist/ignore rules as scan).
7. For each: **BLAKE3 only** — no metadata, no perceptual signature, no embedding (classification
   in Phase 2 needs the exact hash alone). No `assets`/`file_instances`/`phash` rows are written —
   source files are not collection members. Persist each file as a `merge_plan_items` row
   (`source_rel_path`, `size`, `mtime`, `content_hash`, `progress='pending'`) so an interrupted
   run resumes **without re-hashing the source** (the dominant SMB cost, §10.1). *(Metadata —
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
    ignore set** (the same allowlist + `--ignore` globs bound to the root, §8 A1), evaluated on the
    path *relative to the root* (not to `<dest>`), because that is exactly what a later `scan` will
    test. Two outcomes:
    - **Dest path is NOT ignored (the normal case)** → **write** `assets` (`status='active'`, hash
      from Phase 1 + metadata **probed now**, `new` reps only) and `file_instances` (pointing at the
      copied `dest` path), and set `merge_plan_items.progress='registered'`, all **in one
      transaction**. Register is idempotent — `assets` keyed by unique `content_hash`,
      `file_instances` by (`root_id`,`path`) (§4) — so replaying a partially-done file is safe.
      Perceptual signatures are **not** computed here; a later `scan`/`dedup` of `dest` fills in
      `phash`/`vphash` (and `scan --embed` the embedding). It is now a collection member, so a future
      merge recognizes it.
    - **Dest path IS ignored** → **do NOT register** (write no `assets`/`file_instances` row); set
      `progress='copied-unindexed'` and record the ignored dest path. **Rationale (this is the fix
      for the silent-forget bug):** if we registered a file living under an ignored path, the next
      `scan` would not enumerate it → its `last_seen_at` would never bump → deletion-detection
      (§8 A2 Phase 3 step 11) would delete its `file_instances` row and **forget the asset while the
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
    crash→resume window (the worker slot frees on crash, and a plain `scan` isn't blocked, §3), a
    `new` file still copies — producing a redundant *byte-identical* instance, not corruption.
    `dedup <dest>` collapses it later. This is the deliberate cost of deterministic resume that
    never re-reads source bytes.
- **Finalize:** on completion set `merge_runs.status='done'`, `finished_at`; the run and its
  items are **retained** as queryable merge history (§14 #5).
- **Interruption (two paths — merge has no interactive pause and no `--cancel` flag):**
  - **Cooperative cancel** — the *generic* job cancel (§9) via the TUI `[c]` (§12) or another
    terminal; **not** Ctrl-C (which only detaches the view, §11) and **not** a merge-specific
    `--cancel` (that's a dedup/cleanup review verb). The worker sees the flag at its next
    per-file checkpoint, sets `merge_runs.status='cancelled'`, and stops. Already-copied files
    stay — merge is copy-only, so a partial copy leaves nothing unsafe; those files are now real
    collection members. Re-running `merge` does **not** auto-resume a `cancelled` run (it's a
    deliberate stop); it starts a fresh plan.
  - **Process death or clean `daemon stop`** (crash / reboot / power loss / graceful shutdown) —
    the run is left open (`planning`/`copying`) and its `jobs` row is reconciled to `interrupted`
    on next daemon start (§3), **not** `cancelled`; re-running `merge <source> --into <dest>`
    silently auto-resumes it per above. (This is why a stop/crash differs from a cancel: only the
    explicit cancel above discards the plan.)
- `--dry-run` runs Phases 1–2 logic **in memory only** and prints the classification counts /
  would-copy list — it opens **no** `merge_runs`/`merge_plan_items` rows (so it neither trips the
  cross-op guard nor leaves a resumable run) and writes no asset rows. It **also computes the
  would-be-ignored destinations** (test each `new` file's projected dest rel-path against the dest
  root's ignore set) and prints the same per-subpath ignored-destination warning as Phase 4 step
  13 — so the user learns about an ignored `--into` target *before* copying, when it is still
  cheap to fix. **But Phase 0's "refresh the trash collection" still runs for real** — trash
  folders are absorbed and emptied even in dry-run (§6.1); only the copy and all plan/asset writes
  are skipped.
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
| Perceptual hash    | **pdqhash** only — 256-bit PDQ for both photos and video frames (§5.3). No `imagehash`/pHash anywhere. |
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
- **Perceptual hash** — `pdqhash`/PDQ on an RGB numpy array, for **both** photos (the still) and
  video (each sampled frame). Format-agnostic *given a decoded image*; one algorithm for both.
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
  hashing or CLIP — if a frame decodes, PDQ (photo still / video frame alike) and CLIP just run on
  the pixel array. This is why the matrix's last three columns mirror the decode column.
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

### 9.2 Configuration (`config.toml`)

All tunable knobs referenced throughout this plan live in **one** file:
**`%APPDATA%\packrat\config.toml`** — beside the daemon's existing `token` file (§3). TOML because
Python 3.11 parses it natively (`tomllib`, no dependency) and it matches the uv/`pyproject.toml`
world already in the stack (§9).

**Lifecycle:**
- **Auto-created with commented defaults.** On first daemon start, if the file is absent, the
  daemon writes it out fully populated — every key below at its default, each with a one-line
  comment. So the shipped defaults are always visible and editable, never hidden in code.
- **Hand-edited in v1.** There is **no `packrat config` command in v1** — you edit the TOML in a
  text editor. A `packrat config get/set` (with validation) is a deferred nicety (§14) and the
  file format is forward-compatible with it.
- **Re-read at each job start.** The daemon reloads `config.toml` when a job begins, so an edit
  applies to the **next** scan/dedup/merge/cleanup with no daemon restart. A job already running
  keeps the snapshot it started with — which is exactly the config the audit trail records "in
  effect" for that run (§8.1). A malformed file → the job is rejected with a parse error naming the
  bad key, and the daemon keeps serving read-only queries with the last-good config.
- **Missing keys fall back to the built-in default** (the file need not be exhaustive); **unknown
  keys are ignored with a logged warning** (forward-compat / typo signal).

**Scope — global only.** Every knob here is collection-wide. The one *per-root* setting is the
`--ignore` glob list, which is bound to each root at `roots register` time and stored on the `roots` row
(§8 A1), **not** in this file. (The `roots.ignore_globs` column and the deferred per-root scan
interval, §4, are the only per-root config; everything else is global.)

**The knobs (defaults are the shipped values):**

```toml
[allowlist]
# Media extensions that become assets (§8 A1). Photo + video are the fixed default set.
raw = false            # include the RAW group (dng cr2 cr3 nef arw raf orf rw2 pef srw); needs rawpy
# photo/video extension lists are editable here too, but default to the §8 A1 closed sets.

[fastpath]
mtime_tolerance_s = 2  # tolerant-mtime skip window (§8 A2 step 4); 0 = strict path+size+mtime

[match]
t_photo_recompress = 10   # photo PDQ cutoff for dedup stage 2 (recompression band, §5.3/§8 B)
t_photo_edit       = 32    # photo PDQ match cutoff (§5.3); recompress < d ≤ edit → stage 3 (minor edit)
t_match_video      = 90    # per-frame PDQ cutoff for video (§5.3); looser, the frame vote reclaims precision
pdq_max_edge       = 512   # downscale each image/frame to this longest edge before PDQ (~7x faster; 0 = full-res)
video_bitrate_tie_pct = 10.0  # video keep-lead (§8 B): effective-bitrates within this % tie → codec then path
# codec-efficiency weights for the video keep-lead effective bitrate (§8 B); unlisted codec → 1.0
[match.codec_weights]
h264 = 1.0
hevc = 2.0    # == h265 (same codec); ~2x more efficient than h264
av1  = 2.5
vp9  = 1.5
mpeg4 = 0.5

[video]
sample_frames        = 12    # frames sampled per video, at segment midpoints (§5.3)
duration_tol_s       = 1.0   # duration pre-filter: absolute floor (§5.3)
duration_tol_pct     = 5.0   # duration pre-filter: relative part (percent)
frame_match_fraction = 0.60  # ≥ this fraction of comparable frame-pairs must match
min_frame_quality    = 50    # PDQ quality gate; frames below are excluded from the vote
min_comparable_frames = 5    # fewer comparable pairs than this → no match (insufficient evidence)

[review]
low_quality_hint = 50  # photo PDQ quality below this flags a near-dup pair low_confidence (§5.3, annotate-only)

[smb]
scan_workers = 6       # concurrent hashing/decoding streams over SMB (§10.1); 4–8 typical

[audit]
retention_days = 0     # 0 = keep review audits forever (§8.1); >0 = prune older (deferred knob, §14 #5)
```

> **Defaults marked tuning-dependent** (`t_photo_recompress`, `t_photo_edit`, `t_match_video`, the
> `video.*` knobs, and the keep-lead `codec_weights` / `video_bitrate_tie_pct`) are **starting points
> to be calibrated on real data before the first full scan** (§5.3, §8 B, §14 #1) — not
> claimed-correct constants. `mtime_tolerance_s`, `allowlist.raw`, `smb.scan_workers`, and
> `review.low_quality_hint` are ordinary operational settings.

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

**Deletion target — Recycle Bin where it exists, permanent on NAS/SMB (accepted).** Every
"move to Recycle Bin" in this plan (dedup, cleanup, trash refresh — §6, §8 B/§6.2) means: attempt
the Windows Recycle Bin (via `send2trash`/`SHFileOperation`). **Windows provides no Recycle Bin
for network locations** (UNC / mapped-drive paths), and per §10.1 most roots live on the Synology
NAS — so for those files the delete is **permanent** (the shell either errors or hard-deletes).
This is **accepted for v1**, not worked around (no packrat-managed quarantine/`.recycle` folder):
- **Local roots** (NTFS on a fixed/USB disk) → real Recycle Bin, recoverable as the tenets imply.
- **NAS/SMB roots** → **permanent deletion.** The typed-confirmation gate (dedup/cleanup) and
  `--dry-run` are therefore the *real* safety net for network roots, and the tools say so at the
  confirm prompt: **the summary must warn when any file in the delete set is on a non-recyclable
  (network) path** — e.g. "N of M files are on a network share and will be deleted PERMANENTLY
  (no Recycle Bin)." Implementation: detect per-path whether a Recycle Bin is available (network
  vs. fixed volume) and count/flag network-path deletions in the confirm summary.
- Merge is unaffected — it is copy-only and never deletes from a root.
- The DB backup before every destructive op (above) is what makes the *catalog* recoverable
  regardless; the *files* on a NAS are not.

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
| `roots register` | none | trivial |
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
  fingerprints (§4). Rule: **an enumeration error/timeout suppresses deletion-detection only for
  the affected directory subtree, not the whole root** (fail-safe — never delete-and-forget on
  incomplete data). Because a `file_instances` row belongs to a specific directory (its path's
  parent), a gone instance is deleted **only if its containing directory was cleanly enumerated
  this pass**; instances under any directory that errored/timed out are left untouched. So one
  flaky folder on a large NAS root no longer disables deletion-detection for the entire root — only
  that subtree is skipped (and reported), while the cleanly-listed rest reconciles normally. A
  *fully* offline/unreadable root degenerates to the §4 whole-root guard (every directory failed →
  nothing reconciled), which this generalizes from "root offline" to "per-directory incomplete
  listing." **Implementation note:** track the set of cleanly-enumerated directories during Phase 1
  and scope the Phase-3 step-11 `DELETE … WHERE last_seen_at < start` to instances whose parent dir
  is in that set.
- **mtime stability.** The fast-path already tolerates small mtime jitter (§8 A2 step 4). A
  NAS-side reindex or an rsync that rewrites timestamps by more than the tolerance will force
  re-fingerprinting of those files — correct but costly; note it if you run such tools.

---

## 11. CLI surface (complete command reference)

Adding a folder is two commands (`roots register` then `scan`); `dedup` de-duplicates one folder via
Explorer shortcuts (analyze → `--confirm`); `merge` copies new files in (exact-hash, one shot);
trash is handled by `cleanup`, `trash refresh`, and `untrash` (§6). `status` (read-only) and `jobs`
(list / cancel / prioritize the work queue) surface runtime state; `daemon` manages the background
process. (Per design tenet §1.6, every command here is also reachable from the TUI, §12.)

**Shared client semantics** (all job-submitting commands — `scan`, `dedup`, `merge`, `cleanup`,
`trash refresh`, `untrash`, `scan --embed`): each **submits a job to the daemon** and streams its progress.
- **Ctrl-C detaches the view; the job keeps running in the daemon.** Re-attach or stop it via the
  `packrat` TUI, or from another terminal.
- **`--detach`** submits the job and returns immediately without streaming.
- **Every mutating submission is enqueued — nothing is rejected at submit** (`queued`, §3
  guarantee 1 / §4). One mutating operation *runs* at a time; the rest wait in the durable backlog
  and the worker dequeues the first *runnable* one (owned root free) on each pump. A foreground
  submit streams from `queued · waiting for worker` (or `queued · blocked: …`) into live progress;
  `--detach` returns the queued id at once. Read-only commands never queue and are never blocked.
- **Per-root exclusivity (§3 guarantee 2) is a *dequeue* gate, not a submit rejection:** a job whose
  *owned* root already has an active op — a `pending` dedup/cleanup review or an in-flight merge — is
  still enqueued, then **held in the backlog** (`blocked: root iPhone has a pending dedup —
  confirm/cancel to unblock`) and run automatically once the holder clears (the confirm/cancel/merge
  job pumps the queue). No command errors just because a root is busy — you can line work up behind a
  paused review and it drains when you resolve it. This includes `scan`: a manual `scan <root>` on an
  under-review root waits in the backlog; a `--all`/scheduled scan (owns no single root) skips it and
  logs the skip instead of parking the sweep.
- `packrat` with **no arguments** opens the TUI (logo, stats, live/recent jobs, operation menu).

**Root argument resolution — path vs. `--name` handle.** Commands that take a registered root
(`scan`, `dedup`, `cleanup`, and `merge --into`) accept **either** a filesystem path **or** a
root's `--name` handle. Resolution is unambiguous and order-independent:
1. If the argument, canonicalized as a path (§8 A1 step 1), exactly matches a root's stored `path`
   → that root.
2. Else, if it case-insensitively matches a root's `name` → that root.
3. Else → error ("no registered root at path or named `<arg>`"; suggest `packrat roots` to list).
A path never collides with a handle in practice (a handle is a bare label like `iPhone`, a path
contains separators/a drive), and path match is tried first so an odd handle can't shadow a real
path. `untrash <path>` is **excluded** — its argument is arbitrary bytes to hash, never a root
(§6.3).

### `packrat roots` — manage roots
The **noun for root lifecycle/metadata.** v1 subcommands: **`register`** (add) and **`list`**
(read). Removal/rename (`unregister`/`rename`) are deferred (§14 #9). Bare `packrat roots` is an
alias for `packrat roots list`.

#### `packrat roots register <path>` — declare a folder as a root
Metadata-only and instantaneous — walks nothing, fingerprints nothing. The root contributes to
dedup/merge only after a `scan`. The folder's leaf name must be globally unique across roots
(case-insensitive); override with `--name`.

```
packrat roots register <path> [options]

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

#### `packrat roots list` — list registered roots (read-only)
Each root's id, name, path, kind (`library`/`trash`), enabled, asset count, and last-scan recency.
Read-only, runs anytime (§3). `packrat roots` with no subcommand does the same.

```
packrat roots [list] [--json]
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
matches-trashed, embeddings computed vs deferred, plus any roots skipped for being under review).
Near-dup clustering is `dedup`'s job, not scan's. Resumable if interrupted.

Per-root exclusivity (§3): scan won't *run* on a root that has a pending dedup/cleanup review or an
in-flight merge — but a manual `scan <root>` is **enqueued and held** (shown `blocked: … —
confirm/cancel to unblock`), then runs automatically once you confirm/cancel the review (or the
merge finishes). It is no longer rejected at submit. A `--all`/scheduled scan owns no single root, so
it skips a held root and logs it rather than parking the whole sweep.
```

### `packrat dedup`
Dedup **one registered folder** as a **3-stage sequence** (§8 B), one stage staged + reviewed at a
time under `<root>\_packrat_review\`: **stage 1** `_exact_dup_to_delete\` (byte-identical copies,
default-DELETE) → **stage 2** `_suspect_recompression\` (recompressions + all video near-dups,
default-KEEP) → **stage 3** `_with_minor_edits\` (photo minor-edits/crops, default-KEEP). `--confirm`
applies the current stage (to Recycle Bin) and **auto-advances** to the next non-empty stage; after
the last it completes. Compares against all **active** assets collection-wide (internal + external
roots; trashed excluded). At most one `pending` run per folder (one run spans all three stages).

```
packrat dedup <folder>              # analyze → stage 1 → pending (stage 1)
packrat dedup <folder> --confirm    # apply current stage, auto-advance to next; last stage → completed
packrat dedup <folder> --confirm --keep-suggested  # stage 2: keep only each group's suggested lead
packrat dedup <folder> --cancel     # discard the whole run's staging, delete nothing → cancelled
packrat dedup <folder> --dry-run    # compute all 3 stages read-only; stage/write nothing
# (per-root dedup/review state, incl. current stage, is shown by `packrat status`, §11)

Arguments
  <folder>               A registered library root to dedup (path or --name handle).

Options
  --confirm              Apply the current stage's review (read which shortcuts remain, delete
                         accordingly; typed confirmation; DB backup first) and advance to the next
                         non-empty stage — repeat until the run completes after the last stage.
  --keep-suggested       With --confirm on STAGE 2 only: keep just each group's `_suggested` lead
                         and delete every other member, IGNORING your shortcut edits for the stage
                         ("trust packrat's pick"). A group with no suggested lead is fully spared;
                         rejected on stage 1 / stage 3 (no leads there).
  --cancel               Discard the run's staging folders (any stage); delete nothing.
  --dry-run              Compute all 3 stages and print the plan (per-stage counts, would-stage
                         list) without creating staging folders or shortcuts.
  --json                 Machine-readable plan/report.

Conventions differ by stage: `_exact_dup_to_delete\` is default-DELETE (remove a shortcut to SPARE);
`_suspect_recompression\` and `_with_minor_edits\` are default-KEEP (remove a shortcut to DELETE).
Stage 1 keeps oldest-mtime internally / drops all when an external copy exists; stages 2–3 stage
near-dup members (distinct assets) for manual review, split by PDQ distance band (§5.3). In stage 2,
packrat marks the least-compressed photo member `_suggested` (resolution → format rank → file size)
as a keep-hint — advisory by default (override with `--confirm --keep-suggested`), and the staging
report tallies how each group's lead was decided (by resolution / +format / +size).
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
  --into <dest>          Destination folder; must resolve inside a library root. Required. If the
                         resolved dest path falls under the root's ignore rules, files still copy
                         there but are NOT catalogued (scan won't track them) — merge warns loudly
                         per ignored subpath (§8 C Phase 4 step 13).
  --dry-run              Print classification counts / would-copy list (incl. the ignored-dest
                         warning); copy nothing, write no asset rows. NOTE: still
                         refreshes-and-empties the trash collection (§6.1) — that step always runs.
  --json                 Machine-readable report.

Flow: refresh trash collection (§6.1) → classify each source file by exact hash into
dup-in-source / trashed / exact-known / new → copy the `new` files (verified per file), mirroring
the source's folder structure under <dest>, and register them as assets (files landing on an
ignored dest path are copied but left uncatalogued — warned). One shot; resumable from its plan on
crash. Source is left untouched. Follow with `scan <dest>` + `dedup <dest>` to fingerprint the new
files and clean recompressed near-dups.
```

### `packrat cleanup`
Cull junk from a library folder. **Requires exactly one mode** (no bare default):
- `--trash-exact` — files **byte-identical** to trashed content; one-shot (refresh → count-confirm
  → delete to Recycle Bin; no staging). False-positive-free.
- `--trash-perceptual` — also catch *recompressed* trash copies, staged for Explorer review
  (stateful: analyze → `--confirm`); deletes exact matches too, at confirm.
- `--undecodable` — files whose pixels won't decode (§9.1); deletes them **and marks each asset
  `trashed`** so a re-import is excluded from a future merge. One-shot count-confirm. Does **not**
  touch the trashed set. See §6.2 / §9.1.

```
packrat cleanup <folder> --trash-exact       # one-shot: refresh → count-confirm → delete
packrat cleanup <folder> --trash-perceptual  # analyze: delete-nothing-yet, stage perceptual → pending
packrat cleanup <folder> --undecodable       # one-shot: delete undecodables + mark them trashed
packrat cleanup <folder> --confirm           # apply a pending --trash-perceptual run (exact + reviewed)
packrat cleanup <folder> --cancel            # discard the pending perceptual run; delete nothing

Arguments
  <folder>               A registered library root to clean (a trash root is rejected).

Options (one mode required for a fresh op; --confirm/--cancel act on a pending perceptual run)
  --trash-exact          Delete files byte-identical to trashed content (exact hash), one-shot.
  --trash-perceptual     Also match recompressed/resized copies of trashed content (§5 matcher,
                         active-vs-trashed). Stages them at
                         <root>\_packrat_review\_perceptually_identified_trash\ for review, and
                         defers ALL deletions (exact + perceptual) to --confirm.
  --undecodable          Delete the folder's undecodable files (§9.1) and mark each asset trashed
                         (trash_reason='cleanup-undecodable'). One-shot; no trash refresh.
  --confirm              Apply a pending --trash-perceptual run: delete exact matches + still-staged
                         perceptual matches (typed confirmation; DB backup first). Confirmed
                         perceptual deletions mark their asset `trashed`.
  --cancel               Discard the pending --trash-perceptual run's staging; delete nothing.
  --dry-run              Report the count/list that would be deleted (and, with --trash-perceptual,
                         staged) without deleting or staging. NOTE: the trash modes still refresh-
                         and-empty the trash collection (§6.1); --undecodable does not.
  --json                 Machine-readable report.

Review convention (--trash-perceptual, delete-default): a staged shortcut = "will delete"; remove it
to spare the file. Same as dedup's `_exact_dup_to_delete\`; opposite of dedup's keep-default
perceptual stages (`_suspect_recompression\` / `_with_minor_edits\`).
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

### `packrat untrash`
Reverse an accidental trash: **forget content from the permanent trashed-hash set** so it's no
longer excluded from future merges. You *present the file* (it's the identifier — packrat stores no
pixels to preview); untrash hashes it and matches by exact content hash. **It does not restore the
file's bytes** (that's the Recycle Bin, §10) and writes nothing to disk — only DB rows. See §6.3.

```
packrat untrash <path> [--dry-run] [--json]

Arguments
  <path>                 A file, or a folder walked recursively (allowlist/ignore rules as scan).
                         NEED NOT be a registered root — it's just bytes to hash for lookup;
                         untrash does not catalog it or care where it lives.

Options
  --dry-run              Report what would be forgotten/reactivated; change nothing. (Truly nothing
                         — untrash does not call trash-refresh, so §6.1's always-absorb rule doesn't
                         apply here.)
  --json                 Machine-readable report.
```

Per matched `trashed` asset: if it still has live file instances → flip back to `active` (retain
fingerprints); if zero instances → forget it entirely (delete asset + fingerprints), so the content
is treated as brand-new if it ever reappears. Non-matches and `active` matches are no-ops (untrash
never creates an asset). Reports: `untrashed` / `forgotten` / `already-active` / `unknown`. Takes a
global worker slot but owns no root (never blocked by / blocks a review or merge — §3).

### `packrat status` (read-only)
Print collection state without touching disk or the job queue — safe anytime, never blocked by a
running job (§3). The **single status surface** — `dedup`/`cleanup` have no `--status` flag of
their own; their review state shows up here.

```
packrat status [<root>] [--json]     # global rollup, or one root's detail
```

**No arguments — global rollup:** total assets (photo/video split), trashed count, per-root asset
counts + scan freshness, any `interrupted` jobs (with the command to resume, §3), and the
currently-running job plus any `queued` backlog behind it (§3 durable FIFO queue), if any.

**Dedup/cleanup review state — show only what's actionable.** The one state worth surfacing is a
**`pending` review run** (a paused dedup or cleanup awaiting the user); completed/cancelled runs are
history and live in the §8.1 audit trail, **not** here. Per root:
- **Pending run present** → highlight it (`⚠`), with everything needed to act: `run_type`, how long
  ago it was staged, a count summary, the `_packrat_review\` path to open in Explorer, and the exact
  `--confirm` / `--cancel` commands. Because a pending run *owns* the root (§3 per-root
  exclusivity), this line is also the answer to a job showing `blocked: root X …` in the queue — it
  names what to confirm/cancel to free the root and let the blocked job run. Count summary is per
  `run_type` (read from `review_actions`):
  - **dedup:** `N to delete (exact)` · `G groups / M members (near-dup, default-keep)` —
    optionally `(K low-confidence)` from the §5.3 photo-quality flag.
  - **cleanup --trash-perceptual:** `X exact-trash (will delete)` · `P perceptual candidates (staged)`.
- **No pending run** → a compact **recency** stat only: `deduped <age>` / `cleaned <age>`, or
  `never deduped`. Mirrors the "last scan" freshness; no run history is listed. **"Deduped" means the
  last *successful* dedup** — the `confirmed_at` of the newest `review_runs` row with
  `run_type='dedup'` **and `status='completed'`**. A run is `completed` only when it went through
  **all** stages (confirmed stage-by-stage to the end) *or* was **already clean** (no stage had any
  candidate — recorded as an immediate `completed` run so a clean folder still stamps its dedup
  time). A `cancelled` run does **not** count (the folder wasn't fully reviewed), and a still-`pending`
  run isn't "done" yet — it shows as the `⚠ pending` line above instead. `cleaned <age>` is the same,
  for `run_type='cleanup-perceptual'`. (Surfaced by `queries.root_detail` as `last_dedup_at` /
  `last_cleanup_at`; NULL → never.)

**With a root path/handle (`packrat status <root>`):** that root's detail — its pending run's full
plan breakdown (+ the review-folder path and confirm/cancel commands), and the most-recent completed
run's timestamp + one-line outcome (deeper forensics: the audit trail, §8.1). **Plus the root's
most-recent completed scan result** (§4 `scan_results`, read newest-first): the scan banner counts +
flags, and — the actionable part — the list of **problem files** with paths + reasons
(`scan_problem_files`: undecodable / read-error). The undecodable set reflects the root's *current*
catalog state (re-derived, stable across resume/incremental — §8 A2 Phase 5), so it answers "what in
this folder won't decode, and why." Problem-file detail is shown **only** here (per-root), not in the
global rollup. Historical scans are retained in `scan_results` for the M6 TUI to page through; the
CLI shows only the latest.

`--json` gives the machine-readable form of all the above. Related read-only previews on other
commands: `scan --dry-run` (would-index preview). All read-only queries run concurrently with any
job.

### `packrat jobs` — inspect and steer the work queue
The **noun for the job queue** (§3): list recent runs, and cancel or reorder work. Bare
`packrat jobs` is an alias for `packrat jobs list`. Every action here is also available in the TUI's
Queue panel (§12, design tenet §1.6).

```
packrat jobs [list] [--limit N] [--json]   # recent runs, newest-first (running/queued/terminal)
packrat jobs cancel [<job#>]               # cancel a job; no id → the currently-running one
packrat jobs prioritize <job#>             # move a queued job to the front of the queue
```

#### `packrat jobs list` — recent job runs (read-only)
Newest-first: each row shows its **id** (the handle `cancel`/`prioritize` take), display label
(`<verb> <root> (<qualifier>)`, §12), lifecycle status, progress, and the one-line `result_json`
outcome (§4). Includes the durable `queued` backlog and terminal history. `--json` for the full rows.
Read-only — runs anytime, never blocked (§3).

#### `packrat jobs cancel [<job#>]` — cancel a running or queued job
The same cancel the TUI `[c]` issues (§3, §12), addressable by id from any terminal:
- **Running** → a **cooperative** stop at the job's next checkpoint; it lands `cancelled` (terminal,
  distinct from a `daemon stop`'s `interrupted`). For `merge`/review this discards the resumable
  plan (a deliberate abort, §8 C / §8 B).
- **Queued** (runnable *or* blocked) → **dropped** from the backlog immediately (`cancelled`, never
  ran).
- A **terminal** job (done/error/cancelled/interrupted) → no-op.

**With no id, `packrat jobs cancel` targets the currently-running job** — since only one mutating job
runs at a time (§3 guarantee 1), no id is needed to stop "the" running one. Pass an explicit id to
drop a specific *queued* job (or any other). (There is no separate top-level `packrat cancel`; this
is the one cancel verb.)

#### `packrat jobs prioritize <job#>` — jump a queued job to the front
Bumps a **queued** job ahead of every other queued job, so it is the **next** to run when the worker
frees. Mechanism: a durable `jobs.priority` (§4) the dequeue sorts by (`priority DESC, enqueued_at,
id`) — so the bump **survives a daemon restart**, and re-prioritizing another job later moves it
ahead in turn.
- If the worker is free and the job is **runnable** (its owned root, if any, is not held), it starts
  **immediately**.
- If its owned root is **held** (a pending review / open merge, §3 guarantee 2), it stays pinned to
  the **front but `blocked`** — and because dequeue is *runnable-first* (§3), a lower-priority
  *runnable* job legitimately passes it and runs meanwhile. So prioritize **never deadlocks**: it
  can only advance a job as far as its root allows, exactly like normal dequeue.
- Only a **queued** job can be prioritized — a running job is already the one running; a terminal
  job is history (both → no-op).

### `packrat daemon` — manage the background daemon
The daemon normally **auto-spawns** on first client use (§3), so these are rarely needed — exposed
for lifecycle control and troubleshooting.

```
packrat daemon start        # explicitly spawn the detached daemon (no-op if already up)
packrat daemon stop         # graceful shutdown: signals the running job to checkpoint, then exits.
                            #   Leaves an in-flight job `interrupted` (resumable), NOT `cancelled` (§3).
packrat daemon restart      # stop (if up) then start a fresh daemon — picks up new code after an upgrade
packrat daemon status       # is it running? pid, uptime, bound port, in-flight job — read-only
```

`stop` is a **resumable interruption, not a cancel** (§3): re-running the interrupted command
resumes it. To truly abort work, cancel the job (`packrat jobs cancel` / TUI `[c]`), which is distinct.
`restart` is mainly for picking up a new packrat build (config reloads per job, §9.2, but *code*
changes only on restart); it stops any in-flight job as an `interrupted` (resumable), then spawns
fresh. **Self-healing:** `stop`/`restart`/`status` recover an *orphaned* daemon whose token no longer
matches (e.g. one left by a since-deleted `%APPDATA%` during testing) — the daemon binds a fixed
single-instance port (§3), so if the API answers but rejects our token, they force-stop it by that
port instead of failing on the 401.

### `packrat smoke-test` — the §9.1 decode setup check (diagnostic, not core workflow)
A one-time setup diagnostic, not a collection command: it runs the §9.1
decode→hash→perceptual→embed path over one sample of every allowlisted extension to confirm the
decode wheels work on *this* Windows/Python (the ⚠ POC cells — AVIF, RAW/cr3, the `pdqhash` wheel).
Runs **in-process** — it needs no daemon and touches no catalog.

```
packrat smoke-test [<samples>] [--generate] [--json]
```
- **No argument** → report which decode deps are importable (a quick availability check).
- **`<samples>`** → a folder holding one real file per extension; runs the full path over each.
- **`--generate` / `-g`** → synthesize the samples first (into `<samples>` or a temp dir) — except
  RAW, which can't be synthesized (supply real camera files for the RAW group).

Exit code is non-zero if any format fails, so it doubles as a CI/setup gate. (See §9.1 for what each
⚠ cell verifies; the M0 milestone, §13, expects this run before building on the decode stack.)

### `packrat` (no arguments) — the TUI
Opens the Textual TUI — the default face of the tool (§12). Every action it offers is also one of
the CLI commands above (design tenet §1.6); the TUI is a live window onto the same daemon jobs.

### Dev-only commands
A `packrat dev …` group (currently `dev clear-db`, which empties the catalog) is registered **only in
a dev build** (a source checkout or `$PACKRAT_DEV`); a release/wheel install never exposes it, so it
is not part of the user-facing surface documented here.

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
│  │ Assets      124,803          │  │ ▸iPhone    D:\Backup\iPhone   98,412  │  │
│  │  photos     111,240          │  │  Camera    E:\Photos          26,150  │  │
│  │  videos      13,563          │  │  Downloads D:\dump               241  │  │
│  │ Trashed       3,904          │  │  _Trash    D:\Backup\_Trash  (trash)  │  │
│  │ Last scan   2h ago           │  │  …                                    │  │
│  │                              │  │  ● recent ○ stale  ▸ selected → jobs  │  │
│  └───────────────────────────────┘  └───────────────────────────────────────┘  │
│                                                                                │
│  ┌─ Queue ────────────────────────────────────────────────────────────────┐  │
│  │ ▶ scan  iPhone         ███████████░░░  67%  8,912/13,204  ETA 4m (running)│  │
│  │ 2 merge dump→Camera    queued · waiting for worker                       │  │
│  │ 3 scan  Photos         blocked: Photos has a pending dedup (confirm/cancel)│  │
│  │ 4 dedup Photos (confirm) blocked: Photos has a pending dedup             │  │
│  │ ↑/↓ select  [c] cancel selected  [x] cancel-all queued  [Enter] detail   │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                                │
│  ┌─ iPhone — jobs ────────────────────────────────────────────────────────┐  │
│  │ ▶ scan   running  67%  8,912/13,204                            (see Queue)│  │
│  │   dedup  ⚠ awaiting review · 240 delete · 18 grp/47 mbr   11:31          │  │
│  │   scan   done     +412 new · 3 undecodable                    09:04      │  │
│  │   merge  done     240 copied · 1 trashed skipped              Jul 14     │  │
│  │   scan   interrupted — re-run to resume                       Jul 13     │  │
│  │ ↑/↓ select   [Enter] result   [o] open review   [g] confirm  [k] cancel  │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                                │
│  What do you want to do?                                                       │
│   [s] Scan a folder        [d] Dedup a folder      [m] Merge into a folder     │
│   [t] Refresh / cleanup trash   [r] Manage roots   [q] Quit                    │
└────────────────────────────────────────────────────────────────────────────────┘
```

Stacked regions under the logo: **stats + roots**, the **global Queue**, a **per-root jobs** list
(shown when a root is selected), then the **menu**. The header shows version and daemon health
(auto-spawns it if down).

### Panels

- **Logo + tagline** — the packrat mascot (ASCII art of a rat clutching a `♦` — its hoard),
  the tagline "hoards everything, keeps a system", and a **live "· N assets hoarded ·" line** that
  reflects the current total-asset count (updates as scans/merges add assets). Cosmetic + a small
  at-a-glance stat; sets the tone.
- **Collection stats** — read-only DB rollups: total assets (photo/video split), trashed count, and
  last-scan recency. Refreshes live while jobs run. (All fields come straight from
  `queries.status_snapshot` — §11.) *(A `similarity_edges`-derived "duplicates (est)" stat was
  considered and **dropped**: `similarity_edges` is a per-run cache that only exists after a `dedup`
  and is never trusted as a complete count (§8 B), so a headline "est" number would be misleading —
  0 on a fresh collection, stale after scans. Duplicate state is surfaced where it's real and
  actionable instead: the per-root `⚠ awaiting review` count on a pending dedup run.)*
- **Roots** — each registered root with path, asset count, and a freshness dot (scanned recently
  vs. stale/never); trash roots labelled. **`↑/↓` moves a selection cursor (`▸`); selecting a root
  populates the per-root *jobs* panel below** (this is the "go into a root" flow). **[r] Manage
  roots** opens the add flow (`roots register`) and lists roots (`roots list`); remove/rename land
  with the deferred `roots unregister`/`roots rename` verbs (§14 #9).
- **Queue** — the global work pipeline (§3 guarantee 1): the one **running** mutating job at the top
  with its live bar/ETA, then the **durable backlog** of `queued` jobs in `enqueued_at` order. This
  is the direct view of the §3/§4 durable queue. Each queued row shows *why* it waits:
  - **`queued · waiting for worker`** — runnable, just behind the running job (the common case);
  - **`blocked: root R has a pending <run> — confirm/cancel to unblock`** — its owned root is held by
    a pending review / open merge (§3 guarantee 2), so the worker **skips it and runs the next
    runnable job**, retrying it on each pump until the holder clears (the confirm/cancel/merge job's
    completion is what pumps it in). Because dequeue is **runnable-first, not strict FIFO**, a
    runnable job legitimately passes a blocked one ahead of it — so the running order (and the
    history list) is by *start* time, not submit time.
  - **Job labels are derived from `type` + `params`, not the type alone** (pure display — no schema
    field). Many operations submit multiple `jobs` rows of the *same* `type` distinguished only by a
    param — e.g. `--confirm`/`--cancel`/analyze are all `type='dedup'` (§8 B); a `--trash-exact`
    cleanup is a `preview` job then an `apply` job, both `type='cleanup'` (§6.2). A bare type would be
    ambiguous, so the label is **`<verb> <root-name> (<qualifier>)`**, where the qualifier is derived
    from `params_json` and the lifecycle **status** (`queued`/`running`/`done`/…) is shown
    *separately* — so the qualifier stays a stable *noun* that reads correctly in every state (a
    `queued`/`running`/`done` row all read `cleanup iPhone (exact · delete)`, never "Executing
    deletion"). Two display rules: **(a)** show the root by its **name** (e.g. `iPhone`), not its full
    path — matching the Roots panel; in the *per-root* jobs panel the root is dropped entirely (the
    panel header already names it), so rows there read just `(exact · delete)`. `untrash` targets a
    raw path (owns no root, §6.3), so it shows the path's **leaf**, not a root name. **(b)** a
    **`(dry-run)`** job is a non-mutating preview — show the qualifier but **dim the row** so previews
    don't clutter the history visually. The full map:

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
    | **merge** (M5) plain / `dry_run` (shows `<src-leaf> → <dest-root>`) | `merge <src> → D` / `merge <src> → D (dry-run)` |

    (`scan --profile` is a diagnostics flag, not shown in the label — it surfaces in the detail view.)
  - **`↑/↓` navigates the queue**; the highlighted job is the target of the action keys. **`[c]`
    cancels the selected job** — a *queued* selection (runnable or blocked) is dropped from the
    backlog (`cancelled`, never ran); the *running* selection gets a cooperative stop at its next
    checkpoint (§3). **`[x]` cancels every queued job** (drains the backlog) but leaves the running
    one alone. `[Enter]` opens its detail/result view (below).
  - Empty backlog is the common case (submit-while-idle runs immediately); the panel then shows just
    the running job, or "idle" when nothing runs. Queued jobs that were carved out on restart
    (a destructive `--confirm` never auto-run, §3) appear here as `interrupted — re-run to resume`.
- **Per-root jobs** (`<root> — jobs`) — appears when a root is selected in the Roots panel; **the
  answer to "go into a root and see its jobs."** Lists that root's **current** job (if any is running
  or queued for it) and its **job history**, newest-first, from the `jobs` table filtered by
  `jobs.root_id` (plus the per-root rows a `--all` scan writes to `scan_results`, §4). Each row shows
  type, terminal status, a one-line outcome from `jobs.result_json`, and age:
  - a **running** job mirrors the Queue bar (and notes "see Queue");
  - a paused **dedup/cleanup** shows **`⚠ awaiting review`** with the `packrat status` count summary
    (§11 — e.g. `240 delete · 18 grp/47 mbr`); `[o]` opens its `_packrat_review\` folder in Explorer,
    `[g]`/`[k]` run `--confirm`/`--cancel`;
  - a **done** job shows its result one-liner (scan `+N new · K undecodable`, merge `N copied · M
    trashed skipped`, etc.);
  - an **`interrupted`** job (daemon crash/stop, §3) shows **`interrupted — re-run to resume`** with
    the command to continue it, distinguishing "the daemon died, your progress is safe" from a user
    `cancelled`.
  `↑/↓` selects a row; **`[Enter]` opens the result view** for that job (see below). History depth
  is the retained `jobs`/`scan_results` rows (retention deferred, §14 #10).
- **Job detail / result view** (`[Enter]` on any Queue or per-root row) — a full-screen card for one
  job built from `jobs.result_json` (the uniform, always-written outcome summary, §4) plus a link
  into the richer per-op record: a scan's full banner + `scan_problem_files` (undecodable/read-error
  paths, §11), a dedup/cleanup run's per-stage plan and the §8.1 audit (`proposed.json`/
  `applied.json`), a merge's per-item `merge_plan_items` tally. For a terminal job it is pure
  history (read-only); for a paused review it also carries the confirm/cancel/open-in-Explorer
  actions. `Esc` returns.
  - **No live log tail.** Per-job logs are *not* persisted (a job's `ctx.log()` lines stream as SSE
    `log` events while it runs and go to the daemon's rotating `daemon.log`, but are not stored
    per-job and re-queryable). So the result view renders **`result_json`** — which every terminal
    job writes (below) — not a log. (A running job's live log lines can still be shown from the SSE
    stream while attached; there is just nothing to *re-read* for a finished job. A per-job persisted
    log + a tail endpoint is a possible later nicety, deferred — §14.)
  - **`result_json` rendering — one shape per `op`, always present.** Every job writes a
    `result_json` with an **`op`** discriminator (`scan` / `dedup` / `cleanup` / `merge` /
    `trash-refresh` / `untrash`) and a human **`summary`** string, plus op-specific count fields. The
    TUI switches on `op` to render the card; `summary` is the always-safe one-liner fallback. Verified
    shapes (fields the card can surface):
    - **scan** → `{dry_run, full, embed, roots_scanned, roots_skipped, new, exact_dup, backfilled,
      matches_trashed, undecodable, errors, read_errors, skipped_fastpath, deleted_instances,
      forgotten_assets, candidates, summary}`.
    - **merge** → `{dry_run, source, dest_root, new, exact_known, trashed, dup_in_source, collisions,
      unindexed, errors, summary}`.
    - **dedup** → `{action ∈ analyze|confirm|cancel|dry-run, review_status, stage, to_delete_exact,
      groups, members, summary}` (a "already clean" analyze omits the review fields, carries `summary`).
    - **cleanup** → `{mode ∈ exact|perceptual|undecodable, action ∈ preview|delete|analyze|confirm|
      cancel|dry-run, summary}` + mode-specific counts (`would_delete`, or `exact`/`perceptual`,
      or `deleted`/`already_gone`).
    - **trash-refresh** → `{roots, new_trashed, flipped, already_trashed, emptied, undeletable,
      errors, summary}`.
    - **untrash** → `{dry_run, untrashed, forgotten, already_active, unknown, errors, summary}`.
    - **Terminal `error`/`interrupted`** → `result_json` may be **NULL** (the job died before setting
      one). The card then renders from `jobs.status` + `jobs.error` — e.g. an `interrupted` job shows
      `interrupted — re-run to resume`, an `error` shows its message. This is the §4 contract: status
      + error always describe the outcome even when result_json is absent, so **every job is
      show-able**. The renderer must therefore treat result_json as *optional* and key off `status`
      first, `op` second.
- **Menu** — single-key actions that launch the operations. **Nothing is refused at submit** (§3):
  submitting while the worker is busy **enqueues** the job (it appears in the Queue behind the
  running one), and submitting against a root that's under review enqueues it too — it just shows as
  `blocked: … — confirm/cancel to unblock` until you resolve the review, then runs automatically.
  Each action collects its target (a folder picker / path prompt), submits, and drops you onto the
  Queue (or the root's jobs panel) to watch.

### Behavior & scope

- **Observe-and-control, not a file manager.** The TUI never previews or edits media — that is
  Explorer's job (design tenet §1). For dedup/cleanup review it just *links out* to the staging
  folder in Explorer and waits; the actual keep/delete decisions are made by adding/removing
  shortcuts there, then confirmed from the TUI or CLI.
- **Live.** The Queue/running panel subscribes to the running job's **SSE stream** (§3) so a scan
  started in another terminal appears here with a moving bar; the stats/roots/history panels poll
  read-only snapshots on a light timer (queue reorder, a job finishing and the next starting, a new
  result landing). Cancelling here stops the job there.
  - **ETA is computed TUI-side, not by the daemon.** Progress events carry `done`/`total` (and the
    `ProgressEvent.eta_s` field exists but is left unset — no job computes it). The TUI derives the
    `ETA 4m` figure itself from the observed rate: `(total − done) / (Δdone/Δt)` over a short trailing
    window of SSE progress events. So ETA is a pure presentation-layer estimate — no backend change,
    and it degrades to blank until enough progress has streamed to rate-estimate.
- **Every job is show-able.** Because each job writes a uniform `jobs.result_json` at terminal time
  whatever its outcome (§4) — `done`, `cancelled`, `interrupted`, `error` — the per-root history and
  the result view always have something to render, not just for successes. This is the data contract
  the TUI depends on; the CLI's `status` surfaces the actionable slice of the same rows (§11).
- **Keyboard-first**, mouse optional (Textual supports both). All actions reachable by single
  keys shown in brackets; `↑/↓` drives selection in whichever list panel holds focus (Roots →
  Queue → per-root jobs), `Tab` cycles focus between panels.
- **Fixed layout — the window size never changes across interfaces (hard requirement).** Every
  interface (dashboard, the maximized Roots/Queue views, root detail, and the job result cards)
  renders inside the **same fixed terminal region** — one screenful the app owns for its whole
  lifetime. Navigating between interfaces **swaps content in place; it never grows, shrinks, or
  reflows the outer frame**, so nothing below the app jumps and the layout is stable to read. The
  target region is a fixed **80×24** floor (the safe-minimum terminal); on a larger terminal the app
  still presents that same fixed canvas rather than sprawling (extra space is left as margin — a
  responsive/reflowing layout is explicitly *not* a v1 goal). Each interface is designed to fit 80×24
  without scrolling the frame itself; long lists (roots, jobs) scroll **within** their panel, not by
  resizing it. **The `docs/M6-tui-mockups.md` mockups are generated into an identical 80×24 frame to
  mechanically enforce this** — if a future interface can't fit, that's a design signal to trim it,
  not to enlarge the window. (Textual: a fixed-size root container / screen, not auto-sizing widgets.)
- **Read-safe & CLI-complete (design tenet §1.6).** Everything the TUI does maps to an existing CLI
  verb — it issues no privileged operation of its own, so CLI and TUI stay behaviorally identical.
  The reverse also holds: there is **no TUI-only action**. The TUI is the default *face*, but the
  CLI is the complete, authoritative surface every capability must land on first (so packrat stays
  scriptable/headless and the TUI can never outrun the CLI). Both are thin clients over the same
  daemon API (§3).
- **Later milestone** (§13 M6): the CLI + daemon job runtime are the prerequisite; the TUI is a
  presentation layer on top and can land once jobs are observable. **M6 depends on two M0-runtime
  additions this section assumes:** the durable FIFO **queue** and per-job **`root_id`/`result_json`**
  columns (§3/§4). If the queue/result-history work lands as its own step, M6 is a pure presentation
  layer on top of it.

---

## 13. Build milestones (each independently useful)

**What "v1" means (resolves the scope ambiguity):** **v1 = M0–M6** — the complete
register/scan/dedup/trash/merge workflow plus the TUI, i.e. everything needed to hoard, dedup, and
merge a real collection through Explorer. The "**(v1)**" qualifiers elsewhere (non-goals §1, the
schema's deferred knobs §4) refer to this scope. **M7 (semantic embeddings) and M8 (hardening —
scheduled scans, hnswlib, watchdog) are post-v1**; embeddings are opt-in infrastructure whose
tagging behavior is still TBD (§7), and M8 is polish/scale, not core function. (Milestones are
independently useful and need not ship strictly in order — e.g. M6 depends only on the M0 runtime —
but v1 is considered done when M0–M6 are.)

- **M0 — Skeleton + job runtime + decode smoke test**: repo layout, **`config.toml` (§9.2 —
  auto-create-with-defaults + per-job reload)**, core library, SQLite schema; auto-spawned daemon
  with the **single-worker job queue** (submit / stream progress / cooperative-cancel; one job
  *runs* at a time with the rest waiting in a **durable backlog**, dequeued **runnable-first** so a
  job whose owned root is under review is held+skipped (not rejected at submit) until the holder
  clears — §3) and **startup reconciliation** (orphaned `running` → `interrupted`, durable `queued`
  backlog drained with the destructive-apply carve-out; resume-on-re-run, §3), CLI
  client with **Ctrl-C-detaches** and
  `--detach`, `daemon start/stop/status`. **Plus the §9.1 smoke test** — one real sample of every
  allowlisted extension (and the RAW group) run through decode→hash→perceptual→embed to resolve
  the ⚠ cells (AVIF, RAW/cr3, `pdqhash` Windows wheel) before building on them.
- **M1 — Register + scan (exact identity)**: `roots register` (metadata-only root creation) and
  `roots list`, then the `scan` job — walker, fast-path, BLAKE3, metadata, asset/file-instance
  model, exact byte-identity resolution (attach instances), deletion detection — plus `status`. No
  embeddings, no perceptual. Now the collection is known by exact hash.
- **M2 — Perceptual signatures (scan)**: PDQ for both photos and video frames (+ quality) written
  to `phash`/`vphash` during scan, with the §5.3 sampling/quality parameters. No pairwise matching
  yet — just the inputs. No GPU/CLIP. No `imagehash` dependency.
- **M3 — Dedup operation**: single-folder `dedup` as a **3-stage sequence** — §5 matching engine
  over DB fingerprints + lazy liveness, `similarity_edges`/`review_runs`(+`stage`/`stage_phase`)/
  `review_actions`(+`stage`) tables, exact-dup resolution (stage 1: oldest-mtime internal /
  drop-on-external), perceptual banding into recompression (stage 2, + all video) and minor-edit
  (stage 3, photo) stages, Windows-shortcut staging (`_exact_dup_to_delete\` /
  `_suspect_recompression\` / `_with_minor_edits\`), the pending+stage-cursor state machine with
  `--confirm` auto-advance, `--cancel`, `--dry-run`, and the §8.1 audit trail (`proposed.json` +
  `applied.json` in APPDATA). Builds the §5 perceptual matching engine (also reused by
  `cleanup --trash-perceptual`).
- **M4 — Trash model**: multiple `kind='trash'` roots, "refresh the trash collection" (§6.1 —
  index trash-folder files → record/flip assets to `trashed` → empty the folders), scan's refusal
  to index trash roots, `packrat cleanup` (mode-required: `--trash-exact` count-confirm removal;
  `--trash-perceptual` stateful staging of recompressed-trash matches for review — reuses the M3
  engine; `--undecodable` culls the folder's undecodable files + marks them trashed, §9.1), and
  `trash refresh`. Comes before merge because merge's headline value is excluding
  trashed-but-still-on-device content.
- **M5 — Merge workflow**: `merge` — refresh-trash-first, exact-hash classification
  (dup-in-source / trashed / exact-known / new; byte-identical collapse only), copy-only ingest
  of `new` files with hash-verify + register. No perceptual matching or review folder — simple and
  one-shot (resumable from its plan).
- **M6 — TUI (`packrat` no-args)**: Textual app — packrat logo, global stats (total indexed
  assets, per-root counts, trashed count), the **global work Queue** (running + durable FIFO
  backlog, `↑/↓`-navigable, cancel a selected/all queued job, §3), a **per-root jobs list** (current
  + history, `↑/↓`, selected via the Roots panel) with a **job result/detail view** (`[Enter]`), and
  a menu to launch operations. The default entrypoint; a window onto daemon jobs started from any
  terminal. **Assumes two M0-runtime additions** (§3/§4): the durable job **queue** and per-job
  **`root_id`/`result_json`** columns — build them with M0's runtime (or as a small pre-M6 step) so
  the TUI stays a pure presentation layer. (Otherwise depends only on the M0 runtime, so could land
  earlier.)
- **M7 — Semantic embeddings**: opt-in `scan --embed` CLIP pass writing the `embeddings` table;
  brute-force cosine search scaffold. Tagging/classification behavior on top is **TBD** (§7).
- **M8 — Hardening**: scheduled interval-scan triggers (APScheduler wiring in the daemon),
  DB backup, resumability polish, larger-scale perf (hnswlib), SMB tuning (§10.1), optional
  watchdog real-time mode.

---

## 14. Open questions / risks

1. **Near-dup thresholds** `t_photo_recompress`, `t_photo_edit`, and `T_match_video` need empirical
   tuning on your real data (burst shots and edited copies are the hard photo cases; heavy re-encodes
   the hard video ones). They are **separate cutoffs** (§5.3): `t_photo_edit` is the photo match
   decision and `t_photo_recompress` bands matched photos into dedup's stage-2/stage-3 review (§8 B);
   the video cutoff only feeds the `frame_match_fraction` vote and tolerates frame noise, so expect
   `T_match_video` to land more permissive. Calibrate all three — plus the `video.*` structure knobs —
   on a small labeled sample. **Single-signal risk (accepted, §7 gap review):** photos rely on
   **PDQ alone** — pHash is not stored. If calibration shows PDQ-only precision/recall is
   inadequate, adding a second signal (pHash, or an AND/OR gate) means **re-decoding the whole
   collection** to backfill it (a multi-hour `--full`-style pass over SMB). The bet is that PDQ at
   sane thresholds is sufficient for the iPhone-re-export reality; validate on the labeled sample
   *before* the first full scan so a signal change is cheap.
2. **Live Photos & sidecars** (.AAE edits, paired .MOV): decide grouping rules.
3. **Video near-dup** is genuinely hard for heavy re-encodes; sampled per-frame **PDQ** +
   duration-aligned majority voting (§5.3) is a pragmatic start. Because the frame descriptor is
   already PDQ, the natural upgrade is **TMK+PDQF** (whose per-frame descriptor is a PDQ variant) —
   consider it if recall proves insufficient. The `video.*` knobs (frame count, fraction, quality
   gate) plus `T_match_video` all need calibration on real clips (§14 #1).
4. **Shortcut creation mechanism:** `.lnk` files need creating without a copy — via `pywin32`
   (`win32com` Shell.CreateShortcut) or `winshell`. Confirm thumbnail preview works for `.lnk`
   targets in Explorer (it does for real files; verify in the M3 spike). Fallback if `.lnk`
   previews disappoint: NTFS hardlinks (same volume only) or symlinks (needs privilege).
5. **Audit-trail retention (§8.1):** the knob now exists — `audit.retention_days` in `config.toml`
   (§9.2), default `0` = keep forever. What remains deferred is only the **pruning pass** that acts
   on a `>0` value (nothing deletes old audits yet). **Merge:** its `merge_runs`/`merge_plan_items`
   rows are now **retained on completion** (§8 C Safety & resume), giving merge a queryable
   in-DB history (source, dest, per-file classification/disposition). Open sub-question: do we
   *also* want merge to emit the same on-disk `proposed.json`/`applied.json` under
   `%APPDATA%\packrat\audit\merge\…` for symmetry with dedup/cleanup, or is the retained DB plan
   enough? (Leaning: DB plan is sufficient for v1; on-disk audit is a nicety.)
6. **Recompressed-trash on merge (accepted):** `merge` excludes trashed content by **exact hash
   only** — a *recompressed* copy of trashed content slips through as `new` on ingest. This is the
   accepted cost of keeping merge simple/one-shot; it is caught afterward by
   `cleanup <dest> --trash-perceptual` (§6.2), which stages recompressed-trash matches for review.
   (`dedup` still excludes trashed assets from grouping — §5 — so cleanup is the dedicated path.)
7. **`packrat config` command (deferred):** v1 config is a hand-edited, auto-created
   `%APPDATA%\packrat\config.toml` (§9.2) — there is no CLI to read/write keys. A future
   `packrat config get/set` (with value validation and a `--json` view) is a nicety; the TOML
   format is chosen to be forward-compatible with it. Not needed for v1, which only requires the
   file to exist, self-document its defaults, and reload per job.
8. **Batch / list untrash (deferred):** v1 `untrash` (§6.3) is **by-file only** — you present the
   file(s) to forget from trash memory, matched by exact hash. Deferred niceties: (a) a
   **read-only `packrat trash list`** (metadata-only view of trash memory — count, by reason, by
   date — no preview, since no pixels are stored); (b) a **batch `untrash --since <time>` /
   `--reason <r>`** to bulk-undo a bad refresh without re-presenting files (uses existing
   `trashed_at`/`trash_reason`; would need a typed count-confirm since it acts without a file in
   hand). Not required for v1: presenting recovered files (e.g. from the Recycle Bin) already covers
   the accidental-trash case.
9. **Root removal / rename (deferred):** v1's `roots` command has `register` (add, §8 A1) and
   `list` (§11) — but not `roots unregister` (drop a root: delete its `roots` row + cascade its
   instances/orphaned assets, with a typed confirm) or `roots rename` (change a root's `name`
   handle, re-checking global uniqueness). Needed before the TUI's "Manage roots" panel (§12) can
   do more than add + list; scoped as a small follow-on to the `roots` group, not v1-critical.
10. **Scan-result retention (deferred; accepted growth):** every completed scan persists a
   `scan_results` row per root + a `scan_problem_files` row per current problem file (§4, §8 A2
   Phase 5), kept **indefinitely** so the M6 TUI can navigate scan history. Two accumulation facts,
   accepted for now: (a) re-scanning a root **appends** a new `scan_results` row (never replaces),
   so a frequently-scanned root grows one row per scan; (b) because the undecodable problem set is
   re-derived from the catalog each scan, `scan_problem_files` re-inserts a row for *every* current
   undecodable on *every* scan — it grows **per-scan, not per-distinct-problem** (a root with 50
   permanent undecodables scanned 200× → ~10K rows, mostly duplicates). Rows are tiny so this is
   fine at v1 scale, but unbounded. Deferred fix: a retention knob (mirroring `audit.retention_days`
   §8.1) — e.g. keep the last N `scan_results` per root or prune older than N days, cascading their
   problem files — plus possibly deduping the current-undecodable list against the previous scan's.
   `status <root>` reads only the newest row, so this is purely storage hygiene, not correctness.
```
