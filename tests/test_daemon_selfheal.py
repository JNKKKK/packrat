r"""Self-healing an orphaned daemon (§3) — the port→pid→kill recovery path.

An orphaned daemon (spawned under a since-deleted ``PACKRAT_HOME``, e.g. during
testing) answers unauthenticated ``/health`` but rejects our token, so the authed
``daemon stop``/``restart`` gets a 401. The fixed loopback port is packrat's
single-instance lock, so whatever listens there IS the daemon — we recover it by
finding the listener's pid and force-killing it.

Tests split by dependency:
- ``pid_on_port`` / ``terminate_pid`` against a **real** short-lived listener /
  process (OS-level, so cross-version; Windows-primary but the primitives are
  cross-platform).
- ``_is_auth_error`` + ``_force_kill_orphan`` as pure logic with the OS calls
  monkeypatched, so the CLI wiring is covered without killing a real process.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time

from packrat.daemon.state import DEFAULT_PORT, pid_on_port, terminate_pid


# ---------------------------------------------------------------------------
# pid_on_port — find the listener on a port (OS-level)
# ---------------------------------------------------------------------------
def test_pid_on_port_finds_a_real_listener():
    """A socket this process is LISTENING on resolves back to our own pid."""
    import os

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))  # ephemeral free port
        sock.listen(1)
        port = sock.getsockname()[1]
        found = pid_on_port(port)
        # netstat/lsof may be absent in a bare CI image → None is tolerated; when present
        # it must attribute the listener to THIS process.
        if found is not None:
            assert found == os.getpid()
    finally:
        sock.close()


def test_pid_on_port_none_when_free():
    # Bind+close to obtain a port nothing is listening on, then query it.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    time.sleep(0.05)
    assert pid_on_port(port) is None


# ---------------------------------------------------------------------------
# terminate_pid — force-kill a process (OS-level)
# ---------------------------------------------------------------------------
def test_terminate_pid_kills_a_real_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert proc.poll() is None  # alive
        assert terminate_pid(proc.pid) is True
        assert proc.poll() is not None  # gone
    finally:
        if proc.poll() is None:
            proc.kill()


def test_terminate_pid_noop_on_dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    # Already exited → terminate is a no-op success.
    assert terminate_pid(proc.pid) is True


def test_terminate_pid_invalid():
    assert terminate_pid(0) is True
    assert terminate_pid(-1) is True


# ---------------------------------------------------------------------------
# _is_auth_error — the 401 discriminator
# ---------------------------------------------------------------------------
def test_is_auth_error():
    from packrat.cli.main import _is_auth_error
    from packrat.daemon.client import DaemonError

    assert _is_auth_error(DaemonError('401: {"detail":"invalid or missing token"}')) is True
    assert _is_auth_error(DaemonError("404: no such job")) is False
    assert _is_auth_error(DaemonError("500: boom")) is False


# ---------------------------------------------------------------------------
# _force_kill_orphan — the CLI self-heal helper (OS calls monkeypatched)
# ---------------------------------------------------------------------------
def test_force_kill_orphan_finds_and_kills(monkeypatch, packrat_home):
    from packrat.cli import main as cli

    killed = {}
    monkeypatch.setattr(cli, "pid_on_port", lambda port=DEFAULT_PORT: 42504)
    monkeypatch.setattr(cli, "terminate_pid", lambda pid: killed.setdefault("pid", pid) or True)
    assert cli._force_kill_orphan(reason="test") is True
    assert killed["pid"] == 42504


def test_force_kill_orphan_no_listener(monkeypatch):
    from packrat.cli import main as cli

    monkeypatch.setattr(cli, "pid_on_port", lambda port=DEFAULT_PORT: None)
    # Nothing on the port → nothing to heal.
    assert cli._force_kill_orphan(reason="test") is False


def test_force_kill_orphan_terminate_fails(monkeypatch):
    from packrat.cli import main as cli

    monkeypatch.setattr(cli, "pid_on_port", lambda port=DEFAULT_PORT: 999999)
    monkeypatch.setattr(cli, "terminate_pid", lambda pid: False)
    assert cli._force_kill_orphan(reason="test") is False
