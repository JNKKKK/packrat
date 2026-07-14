"""Entrypoint for the detached daemon process: ``python -m packrat.daemon``.

Spawned by :func:`packrat.daemon.spawn.spawn_daemon`. Binding the loopback port
inside :func:`run_daemon` is the single-instance lock (§3): if the port is taken,
this process exits and the client connects to the winner.
"""

from __future__ import annotations

import logging
import sys

from .server import run_daemon


def main() -> int:
    # stdout/stderr are redirected to the daemon.log file (opened UTF-8 by
    # spawn); reconfigure to be safe when run in the foreground on a legacy
    # Windows codepage.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run_daemon()


if __name__ == "__main__":
    sys.exit(main())
