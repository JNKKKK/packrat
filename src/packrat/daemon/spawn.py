"""Race-free auto-spawn of the detached daemon (§3).

The client does **bind-or-connect**, not check-then-spawn, so two clients racing
to start the daemon converge on **one**:
1. Try to connect (``/health``). If up → done.
2. Else spawn ``python -m packrat.daemon`` as a **detached** process and poll
   ``/health`` until it comes up (or times out). The daemon binds the fixed
   loopback port on startup; that bind is the single-instance lock — a loser
   simply fails to bind and exits, and the winner answers everyone's poll.

On Windows the child is spawned **windowless**: with ``pythonw.exe`` (the
GUI-subsystem interpreter, which cannot own a console) when available, plus
``CREATE_NO_WINDOW`` | ``CREATE_NEW_PROCESS_GROUP`` so it runs fully in the
background and outlives the launching terminal (§3, §11: "killing the terminal …
none touch the running job"). Its stdout/stderr go to ``daemon.log``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

from .. import paths
from .client import DaemonClient
from .state import DEFAULT_PORT

log = logging.getLogger("packrat.daemon.spawn")

# Windows process-creation flags (defined here so this imports on non-Windows).
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def _windowless_executable() -> str:
    """Return the interpreter to spawn the daemon with.

    On Windows prefer ``pythonw.exe`` — the GUI-subsystem interpreter, which by
    design has no console, so no window ever appears. Fall back to the current
    ``python.exe`` (with ``CREATE_NO_WINDOW`` doing the hiding) if it's missing.
    """
    exe = sys.executable
    if os.name == "nt":
        candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


def spawn_daemon() -> None:
    """Launch the background daemon process (does not wait for it to bind)."""
    log_file = paths.daemon_log_path()
    logf = open(log_file, "a", encoding="utf-8")  # noqa: SIM115 - handed to child
    kwargs: dict = {
        "stdout": logf,
        "stderr": logf,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        # CREATE_NO_WINDOW hides the console; CREATE_NEW_PROCESS_GROUP detaches
        # from the terminal's Ctrl-C group so closing/Ctrl-C'ing the launching
        # terminal never signals the daemon. (Note: DETACHED_PROCESS is
        # deliberately NOT combined with CREATE_NO_WINDOW — they conflict.)
        kwargs["creationflags"] = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen([_windowless_executable(), "-m", "packrat.daemon"], **kwargs)
    log.info("spawned background daemon (log: %s)", log_file)


def ensure_daemon(*, timeout_s: float = 20.0, port: int = DEFAULT_PORT) -> DaemonClient:
    """Return a client for a live daemon, auto-spawning one if needed (§3).

    Bind-or-connect: connect first; on failure spawn and poll ``/health`` until
    the winner answers. Raises :class:`TimeoutError` if nothing comes up.
    """
    client = DaemonClient(port=port)
    if client.is_up():
        return _with_token(client, port)

    spawn_daemon()

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        probe = DaemonClient(port=port)
        if probe.is_up():
            return _with_token(probe, port)
        time.sleep(0.25)

    raise TimeoutError(f"daemon did not come up within {timeout_s}s (see {paths.daemon_log_path()})")


def _with_token(client: DaemonClient, port: int) -> DaemonClient:
    """Re-read the token once the daemon is up (it writes it on startup, §3)."""
    from . import token as token_mod

    return DaemonClient(port=port, token=token_mod.read_token())
