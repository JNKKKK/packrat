# `probe` — cheap discovery: is there anything new here worth a scan?

```
packrat probe "D:\Backup\iPhone"     # count new/changed files; write NO fingerprints
packrat probe --all                  # probe every enabled library root (one job each)
```

`probe` (`jobs/probe.py`) splits the two halves `scan` conflates — *discovery* (walk + notice
new paths; seconds, no per-file I/O) and *fingerprinting* (hash + decode + PDQ; the multi-hour
cost). Probe is **discovery-only**: it answers one question about a root — *are there files here
we haven't scanned yet?* — and records a per-root "new files waiting" signal (the TUI surfaces it
as a status-dot state, see [tui](tui.md)), so the user learns a root needs a `scan` without remembering they
dropped files in. Runs every 24 h per root via the scheduler (see [architecture](architecture.md)); never blocks the user (press
`[s]`/`packrat scan` to scan now). CLI-exposed; **no** TUI keyshortcut (background-only).

**What it does.** Reuse scan's `enumerate_root(root_path, ignore)` (the walk + allowlist + ignore
filter — Phase 1) to build the candidate list, then per candidate apply scan's **existing
fast-path skip predicate** (path + exact size + tolerant mtime + "fully fingerprinted", step 4)
to decide *known* vs *new/changed*. Count the news. **No BLAKE3, no decode, no PDQ, no
`assets`/`phash`/`vphash` writes** — that is exactly the line between probe and scan. The
predicate is factored into a shared helper (`scan.load_existing_instances` +
`scan.is_fastpath_hit`) that both scan and probe call, so **"probe says N" ⇒ "scan would
fingerprint ≥ N"** holds by construction — no second copy of the rule.

- **"New"** = a candidate with no matching live `file_instances` row, OR a matching row whose
  size/mtime drifted past tolerance (a changed file also needs re-scanning).
- Probe does **no deletion-detection** (that mutates the catalog — scan's job, needs the full
  pass). Probe is **read-only on the catalog**; its only write is the per-root signal.
- **Trash roots:** never probed (scan never touches `kind='trash'` — see [trash refresh](operation-trash-refresh.md)); `probe --all`
  iterates `enabled=1 AND kind='library'`.
- **Offline / unreadable root** (SMB blip, see [SMB/NAS performance](performance.md)): report `root_offline`, write **no** signal —
  absence of a readable listing ≠ "no new files"; never let an unreachable root read as "clean".

**Per-root exclusivity — probe OWNS its root.** `owned_root=root_id`, so a probe **waits in the
backlog until its root is idle** (no running scan/dedup/cleanup/merge on it) via the existing
dequeue gate (see [architecture](architecture.md) guarantee 2), exactly like scan. Rationale for N per-root probe jobs over one
`probe --all` sweep: a single sweep owns no root and *iterates*, so at a busy root it must either
skip it (a silent miss) or stall the whole sweep; N per-root jobs sidestep it — each is an
independent queue entry the scheduler holds until *its* root frees. The cost ("100 roots → 100
jobs / 24 h") is bounded by the submit-dedup below. Probe is sub-second-to-seconds, so the brief
block it imposes is negligible; non-destructive → reconcile drains a queued probe normally, an
interrupted running probe just re-runs (idempotent — recomputes from scratch, writes nothing else).

**The signal.** On **clean completion** (not offline): set `last_probe_at=now` and
`probe_new_count=<n found>` — which may be **0** (found nothing). Writing 0 is correct and
important: it means "a probe ran and there's nothing unscanned," so the dot stays whatever the
scan/dedup state says (see [tui](tui.md)). **A completed `scan` clears `probe_new_count=0`** (Phase 5, alongside
`last_full_scan_at`; skipped for a dry-run or an offline root) — the news are now fingerprinted.
Because the count is self-clearing, `count > 0` *is* the "latest meaningful op is a
probe-with-news" state — no `last_activity` column needed, and it stays honest if the user deletes
the new files and re-probes (re-enumeration finds 0 → dot reverts).

**Submit-time dedup — "one pending probe per root".** The queue never dedups in general (every
submission is enqueued — see [architecture](architecture.md) guarantee 1). A **narrow, probe-only** exception in `JobQueue.submit`
(`_dedup_probe`): if an un-started `probe` job for the same `root_id` is already `status='queued'`,
skip the insert and return that job's id. Match `queued` only — **not** a *running* probe (a fresh
queued one after it is legitimate; files may have arrived after it started). This bounds the "100
roots" backlog: a root whose probe from yesterday is still `queued` (worker backed up) gets a no-op
today. Scan/dedup/merge still enqueue freely.

**Result / CLI / API.** `result_json`: `{op:"probe", new_count, root_offline, candidates}` for the
[tui](tui.md) job card. `packrat probe <root>`/`--all`/`--detach`/`--json`, `client.submit_probe` (returns a
list of job ids), `POST /probe` (does the `--all` fan-out to per-root submissions). [goals and concepts](goals-and-concepts.md) parity
holds: probe is a first-class CLI verb; the TUI only *reflects* its result in the dot, and the
user's manual equivalent is `[s]` scan.

---
