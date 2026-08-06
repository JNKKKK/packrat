# CLI surface (complete command reference)

Adding a folder is two commands (`roots register` then `scan`); `probe` cheaply checks whether a root
has unscanned files (no fingerprinting, see [probe](operation-probe.md)); `dedup` de-duplicates one folder via Explorer
shortcuts (analyze → `--confirm`); `merge` copies new files in (exact-hash, one shot); trash is
handled by `cleanup`, `trash refresh`, and `untrash` (see [trash model](trash-model.md)). `status` (read-only) and `jobs`
(list / cancel / prioritize the work queue) surface runtime state; `daemon` manages the background
process. (Per design tenet in [goals and concepts](goals-and-concepts.md), every command here is also reachable from the TUI — except `probe`,
which is background-only: the TUI *reflects* its result in the status dot rather than exposing a
keyshortcut, see [tui](tui.md).)

**Shared client semantics** (all job-submitting commands — `scan`, `probe`, `dedup`, `merge`, `cleanup`,
`trash refresh`, `untrash`, `scan --embed`): each **submits a job to the daemon** and streams its progress.
- **Ctrl-C detaches the view; the job keeps running in the daemon.** Re-attach or stop it via the
  `packrat` TUI, or from another terminal.
- **`--detach`** submits the job and returns immediately without streaming.
- **Every mutating submission is enqueued — nothing is rejected at submit** (`queued`, see [architecture](architecture.md)
  guarantee 1 / [data model](data-model.md)). One mutating operation *runs* at a time; the rest wait in the durable backlog
  and the worker dequeues the first *runnable* one (owned root free) on each pump. A foreground
  submit streams from `queued · waiting for worker` (or `queued · blocked: …`) into live progress;
  `--detach` returns the queued id at once. Read-only commands never queue and are never blocked.
- **Per-root exclusivity (see [architecture](architecture.md) guarantee 2) is a *dequeue* gate, not a submit rejection:** a job whose
  *owned* root already has an active op — a `pending` dedup/cleanup review or an in-flight merge — is
  still enqueued, then **held in the backlog** (`blocked: root iPhone has a pending dedup —
  confirm/cancel to unblock`) and run automatically once the holder clears (the confirm/cancel/merge
  job pumps the queue). No command errors just because a root is busy — you can line work up behind a
  paused review and it drains when you resolve it. This includes `scan`: a manual `scan <root>` on an
  under-review root waits in the backlog; a `--all`/scheduled scan (owns no single root) skips it and
  logs the skip instead of parking the sweep.
- `packrat` with **no arguments** opens the TUI (logo, stats, live/recent jobs, operation menu).

**Root argument resolution — path vs. `--name` handle.** Commands that take a registered root
(`scan`, `probe`, `dedup`, `cleanup`, and `merge --into`) accept **either** a filesystem path **or** a
root's `--name` handle. Resolution is unambiguous and order-independent:
1. If the argument, canonicalized as a path (see [scan workflow](operation-scan.md) step 1), exactly matches a root's stored `path`
   → that root.
2. Else, if it case-insensitively matches a root's `name` → that root.
3. Else → error ("no registered root at path or named `<arg>`"; suggest `packrat roots` to list).
A path never collides with a handle in practice (a handle is a bare label like `iPhone`, a path
contains separators/a drive), and path match is tried first so an odd handle can't shadow a real
path. `untrash <path>` is **excluded** — its argument is arbitrary bytes to hash, never a root
(see [trash model](operation-untrash.md)).

