r"""The ``probe`` job (§8 A2b) — is there anything new here worth a scan?

Probe is scan's cheap *discovery* half without the multi-hour *fingerprinting*
half. It answers ONE question about a root — *are there files here we haven't
scanned yet?* — **without hashing, decoding, or PDQ-ing anything**, and records the
answer as a per-root signal (``roots.probe_new_count``) the TUI surfaces as a new
status-dot state (§12). Runs every 24 h per root via the periodic scheduler
(:mod:`packrat.jobs.scheduler`); also a first-class CLI verb (``packrat probe``).

What it does (all read-only on the catalog):
- **Enumerate** the root exactly as scan does (:func:`packrat.jobs.scan.enumerate_root`
  — same walk + allowlist + ignore-glob filter), so the candidate set is identical.
- **Count new/changed** by applying scan's *same* fast-path skip predicate
  (:func:`packrat.jobs.scan.is_fastpath_hit`) per candidate: a candidate with **no**
  matching live ``file_instances`` row, or one whose ``size``/``mtime`` drifted past
  tolerance, is *new/changed*. Because it reuses that predicate verbatim, "probe says
  N" ⇒ "a scan would fingerprint ≥ N" holds by construction.
- **No BLAKE3, no decode, no PDQ, no ``assets``/``phash``/``vphash`` writes**, and
  **no deletion-detection** (that mutates the catalog — scan's job, needs the full
  pass). Probe's only write is the per-root signal below.

The one write, on **clean completion** (not offline): set ``last_probe_at=now`` and
``probe_new_count=<n found>`` — which may be **0** (found nothing). Writing 0 is
correct and important: it means "a probe ran and there's nothing unscanned," so the
dot stays whatever the scan/dedup state says (§12 rung ladder). An **offline /
unreadable** root (SMB blip, §10.1) writes **nothing** — absence of a readable
listing ≠ "no new files"; an unreachable root must never read as "clean."

**Concurrency (§3).** Probe **owns its root** (``owned_root=root_id``), so a probe
waits in the durable backlog until its root is idle — reusing the dequeue gate
verbatim, exactly like ``scan <root>`` does. It is non-destructive, so reconcile
drains a queued probe normally; an interrupted running probe just re-runs
(idempotent — recomputes the count from scratch, writes nothing else). The queue's
submit-time dedup (:meth:`packrat.jobs.queue.JobQueue.submit`) caps the backlog at
**one queued probe per root**, which is what bounds the "100 roots → 100 jobs every
24 h" cost. ``probe --all`` is a CLI/scheduler convenience that expands to N per-root
submissions (each its own queue entry + gate), never a single root-less sweep job.
"""

from __future__ import annotations

import logging
import os

from .. import roots
from ..ignore import IgnoreSet
from ..util import now_iso
from .context import JobContext
from .registry import JobSpec, register_job
from .scan import enumerate_root, is_fastpath_hit, load_existing_instances

log = logging.getLogger("packrat.jobs.probe")


def _run_probe(ctx: JobContext) -> None:
    db = ctx.db
    root_id = ctx.params.get("root_id")
    row = db.query_one("SELECT * FROM roots WHERE id=?", (root_id,))
    if row is None:
        raise ValueError(f"no such root id: {root_id}")
    # Trash roots are never scanned (§6.1), so they are never probed either. (The API
    # /probe endpoint + `probe --all` filter to library roots, so this is a belt-and-
    # braces guard for a directly-submitted job.)
    if row["kind"] == "trash":
        raise ValueError(
            f"{row['name']!r} is a trash root; probe never inspects trash folders (§6.1)"
        )

    ctx.log(f"probing {row['name']} ({row['path']})")
    ignore = IgnoreSet.build(ctx.config, roots.ignore_globs_of(row))
    en = enumerate_root(row["path"], ignore)

    # Offline / unreadable root (§10.1): write NO signal — an unreachable root must
    # never be recorded as "0 new files" (that would wrongly clear a real pending
    # signal). Report it so the §12 job card shows the outcome.
    if en.root_offline:
        ctx.log(f"{row['name']} is offline/unreadable — no probe signal written.")
        ctx.set_result({
            "op": "probe", "root_id": root_id, "root_offline": True, "new_count": None,
            "summary": "root offline — no signal written",
        })
        return

    # Count new/changed using scan's SAME fast-path skip predicate (no fingerprinting).
    existing = load_existing_instances(db, int(row["id"]))
    tol = ctx.config.fastpath.mtime_tolerance_s
    ctx.set_total(len(en.candidates))
    new_count = 0
    for i, cand in enumerate(en.candidates, 1):
        rec = existing.get(os.path.normcase(cand.path))
        if not is_fastpath_hit(rec, cand, tol):
            new_count += 1
        # Progress every so often (probe is fast; avoid an event per file).
        if i % 500 == 0 or i == len(en.candidates):
            ctx.progress(i, message=f"{new_count} new/changed")
        ctx.check_cancelled()

    # Clean completion → stamp the signal (may be 0). This is probe's ONLY catalog-
    # adjacent write; last_full_scan_at / the scan/dedup timestamps are untouched.
    db.execute(
        "UPDATE roots SET last_probe_at=?, probe_new_count=? WHERE id=?",
        (now_iso(), new_count, int(row["id"])),
    )
    ctx.log(
        f"probe done: {new_count} new/changed of {len(en.candidates)} candidate(s) "
        f"— {'`packrat scan` to fingerprint them' if new_count else 'nothing to scan'}."
    )
    ctx.set_result({
        "op": "probe", "root_id": root_id, "root_offline": False,
        "new_count": new_count, "candidates": len(en.candidates),
        "summary": (f"{new_count} new/changed awaiting scan" if new_count
                    else "nothing new — up to date"),
    })


register_job(
    JobSpec(
        type="probe",
        handler=_run_probe,
        # Probe OWNS its root → held in the backlog until the root is idle (dequeue
        # gate, §3), exactly like `scan <root>`. Non-destructive → drains normally on
        # reconcile; idempotent on re-run.
        owned_root=lambda params: params.get("root_id"),
    )
)
