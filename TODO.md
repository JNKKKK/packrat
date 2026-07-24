# TODO

## New `probe <root>` job, a general scheduler, and a 4-state status dot

**Goal.** A cheap, scheduled **`probe`** job that walks a root and answers one question —
*are there files here we haven't scanned yet?* — **without fingerprinting anything**. It
records a per-root "new files waiting" signal that the TUI surfaces as a new status-dot state,
so the user learns a root needs a `scan` without remembering they dropped files in. Runs every
24 h per root; never blocks the user (they can always press `[s]` to scan now). CLI-exposed
(`packrat probe <root>` / `--all`); **no** TUI keyshortcut (background-only).

This splits the two halves `scan` conflates today — *discovery* (walk + notice new paths,
seconds, no per-file I/O) and *fingerprinting* (hash + decode + PDQ, the multi-hour cost).
Probe is discovery-only. Alongside it we land the **general scheduler infrastructure** the
daemon has always been slated to own (§3), built to carry future periodic jobs (scheduled
full scans, audit pruning, embedding backfills), not just probe.

Three design decisions are settled (see the discussion notes inline):
- **N per-root probe jobs, not one `probe --all` sweep** — each waits for its own root to be
  idle via the existing dequeue gate; the submit-dedup caps the backlog at one-per-root.
- **A general periodic-job scheduler** (a registry of periodic specs + a daemon timer), not a
  probe-specific hack.
- **Dot precedence driven by `probe_new_count`** — a probe-with-new-files shows grey-half ◐
  (from any state, incl. a never-scanned root); a found-nothing probe never changes the dot
  (never stays never, green stays green, yellow stays yellow).

---

### Part A — the `probe` job

**What it does.** Reuse `scan`'s `enumerate_root(root_path, ignore)` (already the walk +
allowlist + ignore-glob filter — §8 A2 Phase 1) to build the candidate path list, then, per
candidate, apply the **existing fast-path predicate** (`file_instances` row at this
`(root_id, path)` with matching `size` + tolerant `mtime`, §8 A2 step 4) to decide *known* vs
*new/changed*. Count the news. **No BLAKE3, no decode, no PDQ, no `assets`/`phash`/`vphash`
writes** — that is exactly the line between probe and scan.

- "New" = a candidate path with **no** matching live `file_instances` row, OR a matching row
  whose `size`/`mtime` drifted past tolerance (a changed file also needs re-scanning). Probe
  reuses the *same* skip predicate scan uses, so "probe says N" ⇒ "scan would fingerprint ≥ N".
  Factor that predicate into a shared helper if it isn't already callable standalone (avoid a
  second copy — see [[scan-photo-pipeline]]).