## `packrat roots` — manage roots
The **noun for root lifecycle/metadata.** v1 subcommands: **`register`** (add) and **`list`**
(read). Removal/rename (`unregister`/`rename`) are deferred ([roadmap](roadmap.md) #9). Bare `packrat roots` is an
alias for `packrat roots list`.

### `packrat roots register <path>` — declare a folder as a root
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

### `packrat roots list` — list registered roots (read-only)
Each root's id, name, path, kind (`library`/`trash`), enabled, asset count, and last-scan recency.
Read-only, runs anytime (see [architecture](architecture.md)). `packrat roots` with no subcommand does the same.

```
packrat roots [list] [--json]
```

## `packrat scan`
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
  --embed                Also compute CLIP embeddings for tagging/search (see embeddings.md). Off by default.
                         Only affects trash tagging and semantic search; dedup is identical
                         either way. Backfillable later via `scan --embed` or the tagging pass.
  --dry-run              Enumerate and report what would be indexed; write nothing.
  --json                 Machine-readable report.

Exit: prints the report (new assets, exact-dup instances, skipped non-media, errors,
matches-trashed, embeddings computed vs deferred, plus any roots skipped for being under review).
Near-dup clustering is `dedup`'s job, not scan's. Resumable if interrupted.

Per-root exclusivity (see architecture.md): scan won't *run* on a root that has a pending dedup/cleanup review or an
in-flight merge — but a manual `scan <root>` is **enqueued and held** (shown `blocked: … —
confirm/cancel to unblock`), then runs automatically once you confirm/cancel the review (or the
merge finishes). It is no longer rejected at submit. A `--all`/scheduled scan owns no single root, so
it skips a held root and logs it rather than parking the whole sweep.
```

## `packrat probe`
Walk a registered library root and **count new/changed files without fingerprinting** (see [probe](operation-probe.md)) —
scan's cheap *discovery* half. No BLAKE3/decode/PDQ and no catalog writes beyond a per-root "new
files waiting" signal (`roots.probe_new_count`) the TUI surfaces as a 4-state status dot (see [tui](tui.md)). Runs
automatically every 24 h per root via the scheduler (see [architecture](architecture.md)); this verb triggers one now.

```
packrat probe [<path>] [options]

Arguments
  <path>                 A registered library root to probe. Omit with --all.

Options
  --all                  Probe every enabled library root (one probe job each; trash roots skipped).
  --detach               Submit and return without streaming (see shared client semantics).
  --json                 Machine-readable result (the submitted job id(s)).

Exit: for a single root, streams the probe and prints the new/changed count; for --all, submits N
per-root jobs and returns (`packrat status` shows each root's result). Owns its root like scan, so a
probe on an under-review root is enqueued and held until the holder clears; the queue keeps at most
one *queued* probe per root (submit-dedup), so the 24 h sweep re-firing before the last batch drained
is a no-op for any root still waiting. Never fingerprints — press `[s]`/`packrat scan` to do that.
```

## `packrat dedup`
Dedup **one registered folder** as a **3-stage sequence** (see [dedup workflow](operation-dedup.md)), one stage staged + reviewed at a
time under `<root>\_packrat_review\`: **stage 1** `_exact_dup_to_delete\` (byte-identical copies,
default-DELETE) → **stage 2** `_suspect_recompression\` (recompressions + all video near-dups,
default-KEEP) → **stage 3** `_with_minor_edits\` (photo minor-edits/crops, default-KEEP). `--confirm`
applies the current stage (to Recycle Bin) and **auto-advances** to the next non-empty stage; after
the last it completes. Compares against all **active** assets collection-wide (internal + external
roots; trashed excluded). At most one `pending` run per folder (one run spans all three stages).

```
packrat dedup <folder>              # analyze → stage 1 → pending (stage 1)
packrat dedup <folder> --prefer-internal  # analyze, but keep THIS root's copy over an external dup
packrat dedup <folder> --confirm    # apply current stage, auto-advance to next; last stage → completed
packrat dedup <folder> --confirm --keep-suggested  # stage 2: keep only each group's suggested lead
packrat dedup <folder> --cancel     # discard the whole run's staging, delete nothing → cancelled
packrat dedup <folder> --dry-run    # compute all 3 stages read-only; stage/write nothing
# (per-root dedup/review state, incl. current stage, is shown by `packrat status` below)

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
  --prefer-internal      Keep THIS root's copy over a byte-identical/near-dup copy in another root:
                         stage 1 deletes the external copy (not the internal one), and stage-2
                         keep-lead ties go to the internal copy. A RUN-WIDE policy — set at analyze,
                         stored on the run, carried across every --confirm; passing it on a later
                         --confirm that conflicts with the run's stored value is rejected. (Default:
                         the external copy is the master.)
  --cancel               Discard the run's staging folders (any stage); delete nothing.
  --dry-run              Compute all 3 stages and print the plan (per-stage counts, would-stage
                         list) without creating staging folders or shortcuts.
  --json                 Machine-readable plan/report.

Conventions differ by stage: `_exact_dup_to_delete\` is default-DELETE (remove a shortcut to SPARE);
`_suspect_recompression\` and `_with_minor_edits\` are default-KEEP (remove a shortcut to DELETE).
Stage 1 keeps oldest-mtime internally / drops all when an external copy exists (or, under
`--prefer-internal`, keeps an internal copy and drops the external one); stages 2–3 stage near-dup
members (distinct assets) for manual review, split by PDQ distance band (see fingerprints.md). In stage 2, packrat
marks the least-compressed photo member `_suggested` (resolution → format rank → file size) as a
keep-hint — advisory by default (override with `--confirm --keep-suggested`), and the staging report
tallies how each group's lead was decided (photo/video columns; by resolution / +format / +size /
bitrate / codec), the same breakdown the TUI Review box shows.
```

## `packrat merge`
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
                         per ignored subpath (see operation-merge.md, Phase 4 step 13).
  --dry-run              Print classification counts / would-copy list (incl. the ignored-dest
                         warning); copy nothing, write no asset rows. NOTE: still
                         refreshes-and-empties the trash collection (see trash-model.md) — that step always runs.
  --json                 Machine-readable report.

Flow: refresh trash collection (see trash-model.md) → classify each source file by exact hash into
dup-in-source / trashed / exact-known / new → copy the `new` files (verified per file), mirroring
the source's folder structure under <dest>, and register them as assets (files landing on an
ignored dest path are copied but left uncatalogued — warned). One shot; resumable from its plan on
crash. Source is left untouched. Follow with `scan <dest>` + `dedup <dest>` to fingerprint the new
files and clean recompressed near-dups.
```

## `packrat cleanup`
Cull junk from a library folder. **Requires exactly one mode** (no bare default):
- `--trash-exact` — files **byte-identical** to trashed content; one-shot (refresh → count-confirm
  → delete to Recycle Bin; no staging). False-positive-free.
- `--trash-perceptual` — also catch *recompressed* trash copies, staged for Explorer review
  (stateful: analyze → `--confirm`); deletes exact matches too, at confirm.
- `--undecodable` — files whose pixels won't decode (see [format coverage](tech-stack.md)); deletes them **and marks each asset
  `trashed`** so a re-import is excluded from a future merge. One-shot count-confirm. Does **not**
  touch the trashed set. See [trash model](operation-cleanup.md) / [format coverage](tech-stack.md).

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
  --trash-perceptual     Also match recompressed/resized copies of trashed content (see fingerprints.md matcher,
                         active-vs-trashed). Stages them at
                         <root>\_packrat_review\_perceptually_identified_trash\ for review, and
                         defers ALL deletions (exact + perceptual) to --confirm.
  --undecodable          Delete the folder's undecodable files (see tech-stack.md) and mark each asset trashed
                         (trash_reason='cleanup-undecodable'). One-shot; no trash refresh.
  --confirm              Apply a pending --trash-perceptual run: delete exact matches + still-staged
                         perceptual matches (typed confirmation; DB backup first). Confirmed
                         perceptual deletions mark their asset `trashed`.
  --cancel               Discard the pending --trash-perceptual run's staging; delete nothing.
  --dry-run              Report the count/list that would be deleted (and, with --trash-perceptual,
                         staged) without deleting or staging. NOTE: the trash modes still refresh-
                         and-empty the trash collection (see trash-model.md); --undecodable does not.
  --json                 Machine-readable report.

Review convention (--trash-perceptual, delete-default): a staged shortcut = "will delete"; remove it
to spare the file. Same as dedup's `_exact_dup_to_delete\`; opposite of dedup's keep-default
perceptual stages (`_suspect_recompression\` / `_with_minor_edits\`).
```

