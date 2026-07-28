"""Entrypoint for the detached daemon process: ``python -m packrat.daemon``.

Spawned by :func:`packrat.daemon.spawn.spawn_daemon`. Binding the loopback port
inside :func:`run_daemon` is the single-instance lock (§3): if the port is taken,
this process exits and the client connects to the winner.

Logging goes to a **date-rotating** ``daemon.log`` (rolls at local midnight into
``daemon.log.YYYY-MM-DD`` backups) via :func:`_setup_logging`, which
:func:`run_daemon` calls only after winning the single-instance port bind. uvicorn's
own handlers are disabled (``log_config=None`` in :func:`run_daemon`) so its error
records propagate to the root logger and land in the same rotating file; its
per-request *access* logger is quieted to ``WARNING`` (poll noise). The rotating
handler tolerates a Windows locked-rename at midnight (see
:class:`_SafeTimedRotatingFileHandler`) instead of looping forever.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

from .. import paths
from .server import run_daemon

#: Keep this many days of rotated logs (0 = keep all). A cheap bound so the log
#: dir doesn't grow without limit; tune later if needed (no config knob in v1).
LOG_BACKUP_DAYS = 30


class _SafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """A midnight-rotating handler that survives a locked rename on Windows.

    The stdlib ``doRollover`` closes the stream, renames ``daemon.log`` →
    ``daemon.log.YYYY-MM-DD``, and only advances ``rolloverAt``/reopens the stream
    **after** the rename returns. On Windows the rename raises ``WinError 32`` if
    *any* process still holds the file open (an overlapping daemon instance, an AV
    scanner, the search indexer). When it raises, ``rolloverAt`` is never advanced
    and the stream is left ``None``, so **every subsequent record** retries the
    rollover and — via ``logging.Handler.handleError`` — dumps a full traceback to
    stderr. In the detached daemon that stderr is redirected into
    ``daemon-bootstrap.log`` (spawn.py), which then balloons without bound while
    ``daemon.log`` freezes at the failed-rollover instant.

    We override :meth:`rotate` to **never raise**: a failed rename is logged once
    to stderr and swallowed, so ``doRollover`` runs to completion — ``rolloverAt``
    advances and the stream reopens (appending to the still-present base file). The
    only cost is that that one day isn't split into its own dated backup, which is
    strictly better than the runaway-flood failure it replaces.
    """

    def rotate(self, source: str, dest: str) -> None:
        try:
            # os.replace overwrites an existing dest atomically (doRollover already
            # removes it, but be robust if a prior skipped day left one behind).
            os.replace(source, dest)
        except OSError as exc:
            # Do NOT re-raise: that strands rolloverAt and loops forever (see above).
            print(f"packrat: daemon.log rollover skipped ({exc})", file=sys.stderr, flush=True)


def _setup_logging() -> None:
    """Route all logging through a midnight-rotating ``daemon.log`` (UTC-agnostic).

    Attaches a :class:`_SafeTimedRotatingFileHandler` to the **root** logger so both
    packrat loggers and uvicorn's (which propagate once ``log_config=None``) share
    one dated file. ``delay=True`` so the file opens on first write. Only the daemon
    that WON the single-instance port bind calls this (from :func:`run_daemon`), so a
    race-losing daemon never opens ``daemon.log`` and can't be the process pinning it
    across our midnight rename.

    uvicorn's **access** logger is also nudged to ``WARNING`` here as a fallback (it
    emits one record per HTTP request, and the TUI/CLI poll ``/health``/``/jobs``/
    ``/status`` continuously — the per-poll spam that previously filled the log). Note
    this ``setLevel`` alone does *not* stick under a live server: uvicorn's
    ``configure_logging()`` resets ``uvicorn.access`` back to its ``log_level`` during
    ``server.run()``. The authoritative kill is ``access_log=False`` on the
    ``uvicorn.Config`` in :func:`run_daemon`; this line only covers a standalone
    ``_setup_logging()`` (e.g. tests). Startup/error lines (``uvicorn.error``) stay INFO.
    """
    handler = _SafeTimedRotatingFileHandler(
        paths.daemon_log_path(),
        when="midnight",
        backupCount=LOG_BACKUP_DAYS,
        encoding="utf-8",
        delay=True,
    )
    # Rotated backups get the calendar date they cover: daemon.log.2026-07-14.
    handler.suffix = "%Y-%m-%d"
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Idempotent: never stack a second handler if this is somehow re-entered.
    if not any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    # Drop uvicorn's per-request access log — pure poll noise for a local daemon.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def main() -> int:
    # The raw stdout/stderr fds are redirected to daemon-bootstrap.log by spawn
    # (pre-logging / hard-crash output only). reconfigure to UTF-8 to be safe when
    # run in the foreground on a legacy Windows codepage.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    # NOTE: _setup_logging() is deliberately NOT called here. run_daemon() calls it
    # only after winning the single-instance port bind, so a race-losing daemon never
    # opens daemon.log and can't be the process pinning it across the midnight rename.
    return run_daemon()


if __name__ == "__main__":
    sys.exit(main())
