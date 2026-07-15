"""Entrypoint for the detached daemon process: ``python -m packrat.daemon``.

Spawned by :func:`packrat.daemon.spawn.spawn_daemon`. Binding the loopback port
inside :func:`run_daemon` is the single-instance lock (§3): if the port is taken,
this process exits and the client connects to the winner.

Logging goes to a **date-rotating** ``daemon.log`` (rolls at local midnight into
``daemon.log.YYYY-MM-DD`` backups) via :func:`_setup_logging`. uvicorn's own
handlers are disabled (``log_config=None`` in :func:`run_daemon`) so its access/
error records propagate to the root logger and land in the same rotating file.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler

from .. import paths
from .server import run_daemon

#: Keep this many days of rotated logs (0 = keep all). A cheap bound so the log
#: dir doesn't grow without limit; tune later if needed (no config knob in v1).
LOG_BACKUP_DAYS = 30


def _setup_logging() -> None:
    """Route all logging through a midnight-rotating ``daemon.log`` (UTC-agnostic).

    Attaches a :class:`TimedRotatingFileHandler` to the **root** logger so both
    packrat loggers and uvicorn's (which propagate once ``log_config=None``) share
    one dated file. ``delay=True`` so the file opens on first write, and the
    handler owns it exclusively so the midnight rename can't fail on a pinned fd.
    """
    handler = TimedRotatingFileHandler(
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


def main() -> int:
    # The raw stdout/stderr fds are redirected to daemon-bootstrap.log by spawn
    # (pre-logging / hard-crash output only). reconfigure to UTF-8 to be safe when
    # run in the foreground on a legacy Windows codepage.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    _setup_logging()
    return run_daemon()


if __name__ == "__main__":
    sys.exit(main())