## `packrat trash refresh`
Absorb whatever is sitting in the registered trash folders into the permanent trashed-hash set,
then empty those folders (to Recycle Bin). Runs automatically inside `cleanup` and `merge`;
exposed standalone for when you've just dropped junk into a trash folder (see [trash model](operation-trash-refresh.md)).

```
packrat trash refresh [<root>] [--json]

Arguments
  <root>                 OPTIONAL. A registered trash root (path or --name) to refresh on its own.
                         Must resolve to a kind='trash' root (a library root is rejected — its
                         files are indexed by `scan`, not consumed). Omit it to refresh EVERY trash
                         root as one logical set (the original behavior, and what `cleanup`/`merge`
                         always invoke). This single-root form is what the TUI issues when you pick a
                         trash root — a trash root has no detail screen, so selecting it opens a
                         confirm modal that runs exactly this verb (see tui.md, goals-and-concepts.md parity).

Options
  --json                 Machine-readable report of what was absorbed/emptied.

**No `--dry-run`.** Unlike `cleanup`/`merge` (whose `--dry-run` skips *their own* destructive
step while refresh still runs), `trash refresh` *is* the refresh procedure — there is nothing
left to skip. Per trash-model.md refresh is never a no-op: a "dry" refresh would either contradict that
rule or be a `--dry-run` that isn't dry, so the flag is deliberately omitted. To see what is in
the trash folders without consuming them, browse them in Explorer before running this. (A true
preview-then-absorb mode would need the real refresh run inside a DB transaction and rolled back —
rejected for v1 as needless complexity, since refresh is non-destructive to the library.)

Flow: for every kind=trash root → fingerprint files (hash + perceptual, no embed) → record/flip
assets to `trashed` → delete the files (Recycle Bin). Reports new trashed fingerprints added and
files emptied.
```