- Probe does **not** do deletion-detection (that mutates `file_instances`/forgets assets, §8 A2
  step 11 — scan's job, needs the full pass). Probe is **read-only on the catalog**; its only
  write is the per-root signal below. Safe to run unattended with zero catalog risk.
- **Trash roots:** skip (scan never touches `kind='trash'` — §6.1). `probe --all` iterates
  `enabled=1 AND kind='library'`.
- **Offline / unreadable root** (SMB blip, §10.1): report `root_offline`, write **no** signal —
  absence of a readable listing ≠ "no new files"; never let an unreachable root read as "clean".

**Per-root exclusivity — probe OWNS its root (decision #2).** Set `owned_root = root_id`, so a
probe **waits in the backlog until its root is idle** (no running scan/dedup/cleanup/merge on
it) exactly like scan does (§3 guarantee 2, dequeue gate). Rationale + the discussion below:

> **Why per-root probe jobs beat one `probe --all` sweep.** A single sweep job owns no root and
> *iterates* — so when it reaches a root with an active op it must either **skip it** (that root
> goes un-probed this cycle — a silent miss) or **stall the whole sweep** waiting on one busy
> root. Neither is good. N per-root jobs sidestep it: each is an independent queue entry that the
> existing runnable-first scheduler holds until *its* root frees, then runs in seconds and
> releases. No root is missed, no root blocks another. The cost — "100 roots → 100 jobs every
> 24 h" — is bounded by the **submit-dedup** below: at most one queued probe per root ever
> exists, so a backed-up worker never piles them up; the next tick is a no-op for any root that
> still has one waiting. This is the design the queue was built for (a durable per-root backlog
> that waits on the worker, never on a human — §3).
>
> **Decided: probe OWNS its root** (blocks other ops on it while it runs). Reuses the dequeue
> gate verbatim — no new "wait-but-don't-hold" gate class — and probe is sub-second-to-seconds,
> so the brief block is negligible. (The alternative, *deferring* without holding, was rejected:
> it needs machinery no other op wants, for a saving that doesn't matter at probe's speed.)

**Where the signal lives.** Add to `roots` (mirrors `last_full_scan_at`, already on the row):

```sql
-- db/schema.py, roots table:
last_probe_at         TEXT,  -- when probe last completed on this root (recency display)
probe_new_count       INTEGER NOT NULL DEFAULT 0,  -- new/changed files probe last saw awaiting
                             -- a scan. Set by probe; CLEARED to 0 by a completed scan of the root.
                             -- This ONE field carries the whole dot-precedence signal (Part C):
                             -- because a completed scan always resets it to 0, `count > 0` means
                             -- exactly "a probe found unscanned files and no scan has consumed
                             -- them yet" — no separate `last_activity` column is needed.
```

Nullable / default 0 → a row predating the columns reads NULL/0 = current behavior; no
migration runner (pre-release), but the **live DB needs the same `ALTER TABLE roots ADD
COLUMN …` patch** used for the review_runs threshold snapshot (apply the 2 columns before
running the new code; a fresh DB gets them from `SCHEMA_SQL`).

**Why no `last_activity` column** (a simplification the dot cases below forced out): an earlier
draft added a `last_activity ∈ {scan,probe}` to encode "latest op wins." It's redundant.
`probe_new_count` is self-clearing — scan zeroes it — so `count > 0` *is* the "latest meaningful
op is a probe-with-news" state, and `count = 0` means "nothing unscanned," whatever ran last.
The count also stays honest if the user deletes the new files and re-probes (re-enumeration
finds 0 → dot reverts), which a sticky `last_activity='probe'` would get wrong.

**`scan` clears the count; `probe` sets it.**
- A completed `scan <root>` (Phase 5 persist, §8 A2, where it stamps `last_full_scan_at`):
  set `probe_new_count=0` (news consumed); `last_probe_at` unchanged. A `--dry-run` scan
  changes nothing. An interrupted/failed scan clears nothing.
- A completed `probe <root>` (clean enumeration, not offline): set `last_probe_at=now` and
  `probe_new_count=<n found>` — which may be **0** (found nothing). Writing 0 is correct and
  important: it means "a probe ran and there's nothing unscanned," so the dot stays whatever
  the scan/dedup state says (green stays green, yellow stays yellow — Part C decision #3).
- **Offline probe writes nothing** (no `last_probe_at`, no count change) — an unreachable root
  must never be recorded as "0 new files" (that would wrongly clear a real pending signal).

**Job wiring (`jobs/probe.py`, register via `JobSpec`).**
- `owned_root=lambda p: p.get("root_id")` (decision #2 above). Non-destructive → reconcile
  drains a queued probe normally (no carve-out, §3); an interrupted running probe just re-runs
  (idempotent — recomputes the count from scratch, writes nothing else).
- `params`: `{root_id}`. (`probe --all` is a CLI/scheduler convenience that **expands to N
  per-root submissions**, not a single root-less job — decision #2.)
- `result_json`: `{op: "probe", new_count, root_offline}` so the §12 job card shows an outcome.

**CLI (`packrat probe`).** Mirror the `scan` command shape (`cli/main.py`), minus fingerprint
flags: `packrat probe <root>` / `probe --all`, `--detach`, `--json`. `probe --all` resolves the
enabled library roots and submits one `probe <root>` per root (the client or the `/probe`
endpoint does the fan-out), so each gets its own queue entry + dequeue gate. Add
`client.submit_probe(...)` and a `POST /probe` endpoint (resolve root arg like `/scan`). §1.6
parity holds: probe is a first-class CLI verb; the TUI only *reflects* its result in the dot,
and the user's manual equivalent is `[s]` scan ([[cli-tui-parity]]).

**Submit-time dedup — "one pending probe per root".** The queue never dedups today (every
submission enqueued, §3 guarantee 1). Add a **narrow, probe-only** exception in
`JobQueue.submit` (or a pre-submit guard): if an un-started `probe` job for the same `root_id`
is already `status='queued'`, **skip the insert** and return that job's id.
- Match `type='probe' AND status='queued' AND root_id=?`. Do **not** dedup against a *running*
  probe (a fresh queued one after it is legitimate — files may have arrived after it started).
- Keep it probe-specific: scan/dedup/merge still enqueue freely (a second scan behind a first
  is intentional). Document why this one type dedups when nothing else does.
- This is what bounds the "100 roots" backlog: the scheduler enqueues one per root; a root whose
  probe from yesterday is still `queued` (worker backed up) gets a no-op today.

### Part B — general periodic-job scheduler infrastructure

**Framing:** build the scheduler the daemon was always meant to own (§3 lists "Scheduler
(APScheduler → interval scans)" as a daemon responsibility) — the plan *named* APScheduler here,
so this realizes it — **general** enough for future periodic work (scheduled `--full` scans per
§13 M8, audit pruning §8.1/§14 #5, embedding backfills §7), with **probe as its first client**.
There is **no scheduler in the codebase yet**; APScheduler becomes a **new core dependency**.

**Decided: APScheduler** (`BackgroundScheduler`), for future flexibility — cron expressions,
per-job jitter, misfire grace, and per-root independent schedules (the deferred §13 M8 goal)
come for free rather than being re-implemented on a raw thread. Add `apscheduler>=3.10` to
`pyproject.toml` `[project] dependencies` (pure-Python, no wheel risk — safe for the lean core,
unlike the `media` extras). `BackgroundScheduler` owns its own daemon thread, so it slots into
the existing daemon lifecycle exactly where `JobQueue`'s thread already does.

**Design — a thin `PeriodicScheduler` wrapper + a declarative `PeriodicTask` registry OVER
APScheduler** (so tasks stay declarative and the engine is swappable; the registry mirrors the
`JobSpec` pattern):

```python
# jobs/scheduler.py
@dataclass(frozen=True)
class PeriodicTask:
    name: str                       # 'probe-all', later 'full-scan-all', 'audit-prune'
    submit: Callable[[JobQueue, Database], None]     # enqueues the work (fan-out lives here)
    trigger: Callable[[Config], object]              # → an APScheduler trigger (interval/cron),
                                                     #   built from config so it's tunable per task
    enabled: Callable[[Config], bool] = lambda _c: True   # config gate / off-switch

PERIODIC_TASKS: list[PeriodicTask] = [PROBE_ALL_TASK]   # registry

class PeriodicScheduler:
    """Daemon-owned wrapper over APScheduler.BackgroundScheduler. Registers each enabled
    PeriodicTask as a job whose func calls task.submit(queue, db) through the normal queue."""
    def __init__(self, queue, db, config, tasks=PERIODIC_TASKS): ...
    def start(self): ...     # scheduler.add_job(...) per enabled task, then .start()
    def shutdown(self): ...  # scheduler.shutdown(wait=False) — symmetric with JobQueue.shutdown()
```

- **The probe task's `submit`** does the **fan-out**: query enabled library roots and
  `queue.submit("probe", {"root_id": r})` per root — the submit-dedup (Part A) makes re-firing
  before the last batch drained a no-op. So the scheduler stays generic; probe's "one job per
  root" policy lives in the task thunk + the dedup, **not** in APScheduler or the wrapper.
- **The trigger** for probe is `IntervalTrigger(hours=cfg.schedule.probe_interval_hours)` (24 by
  default), with a small `jitter` so 100 roots' probes don't thundering-herd the queue at the
  same instant. A future per-root or cron schedule is just a different `trigger` on a task —
  no scheduler change.
- **Wiring:** `build_app` (daemon/server.py) constructs `PeriodicScheduler(queue, db, config)`
  right after `JobQueue` + reconcile; `scheduler.start()` in the `@app.on_event("startup")` hook
  and `scheduler.shutdown()` in `@app.on_event("shutdown")` — symmetric with the `queue.shutdown()`
  already there. **Important:** APScheduler's job func runs on *its* thread and only calls
  `queue.submit(...)` (enqueue + pump) — it never runs job work itself, so the "one mutating job
  at a time" invariant (§3) is untouched; the scheduler is just another *client* submitting jobs,
  exactly as §3 describes ("the scheduler submits scan jobs like any client").
- **Use an in-memory jobstore (APScheduler default), NOT a persistent one.** The schedule is
  re-armed from `PERIODIC_TASKS` on every daemon start; a tick missed while the daemon was down
  just runs at the next fire. Probe is cheap + idempotent, so a missed/extra tick is harmless —
  **no persistent APScheduler jobstore / no durable schedule table.** (Durability lives in the
  *job queue*, which is where it matters — §3; the schedule itself is disposable.) Set
  `coalesce=True` + a `misfire_grace_time` so a backlog of missed fires collapses to one.
- **Config:** a `[schedule]` block (`probe_interval_hours = 24`, `probe_enabled = true`) tunable
  in `config.toml` (§9.2). Read when the scheduler arms its jobs at daemon start. (Interval edits
  apply on next daemon restart — acceptable for a background cadence; live reload can come later.)
- **Testability:** APScheduler makes this easy — construct the `PeriodicScheduler` with a task
  whose `submit` is a spy and call the registered func directly (or use APScheduler's
  `run_job`/a `MemoryJobStore` + manual trigger) to assert it enqueues through the queue, with no
  real-time wait. Also unit-test the probe task's fan-out (`submit`) independently of APScheduler:
  it's a plain function `(queue, db) -> None` that submits one probe per enabled library root.

### Part C — the 4-state status dot (a semantics change, not just a new state)

**Current** (`tui/tokens.py:status_dot(kind, last_scan_at, last_dedup_at)`, via
`render.root_dot`): 3-way — `◉` deduped (any `last_dedup_at`), `◐` scanned-only, `○` never.

**New** (4 states + **color**). Precedence is driven by `probe_new_count` + the two timestamps
— no `last_activity` needed (the count is self-clearing, so it *is* the "latest op is a
probe-with-news" signal):

| Dot | Color | Meaning | When |
|---|---|---|---|
| `◐` | grey | **new files probed** — unscanned files waiting | `probe_new_count > 0` |
| `○` | grey | **never** — never scanned | `probe_new_count == 0` AND no `last_scan_at` |
| `◉` | green | **deduped** — deduped after the latest scan | `probe_new_count == 0` AND `last_dedup_at > last_scan_at` |
| `◉` | yellow | **need dedup** — scanned, not (re-)deduped since | `probe_new_count == 0` AND scanned, not deduped-after-scan |

**Resolve order in `status_dot`** (the exact ladder — get this right, it's the subtle part):

```
1. probe_new_count > 0        → ◐ grey   (new files probed — outranks EVERY other state)
2. no last_scan_at            → ○ grey   (never scanned)
3. last_dedup_at > last_scan_at → ◉ green  (deduped after the latest scan)
4. else (scanned)             → ◉ yellow (need dedup)
```

**`probe_new_count > 0` is checked FIRST — above `never`.** A freshly-registered, never-scanned
root has `last_scan_at = NULL` and its first probe finds *every* file new (`count > 0`); it must
show ◐ "new files probed," not ○ "never." If `never` were rung 1 (an earlier draft), the NULL
`last_scan_at` would win and wrongly keep it ○. So "has unscanned files" outranks *all* the
scan/dedup states, including never. (The user's third example.)

**Decision #3 — the exact behavior, with all the user's worked cases:**
- **`probe_new_count > 0`** (a probe found unscanned files, no scan has consumed them) →
  **[grey] ◐**, from any prior state (never / green / yellow). A `scan` then clears the count →
  the dot moves on to never's successor rungs.
- **A found-no-new-file probe (`count = 0`) never changes the dot** — it skips rung 1 and falls
  through to the never/scan/dedup rungs, which read only the (probe-untouched) `last_scan_at`/
  `last_dedup_at`:
  - **never** root (empty folder), probe finds nothing → rung 1 skip → no `last_scan_at` →
    **stays ○ never** ✓ (the user's third example, negative half)
  - **green solid** (deduped), probe finds nothing → rung 1 skip → `dedup > scan` still true →
    **stays green solid ◉** ✓ (first example)
  - **yellow solid** (need-dedup), probe finds nothing → rung 1 skip → scanned, not deduped →
    **stays yellow solid ◉** ✓ (second example)
  This is *why* `last_activity` was dropped: rungs 2–4 depend only on the never/scan/dedup
  timestamps, which a probe never writes, so a count-0 probe is inherently a dot no-op. The count
  alone is the honest, self-correcting signal.
- **latest op is a scan** → the scan cleared `probe_new_count=0`, so rung 1 is skipped and the
  dot is green (if a later dedup beats it) or yellow — matching "a scan means re-dedup."

**Test all four rungs + every count-0-preserves case: never→never, green→green, yellow→yellow,
and the never→probe-finds-new→◐ transition.**

**Two behavior shifts beyond adding a state:**
1. **"deduped" is now recency-relative.** Today *any* `last_dedup_at` → green ◉, even if the
   root was scanned again afterward. New: green only when **dedup is newer than the latest scan**
   (`last_dedup_at > last_scan_at`); a scan after the last dedup drops it to yellow. This makes
   `status_dot` a **timestamp comparison**, not a truthiness check. Both timestamps are already
   on the roots snapshot (`queries.roots_snapshot` carries `last_scan_at` + `last_dedup_at`).
2. **Color.** The dot is a bare glyph today; the spec colors it. `tui/tokens.py` already has a
   `color(role)` map (`success`/`warn`/…) and the glyph tokens. Return a **(glyph, role)** pair
   (or a pre-colorized token) and let the colorizer paint green=`success`, yellow=`warn`,
   grey=dim. `◉` is now **both** green and yellow (color, not shape, splits deduped vs need-dedup);
   `◐` is the new grey probed-new state. Update the dot **legend/key** wherever it's explained
   (dashboard, roots interface).

**Signal plumbing:** `queries.roots_snapshot()` + `root_detail()` add `last_probe_at` +
`probe_new_count` (read the new `roots` columns; `last_scan_at`/`last_dedup_at` are already
there) and pass `last_scan_at`/`last_dedup_at`/`probe_new_count` to `status_dot`. Keep
`status_dot` **pure** (no DB) — same as today; the 4th state + color + precedence are pure
functions of those three values. Its unit tests (`tests/test_tui_*`, search
`status_dot`/`root_dot`) extend with the new cases (all four rungs + the two count-0 preserves).

### Scope / steps

1. **Schema:** `roots` gains `last_probe_at`, `probe_new_count` (2 columns — no `last_activity`).
   Patch the live DB (`ALTER TABLE roots ADD COLUMN …`); fresh DB gets them from `SCHEMA_SQL`.
2. **Factor the fast-path skip predicate** into a standalone helper callable by both scan and
   probe (if not already), so probe reuses it verbatim (no drift, no second copy).
3. **`jobs/probe.py`:** enumerate + count-new (no fingerprint), `JobSpec(owned_root=root_id)` —
   probe owns its root (decision #2), `result_json`, register in `daemon/server.py`'s job-module
   imports. On clean completion stamp `last_probe_at` + `probe_new_count` (may be 0); on offline,
   write nothing.
4. **`scan` completion** clears `probe_new_count=0` (Phase 5 persist, alongside `last_full_scan_at`).
5. **Queue submit-dedup for probe** (Part A) — one pending probe per root; probe-only, documented.
6. **`jobs/scheduler.py`** (Part B): add `apscheduler>=3.10` to `pyproject.toml` core deps;
   `PeriodicScheduler` wrapper over `BackgroundScheduler` + the `PeriodicTask` registry + the
   `probe-all` task (interval trigger + jitter, fan-out to per-root submits). Wire start/shutdown
   into `build_app`'s lifespan hooks (in-memory jobstore, `coalesce=True`). `[schedule]` config
   block (interval + enable).
7. **CLI + API:** `packrat probe <root>`/`--all`, `client.submit_probe`, `POST /probe` (fan-out
   for `--all`).
8. **Status dot** (Part C): 4-state + color + the 4-rung ladder in `tokens.status_dot`; thread
   `last_probe_at`/`probe_new_count` through `queries` + `render.root_dot`; update the legend;
   the recency-relative deduped predicate (`last_dedup_at > last_scan_at`).
9. **Tests:** probe counts new-only + writes NO fingerprint rows; scan clears the count;
   submit-dedup skips a 2nd queued probe but not a 2nd scan; a probe waits for a busy root
   (dequeue gate) and runs when it frees; the `probe-all` task's fan-out submits one probe per
   enabled library root (tested as a plain function, no APScheduler); the scheduler registers +
   fires the task through the queue (spy `submit`, no real-time wait); `probe --all` skips trash;
   the dot's four
   rungs incl. the green↔yellow recency flip; **probe-new shows ◐ from EVERY prior state incl.
   a never-scanned root (count>0 outranks `never`)**; and **a found-nothing probe (count 0)
   leaves the dot unchanged in all three cases: never→never, green→green, yellow→yellow**.
10. **Docs:** PLAN §8 (probe workflow, sibling to A2), §3 (the scheduler now exists — the §3
    "Scheduler (APScheduler)" line is now realized; describe `PeriodicScheduler`), §4 (roots
    columns), §9.1/§9.2 (APScheduler as a new core dep + the `[schedule]` block), §12 (dot legend
    + probe job card), §13 (milestone). Memory: a `probe-job` note (catalog-read-only +
    owns-its-root; one-pending-per-root submit-dedup; the count-driven dot precedence with no
    `last_activity`) and a `periodic-scheduler` note (APScheduler `BackgroundScheduler` + the
    `PeriodicTask` registry; in-memory jobstore; scheduler is just a queue *client*).

**Settled decisions** (all previously-open questions now locked):
- Probe **owns** its root (blocks other ops briefly) — reuses the dequeue gate; probe is
  sub-second-to-seconds so the block is negligible (Part A).
- The dot precedence needs **only `probe_new_count` + the two timestamps** — no `last_activity`
  column; a found-nothing probe is inherently a dot no-op (Part C).
- Scheduler = **APScheduler** (`BackgroundScheduler`, new core dep) for future flexibility
  (cron / jitter / per-root schedules), wrapped by a declarative `PeriodicTask` registry (Part B).
