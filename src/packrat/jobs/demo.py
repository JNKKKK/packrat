"""The M0 ``demo`` job — a real, cancellable, progress-emitting job.

M0 ships the job *runtime* before any actual operation (scan/dedup/merge land in
M1+). This job exercises every runtime property end-to-end: it emits progress,
respects cooperative cancellation at its checkpoints, and can be interrupted by a
daemon stop/crash (leaving an ``interrupted`` row that reconciliation flips). It
owns no root, so it never trips per-root exclusivity. It is also what the M0
verification and tests drive.
"""

from __future__ import annotations

import time

from .context import JobContext
from .registry import JobSpec, register_job


def _run_demo(ctx: JobContext) -> None:
    steps = int(ctx.params.get("steps", 10))
    delay = float(ctx.params.get("delay_s", 0.2))
    ctx.set_total(steps)
    ctx.log(f"demo job: {steps} steps @ {delay}s")
    for i in range(steps):
        # Cooperative cancellation checkpoint (§9).
        ctx.check_cancelled()
        time.sleep(delay)
        ctx.progress(i + 1, message=f"step {i + 1}/{steps}")
    ctx.log("demo complete")


register_job(
    JobSpec(
        type="demo",
        handler=_run_demo,
        mutating=True,
        owned_root=None,
    )
)
