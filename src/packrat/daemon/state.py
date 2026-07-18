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
    """Best-effort liveness check for a pid (cross-platform).

    **Windows note:** ``os.kill(pid, 0)`` is NOT a liveness probe here — CPython maps
    signal ``0`` to ``CTRL_C_EVENT`` and calls ``GenerateConsoleCtrlEvent``, which
    returns regardless of whether the process exists. So on Windows we ask the kernel
    directly via ``OpenProcess`` + ``WaitForSingleObject`` (ctypes, no dependency):
    a signaled or un-openable handle ⇒ dead. This matters for :func:`terminate_pid`'s
    post-kill confirmation, which polls this.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except OSError as exc:
        import errno

        # EPERM means the process exists but we can't signal it → alive.
        return exc.errno == errno.EPERM
    return True


def _pid_alive_windows(pid: int) -> bool:
    """Windows liveness via Win32 (see :func:`pid_alive`)."""
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    WAIT_TIMEOUT = 0x00000102  # handle NOT signaled → process still running

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    handle = k32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Can't open → gone (ERROR_INVALID_PARAMETER) or inaccessible; treat as dead.
        return False
    try:
        # WaitForSingleObject(handle, 0): WAIT_TIMEOUT ⇒ not signaled ⇒ alive; any other
        # result (WAIT_OBJECT_0=signaled/exited, or an error) ⇒ dead. Reliable even for a
        # terminated-but-not-reaped process (a live handle keeps the pid valid → the wait
        # correctly reports it signaled), unlike GetExitCodeProcess's STILL_ACTIVE ambiguity.
        return k32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        k32.CloseHandle(handle)


def pid_on_port(port: int = DEFAULT_PORT) -> int | None:
    """PID of the process LISTENING on ``port`` (loopback), or ``None`` (§3 self-heal).

    The daemon binds a **fixed** loopback port as its single-instance lock (§3), so
    whatever listens there IS the packrat daemon — nothing else uses this port. Used to
    force-stop an **orphaned** daemon whose token no longer matches ours (e.g. one
    spawned under a since-deleted ``PACKRAT_HOME`` during testing): the authed
    stop/restart can't reach it, but we can still find + kill it by port.

    OS-level (independent of the daemon's code version, unlike an API self-report), so
    it recovers orphans running *older* code too. Windows parses ``netstat -ano``; POSIX
    tries ``lsof``. Best-effort — returns ``None`` if the tool is absent or nothing listens.
    """
    import subprocess

    try:
        if os.name == "nt":
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout
            suffix = f":{port}"
            for line in out.splitlines():
                parts = line.split()
                # e.g. ["TCP", "127.0.0.1:51789", "0.0.0.0:0", "LISTENING", "42504"]
                if len(parts) >= 5 and parts[0].upper() == "TCP" \
                        and parts[3].upper() == "LISTENING" and parts[1].endswith(suffix):
                    try:
                        return int(parts[-1])
                    except ValueError:
                        continue
            return None
        # POSIX (dev courtesy — project is Windows-primary).
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip()
        for tok in out.split():
            try:
                return int(tok)
            except ValueError:
                continue
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def terminate_pid(pid: int, *, timeout_s: float = 5.0) -> bool:
    """Forcibly terminate a process by pid; return ``True`` once it's gone (§3 self-heal).

    Cross-platform: ``taskkill /F`` on Windows, ``SIGTERM`` then ``SIGKILL`` on POSIX.
    Polls up to ``timeout_s`` for the process to exit. An already-dead / invalid pid is
    a no-op success. Used to recover an orphaned daemon we can't stop via the API.
    """
    import time

    if pid <= 0:
        return True
    if not pid_alive(pid):
        return True
    try:
        if os.name == "nt":
            import subprocess

            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10, check=False)
        else:
            import signal

            os.kill(pid, signal.SIGTERM)
    except (OSError, Exception):  # noqa: BLE001 - kill is best-effort; we poll below
        pass

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)

    if os.name != "nt":  # last resort on POSIX
        try:
            import signal

            os.kill(pid, signal.SIGKILL)
            time.sleep(0.2)
        except OSError:
            pass
    return not pid_alive(pid)


def current_state() -> DaemonState:
    from .. import __version__

    return DaemonState(
        pid=os.getpid(),
        port=DEFAULT_PORT,
        started_at=now_iso(),
        version=__version__,
    )