## `packrat untrash`
Reverse an accidental trash: **forget content from the permanent trashed-hash set** so it's no
longer excluded from future merges. You *present the file* (it's the identifier — packrat stores no
pixels to preview); untrash hashes it and matches by exact content hash. **It does not restore the
file's bytes** (that's the Recycle Bin, see [performance](performance.md)) and writes nothing to disk — only DB rows. See [trash model](operation-untrash.md).

```
packrat untrash <path> [--dry-run] [--json]

Arguments
  <path>                 A file, or a folder walked recursively (allowlist/ignore rules as scan).
                         NEED NOT be a registered root — it's just bytes to hash for lookup;
                         untrash does not catalog it or care where it lives.

Options
  --dry-run              Report what would be forgotten/reactivated; change nothing. (Truly nothing
                         — untrash does not call trash-refresh, so trash-model.md's always-absorb rule doesn't
                         apply here.)
  --json                 Machine-readable report.
```

Per matched `trashed` asset: if it still has live file instances → flip back to `active` (retain
fingerprints); if zero instances → forget it entirely (delete asset + fingerprints), so the content
is treated as brand-new if it ever reappears. Non-matches and `active` matches are no-ops (untrash
never creates an asset). Reports: `untrashed` / `forgotten` / `already-active` / `unknown`. Takes a
global worker slot but owns no root (never blocked by / blocks a review or merge — see [architecture](architecture.md)).

## `packrat status` (read-only)
Print collection state without touching disk or the job queue — safe anytime, never blocked by a
running job (see [architecture](architecture.md)). The **single status surface** — `dedup`/`cleanup` have no `--status` flag of
their own; their review state shows up here.

```
packrat status [<root>] [--json]     # global rollup, or one root's detail
```

