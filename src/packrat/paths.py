"""Filesystem locations packrat owns, all under ``%APPDATA%\\packrat`` (§3, §9.2).

Everything the daemon persists outside the collection lives here: the SQLite DB,
the loopback ``token`` file, ``config.toml``, the review audit trail, and daemon
runtime state (pid/port). Centralized so every module agrees on the layout and
so tests can redirect it with ``PACKRAT_HOME``.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Env var to override the packrat home dir (used by tests and for a portable
#: install). When unset we fall back to ``%APPDATA%\packrat`` on Windows, or
#: ``~/.packrat`` elsewhere (dev on non-Windows).
HOME_ENV = "PACKRAT_HOME"


def home_dir() -> Path:
    """Return the packrat home directory, creating it if needed.

    Resolution order:
    1. ``$PACKRAT_HOME`` if set (tests, portable installs).
    2. ``%APPDATA%\\packrat`` on Windows (the documented v1 location, §3).
    3. ``~/.packrat`` as a cross-platform dev fallback.
    """
    override = os.environ.get(HOME_ENV)
    if override:
        base = Path(override)
    else:
        appdata = os.environ.get("APPDATA")
        if appdata:
            base = Path(appdata) / "packrat"
        else:
            base = Path.home() / ".packrat"
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_path() -> Path:
    """``config.toml`` — tunable knobs, auto-created with defaults (§9.2)."""
    return home_dir() / "config.toml"


def token_path() -> Path:
    """Loopback API token, written by the daemon before it serves (§3)."""
    return home_dir() / "token"


def db_path() -> Path:
    """The SQLite catalog (WAL). The crown jewel (§10)."""
    return home_dir() / "packrat.db"


def daemon_state_path() -> Path:
    """JSON with the running daemon's pid + bound port (for ``daemon status``)."""
    return home_dir() / "daemon.json"


def logs_dir() -> Path:
    """The daemon log directory (``logs/``), holding the rolling + bootstrap logs."""
    d = home_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def daemon_log_path() -> Path:
    """The daemon's rolling log — the *active* file for today (``logs/daemon.log``).

    Owned exclusively by a ``TimedRotatingFileHandler`` (see
    :func:`packrat.daemon.__main__._setup_logging`) that rotates at local midnight
    into dated backups (``logs/daemon.log.YYYY-MM-DD``). Not an fd-redirect target —
    see :func:`daemon_bootstrap_log_path` for that.
    """
    return logs_dir() / "daemon.log"


def daemon_bootstrap_log_path() -> Path:
    """Where the detached daemon's raw stdout/stderr fds are redirected.

    ``logs/daemon-bootstrap.log``. Kept separate from :func:`daemon_log_path` so
    the rotating handler can own ``daemon.log`` exclusively (a Windows midnight
    rename fails if another handle pins the file). Captures only pre-logging /
    hard-crash output — startup tracebacks, C-level faults — that never went
    through Python ``logging``.
    """
    return logs_dir() / "daemon-bootstrap.log"


def audit_dir() -> Path:
    """Root of the review-run audit trail: ``audit/{run_type}/{root}/{run_id}`` (§8.1)."""
    d = home_dir() / "audit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def backups_dir() -> Path:
    """DB backups taken before every destructive op (§10)."""
    d = home_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d
