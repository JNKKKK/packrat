"""Daemon runtime state: pid + bound port (§3, §11 ``daemon status``).

Written by the daemon at startup (after it binds and writes the token), read by
clients to find the API and by ``daemon status``. Kept separate from the token
so a stale state file (dead daemon) can be detected and cleaned.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import paths
from ..util import now_iso

#: Fixed loopback port for the daemon API. The auto-spawn handshake binds this
#: port as the single-instance lock (§3): whoever binds it is the daemon.
DEFAULT_PORT = 51789
HOST = "127.0.0.1"


@dataclass
class DaemonState:
    pid: int
    port: int
    started_at: str
    version: str

    def write(self, path: Path | None = None) -> Path:
        p = path or paths.daemon_state_path()
        p.write_text(json.dumps(asdict(self)), encoding="utf-8")
        return p


def read_state(path: Path | None = None) -> DaemonState | None:
    p = path or paths.daemon_state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return DaemonState(**data)
    except (OSError, ValueError, TypeError):
        return None


def clear_state(path: Path | None = None) -> None:
    p = path or paths.daemon_state_path()
    try:
        p.unlink()
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    """Best-effort liveness check for a pid (cross-platform)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        import errno

        # EPERM means the process exists but we can't signal it → alive.
        return exc.errno == errno.EPERM
    return True


def current_state() -> DaemonState:
    from .. import __version__

    return DaemonState(
        pid=os.getpid(),
        port=DEFAULT_PORT,
        started_at=now_iso(),
        version=__version__,
    )
