# Architecture

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
             │  Scheduler (APScheduler → periodic probe/scan sweeps)      │
             │  core library (fingerprint · match engine · trash · review)│
             │  SQLite (WAL)  +  perceptual/vector search  +  (opt) CLIP  │
             └───────────────────────────────────────────────────────────┘
```

**Daemon** — single long-running process, auto-spawned detached on first client use (no manual
"start the server" step). Owns:
- the **SQLite DB** (single writer — see concurrency below);
- a **persisted job queue** running **one mutating job at a time**, the rest waiting in a durable
  FIFO backlog that survives restart (guarantee 1 below); each job is cooperatively cancellable and
  checkpointed/resumable (per the [scan](operation-scan.md)/[dedup](operation-dedup.md)/[merge](operation-merge.md) workflows);
- the **periodic-job scheduler** (`jobs/scheduler.py`, **now realized** — see below) which
  submits jobs like any client;
- the review-run state (`review_runs`) and audit trail.
Exposes a small HTTP API on `127.0.0.1` with a local token (`%APPDATA%\packrat\token`). Reads
tunable settings from `%APPDATA%\packrat\config.toml` (see [tech stack](tech-stack.md)), reloaded at each job start.

**The periodic scheduler (`PeriodicScheduler`, `jobs/scheduler.py`).** A thin wrapper over
APScheduler's `BackgroundScheduler` + a declarative `PeriodicTask` registry (`PERIODIC_TASKS`,
mirroring the `JobSpec` pattern), general enough for future periodic work (scheduled `--full`
scans → M8, audit pruning [dedup workflow](operation-dedup.md), embedding backfills [embeddings](embeddings.md)). Its **first client is `probe`** (see [scan workflow](operation-probe.md)): the `probe-all` task's `submit(queue, db)` thunk fans out to one `probe <root>` per
enabled library root every `schedule.probe_interval_hours` (24 h default, small jitter). The
scheduler is **just another queue client** — its job func runs on APScheduler's own thread and
only calls `queue.submit(...)`, never runs job work, so the "one mutating job at a time"
invariant is untouched. It uses an **in-memory jobstore** (the schedule is re-armed from
`PERIODIC_TASKS` on every daemon start; durability lives in the job queue, not the schedule —
`coalesce=True` + a misfire grace collapse missed fires). `build_app` constructs it after the
queue + reconcile; `scheduler.start()`/`shutdown()` run in the daemon's startup/shutdown lifespan
hooks, symmetric with `queue.shutdown()`. APScheduler is a **new core dependency** (pure-Python,
no wheel risk — see [tech stack](tech-stack.md)).

**Progress transport — server-sent events (SSE), not polling.** A client that submits (or attaches
to) a job holds an SSE stream on the HTTP API; the daemon pushes progress/state events (bar, counts,
ETA, completion). The TUI uses the same stream for the running job. Read-only *reads* (see the
resource API below) are plain request/response — polled on a light timer. SSE is chosen over polling
for the live progress path so a moving bar doesn't require a busy-loop of requests; it degrades
gracefully (a dropped stream just reconnects, since job state is durable in the `jobs` table).

**HTTP API — resource-oriented, single-concern reads.** The read surface is organized so each
resource returns **one concern**, and each TUI dashboard box polls the *same* resource its maximized
screen does (the source-sharing principle — no box can drift from its full view). The reads:
- `GET /stats` — the collection summary alone (active-asset counts, trashed, on-disk size, lifetime-
  deduped). O(collection) aggregations; nothing job-, root-, or review-specific. Backs the dashboard
  Collection box.
- `GET /jobs` — global job history, newest-first, **paged** (`limit`/`offset`, `terminal_only`,
  returns `total`). `GET /jobs/live` — the live set (running ≤1 + queued backlog + interrupted) in
  one consistent read. `GET /jobs/{id}` — one job; `GET /jobs/{id}/stream` — its SSE stream.
- `GET /reviews` — the open (`pending`) review runs across all roots. **A review is `review_runs`
  state, not a job**, so it is its own resource (not folded into `/jobs/live`).
- `GET /roots` — the root list (`id`, name, path, kind, counts, scan/dedup recency). `GET
  /roots/resolve?q=<name|path>` — resolve a user-typed handle → `{id}` (the *one* name/path→id hop;
  a query value, so a Windows path with `\`/spaces/`%` is percent-encoded and never a URL path
  segment). `GET /roots/{id}` — one root's detail **by id** (counts, recency, its running/queued
  jobs, its `pending_review`). `GET /roots/{id}/history` — that root's terminal job history, paged
  (always terminal — the root's live running/queued come from `/roots/{id}`).

There is **no combined `/status` snapshot** — the CLI `status` composes the global rollup client-side
from `/stats` + `/jobs/live` + `/reviews` + `/roots` (a human summary, not a hot path). Two invariants
this shape buys: (1) **unbounded lists are always paged** — job history (global and per-root) is never
fetched wholesale, only a window at a time (see [TUI](tui.md)); (2) **live vs. terminal are separate** — running/
queued (small, mutable, live) come from `/jobs/live` or `/roots/{id}`, while history (large, append-
only) is its own paged resource, so a poll of one never drags the other.

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
  the work (per the [CLI](cli.md) recovery decision): the durable per-op plan is intact, so the user re-runs the
  command to continue. This avoids a **crash-loop** (a file/bug that killed the daemon would
  re-kill it on boot) and never resumes a *destructive* apply (`dedup`/`cleanup --confirm`) with
  nobody watching. Resume paths per type (all already specified — this step only flips the stale
  status flag so the machinery can re-engage):
  - **scan** → re-run `scan`; the **fast-path** (path+size+mtime skip, see [scan workflow](operation-scan.md) step 4) makes already-
    fingerprinted files no-ops, so it effectively continues where it left off — `jobs.done` is just
    the progress number, not the resume key. Deletion-detection is naturally safe (it keys off this
    pass's enumeration, see [scan workflow](operation-scan.md) step 11).
  - **merge** → its `merge_runs` row is still open (`planning`/`copying`); re-running `merge`
    silently auto-resumes from the frozen plan (see [merge workflow](operation-merge.md)). *(A crash in Phase 1 before the plan was
    committed leaves no open `merge_runs` → re-run just starts fresh.)*
  - **trash refresh** → idempotent by construction (record-then-delete, see [trash refresh](operation-trash-refresh.md)); re-run re-processes
    only the trash files still present.
  - **untrash** → idempotent (hash → forget/reactivate, see [trash model](operation-untrash.md)); re-run is a no-op on already-handled
    files.
  - **dedup/cleanup analyze** interrupted mid-staging → the crash left a `pending` review_run with
    **half-built staging**. Reconciliation **rolls it back**: delete the partial
    `_packrat_review\` staging folders and mark that review_run `cancelled` (record it as
    `interrupted-analyze` in the audit `applied.json`, see [dedup workflow](operation-dedup.md)). This clears the way for a clean
    re-run — otherwise the pending row would reject a fresh `dedup`, and `--confirm` on partial
    staging would apply a wrong plan. *(A **completed** analyze — paused, fully staged, awaiting the
    user — has no `running` job row, so it is untouched: its `pending` review_run and staging remain
    exactly as left, ready for `--confirm`/`--cancel`.)*
  - **dedup/cleanup `--confirm`** interrupted mid-apply → the review_run is still `pending` and the
    plan (`review_actions`) records intended deletions; re-running `--confirm` re-reads shortcut
    presence and re-applies via the per-file lazy-liveness gate (see [dedup workflow](operation-dedup.md) Phase 6), which is idempotent
    (already-deleted files → "already-gone"). The DB backup taken before apply is the backstop.
- **Durable `queued` backlog → drained, with one carve-out.** Because the backlog is persisted
  (guarantee 1 below, [data model](data-model.md) `jobs.status='queued'`), jobs that were merely *waiting* — never started, so
  nothing on disk or in the DB was touched — are **not** stale and are **kept**: after the running
  row is reconciled, the daemon resumes draining them in `enqueued_at` order like normal. This is the
  point of a durable queue — an auto-appended `roots register --scan` (see [scan workflow](operation-register.md)) still runs after a
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
/ another terminal, see [tech stack](tech-stack.md)/[TUI](tui.md)) that *does* set `cancelled` (terminal) and, for merge/review, discards
the resumable plan. So "stop the daemon" never loses in-flight progress; only an explicit cancel
does. **Shutdown must not hang on an open SSE stream:** an attached `/jobs/{id}/stream` never
completes on its own (its response stays open, heartbeating), and uvicorn's graceful shutdown waits
for in-flight request tasks — with the default (infinite) grace timeout that hangs the process,
leaking an orphaned daemon that keeps files (e.g. `daemon.log`) open. So `/shutdown` **closes all SSE
subscribers up front** (pushes the stream-end sentinel so each generator returns and frees its worker
thread), and the server sets a **finite `timeout_graceful_shutdown`** as the backstop.

**CLI** — thin client. `packrat scan D:\…` submits a job and **streams its progress**. Key
property: **the job runs in the daemon, not the terminal**, so:
- **Ctrl-C detaches the view, it does NOT stop the job.** The CLI prints "still running — type
  `packrat` to track or stop it." (`--detach` submits and returns immediately without streaming.)
- Killing the terminal, closing SSH, logging out — none touch the running job.

**TUI** (`packrat` with no args) — the default face of the tool: the packrat logo, global stats
(total indexed assets, per-root counts), **live and recent job runs with progress**, and a menu
to launch operations. It is also where you **cancel** a running job. Because jobs live in the
daemon, the TUI is a *window* onto them — open it anytime, from any terminal, to watch or stop
work started elsewhere. (TUI appearance & function: see [TUI](tui.md); milestone: see [roadmap](roadmap.md) M6.)

**Concurrency — two independent guarantees.** packrat serializes work at two levels; a mutating
job must clear **both** to start.

1. **Global: one mutating job runs at a time, the rest wait in a durable queue.** The single-worker
   queue is the enforcement point: exactly one mutating job is ever *running*. **Every** mutating
   submission is **enqueued** — a `jobs` row with `status='queued'` (see [data model](data-model.md)) — *never rejected at
   submit*; nothing is turned away. The backlog is **persisted** (durable `jobs` rows, not an
   in-memory list), so queued work survives a daemon restart and drains on the next start (see startup
   reconciliation above — with one safety carve-out: a queued destructive apply is *not* auto-run
   unattended). This is what lets one command line up work behind another (and, later, lets `roots
   register --scan` append a scan behind whatever is running — see [scan workflow](operation-register.md)). The worker *slot* is still
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
     (see [dedup workflow](operation-dedup.md)/[trash model](operation-cleanup.md): `--confirm`/`--cancel` are separate `jobs` rows of the same type, dispatched by
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
     reason, see [TUI](tui.md)), and you can cancel it out of the backlog anytime.
   - **`--detach` returns the queued job's id immediately**; a foreground CLI submit streams from the
     moment it's enqueued (showing `queued · waiting for worker`, or `queued · blocked: …` when its
     root is held, then live progress once it starts). Cancelling a still-`queued` job drops it from
     the backlog (`cancelled`, never ran) — distinct from cancelling the running one (see cancellation above).

2. **Per-root: one *active* operation owns a root at a time** (running **or** pending). This is the
   general invariant that the per-operation validations ([dedup workflow](operation-dedup.md) Phase 0, [trash model](operation-cleanup.md), [merge workflow](operation-merge.md) Phase 0) and the
   DB's partial-unique indexes ([data model](data-model.md): one pending `review_runs` per root; one open `merge_runs` per
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
   > review/open merge waits in the backlog rather than churning the plan's rows (see [scan workflow](operation-scan.md) step 1a); a
   > scheduled / `--all` scan still **skips that root and logs it** (it iterates roots rather than
   > owning one, so it must not park the whole sweep on one under-review root).

   **Owned vs. referenced — what is *not* locked.** Exclusivity is on the **owned** root only, not
   on roots an op merely reads or can reach into: `dedup` compares against **all active assets
   collection-wide** and may delete an *external* survivor in another root (see [dedup workflow](operation-dedup.md) cross-folder note);
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