**No arguments — global rollup:** total assets (photo/video split), trashed count, per-root asset
counts + scan freshness, any `interrupted` jobs (with the command to resume, see [architecture](architecture.md)), and the
currently-running job plus any `queued` backlog behind it (see [architecture](architecture.md) durable FIFO queue), if any. This
rollup is **composed client-side** from the single-concern resource reads (`/stats` + `/jobs/live` +
`/reviews` + `/roots`, see [architecture](architecture.md)) — there is no combined `/status` endpoint; the CLI assembles the human
summary because it's a summary view, not a hot path. `<root>` resolves the handle to an id
(`/roots/resolve`) then reads `/roots/{id}`.

**Dedup/cleanup review state — show only what's actionable.** The one state worth surfacing is a
**`pending` review run** (a paused dedup or cleanup awaiting the user); completed/cancelled runs are
history and live in the [audit trail](operation-dedup.md), **not** here. Per root:
- **Pending run present** → highlight it (`⚠`), with everything needed to act: `run_type`, how long
  ago it was staged, a count summary, the `_packrat_review\` path to open in Explorer, and the exact
  `--confirm` / `--cancel` commands. Because a pending run *owns* the root (see [architecture](architecture.md) per-root
  exclusivity), this line is also the answer to a job showing `blocked: root X …` in the queue — it
  names what to confirm/cancel to free the root and let the blocked job run. Count summary is per
  `run_type` (read from `review_actions`):
  - **dedup:** `N to delete (exact)` · `G groups / M members (near-dup, default-keep)` —
    optionally `(K low-confidence)` from the [fingerprints](fingerprints.md) photo-quality flag.
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
run's timestamp + one-line outcome (deeper forensics: the [audit trail](operation-dedup.md)). **Plus the root's
most-recent completed scan result** (see [data model](data-model.md) `scan_results`, read newest-first): the scan banner counts +
flags, and — the actionable part — the list of **problem files** with paths + reasons
(`scan_problem_files`: undecodable / read-error). The undecodable set reflects the root's *current*
catalog state (re-derived, stable across resume/incremental — see [scan workflow](operation-scan.md) Phase 5), so it answers "what in
this folder won't decode, and why." Problem-file detail is shown **only** here (per-root), not in the
global rollup. Historical scans are retained in `scan_results` for the M6 TUI to page through; the
CLI shows only the latest.

`--json` gives the machine-readable form of all the above. Related read-only previews on other
commands: `scan --dry-run` (would-index preview). All read-only queries run concurrently with any
job.

## `packrat jobs` — inspect and steer the work queue
The **noun for the job queue** (see [architecture](architecture.md)): list recent runs, and cancel or reorder work. Bare
`packrat jobs` is an alias for `packrat jobs list`. Every action here is also available in the TUI's
Queue panel (see [tui](tui.md), design tenet in [goals and concepts](goals-and-concepts.md)).

```
packrat jobs [list] [--limit N] [--json]   # recent runs, newest-first (running/queued/terminal)
packrat jobs cancel [<job#>]               # cancel a job; no id → the currently-running one
packrat jobs prioritize <job#>             # move a queued job to the front of the queue
```

### `packrat jobs list` — recent job runs (read-only)
Newest-first: each row shows its **id** (the handle `cancel`/`prioritize` take), display label
(`<verb> <root> (<qualifier>)`, see [tui](tui.md)), lifecycle status, progress, and the one-line `result_json`
outcome (see [data model](data-model.md)). Includes the durable `queued` backlog and terminal history. `--json` for the full rows.
Read-only — runs anytime, never blocked (see [architecture](architecture.md)).

### `packrat jobs cancel [<job#>]` — cancel a running or queued job
The same cancel the TUI `[c]` issues (see [architecture](architecture.md), [tui](tui.md)), addressable by id from any terminal:
- **Running** → a **cooperative** stop at the job's next checkpoint; it lands `cancelled` (terminal,
  distinct from a `daemon stop`'s `interrupted`). For `merge`/review this discards the resumable
  plan (a deliberate abort, see [merge workflow](operation-merge.md) / [dedup workflow](operation-dedup.md)).
- **Queued** (runnable *or* blocked) → **dropped** from the backlog immediately (`cancelled`, never
  ran).
- A **terminal** job (done/error/cancelled/interrupted) → no-op.

**With no id, `packrat jobs cancel` targets the currently-running job** — since only one mutating job
runs at a time (see [architecture](architecture.md) guarantee 1), no id is needed to stop "the" running one. Pass an explicit id to
drop a specific *queued* job (or any other). (There is no separate top-level `packrat cancel`; this
is the one cancel verb.)

### `packrat jobs prioritize <job#>` — jump a queued job to the front
Bumps a **queued** job ahead of every other queued job, so it is the **next** to run when the worker
frees. Mechanism: a durable `jobs.priority` (see [data model](data-model.md)) the dequeue sorts by (`priority DESC, enqueued_at,
id`) — so the bump **survives a daemon restart**, and re-prioritizing another job later moves it
ahead in turn.
- If the worker is free and the job is **runnable** (its owned root, if any, is not held), it starts
  **immediately**.
- If its owned root is **held** (a pending review / open merge, see [architecture](architecture.md) guarantee 2), it stays pinned to
  the **front but `blocked`** — and because dequeue is *runnable-first* (see [architecture](architecture.md)), a lower-priority
  *runnable* job legitimately passes it and runs meanwhile. So prioritize **never deadlocks**: it
  can only advance a job as far as its root allows, exactly like normal dequeue.
- Only a **queued** job can be prioritized — a running job is already the one running; a terminal
  job is history (both → no-op).

## `packrat daemon` — manage the background daemon
The daemon normally **auto-spawns** on first client use (see [architecture](architecture.md)), so these are rarely needed — exposed
for lifecycle control and troubleshooting.

```
packrat daemon start        # explicitly spawn the detached daemon (no-op if already up)
packrat daemon stop         # graceful shutdown: signals the running job to checkpoint, then exits.
                            #   Leaves an in-flight job `interrupted` (resumable), NOT `cancelled` (see architecture.md).
packrat daemon restart      # stop (if up) then start a fresh daemon — picks up new code after an upgrade
packrat daemon status       # is it running? pid, uptime, bound port, in-flight job — read-only
```

`stop` is a **resumable interruption, not a cancel** (see [architecture](architecture.md)): re-running the interrupted command
resumes it. To truly abort work, cancel the job (`packrat jobs cancel` / TUI `[c]`), which is distinct.
`restart` is mainly for picking up a new packrat build (config reloads per job, see [tech stack](tech-stack.md), but *code*
changes only on restart); it stops any in-flight job as an `interrupted` (resumable), then spawns
fresh. **Self-healing:** `stop`/`restart`/`status` recover an *orphaned* daemon whose token no longer
matches (e.g. one left by a since-deleted `%APPDATA%` during testing) — the daemon binds a fixed
single-instance port (see [architecture](architecture.md)), so if the API answers but rejects our token, they force-stop it by that
port instead of failing on the 401.

## `packrat smoke-test` — the [format coverage](tech-stack.md) decode setup check (diagnostic, not core workflow)
A one-time setup diagnostic, not a collection command: it runs the [format coverage](tech-stack.md)
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

Exit code is non-zero if any format fails, so it doubles as a CI/setup gate. (See [format coverage](tech-stack.md) for what each
⚠ cell verifies; the M0 milestone, [roadmap](roadmap.md), expects this run before building on the decode stack.)

## `packrat` (no arguments) — the TUI
Opens the Textual TUI — the default face of the tool (see [tui](tui.md)). Every action it offers is also one of
the CLI commands above (design tenet in [goals and concepts](goals-and-concepts.md)); the TUI is a live window onto the same daemon jobs.
`--offline` runs it on a bundled sample dataset (no daemon) for demoing; `--nsfw` masks adult-content
root names/paths on screen (display-only privacy redaction, see [tui](tui.md)).

## Dev-only commands
A `packrat dev …` group (currently `dev clear-db`, which empties the catalog) is registered **only in
a dev build** (a source checkout or `$PACKRAT_DEV`); a release/wheel install never exposes it, so it
is not part of the user-facing surface documented here.
