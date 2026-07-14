"""Client-side progress streaming with Ctrl-C-detaches semantics (§3, §11).

Key property: the job runs **in the daemon, not the terminal**. Streaming just
renders SSE events. So:
- **Ctrl-C detaches the view; the job keeps running.** We catch KeyboardInterrupt,
  print "still running — type ``packrat`` to track or stop it", and return.
- Killing the terminal / closing SSH doesn't touch the job either (it's the
  daemon's thread).
"""

from __future__ import annotations

import sys

from ..daemon.client import DaemonClient


def _fmt_bar(done: int | None, total: int | None, width: int = 24) -> str:
    if not total:
        return f"{done or 0}"
    frac = min(1.0, (done or 0) / total)
    filled = int(frac * width)
    return f"[{'#' * filled}{'.' * (width - filled)}] {int(frac * 100):3d}%  {done}/{total}"


def stream_job(client: DaemonClient, job_id: int, *, label: str = "") -> str:
    """Stream a job's progress to the terminal until it terminates.

    Returns the final status string. On Ctrl-C, prints the detach notice and
    returns ``'detached'`` without stopping the job.
    """
    prefix = f"{label} " if label else ""
    last_status = "running"
    try:
        for ev in client.stream_job(job_id):
            etype = ev.get("type")
            if etype in ("progress",):
                bar = _fmt_bar(ev.get("done"), ev.get("total"))
                msg = ev.get("message", "")
                sys.stdout.write(f"\r{prefix}{bar}  {msg}".ljust(78))
                sys.stdout.flush()
            elif etype == "log":
                sys.stdout.write(f"\r{prefix}· {ev.get('message', '')}".ljust(78) + "\n")
                sys.stdout.flush()
            elif etype in ("state", "done", "error"):
                st = ev.get("status")
                if st:
                    last_status = st
                if etype in ("done", "error"):
                    break
        # Confirm the final state from the durable record.
        detail = client.get_job(job_id)
        last_status = detail.get("status", last_status)
        sys.stdout.write("\n")
        return last_status
    except KeyboardInterrupt:
        sys.stdout.write(
            f"\n{prefix}still running — the job keeps going in the daemon.\n"
            f"  type `packrat` to track or stop it, or `packrat jobs` to list.\n"
        )
        return "detached"
