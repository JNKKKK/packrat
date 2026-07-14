"""The background daemon: HTTP API, token auth, and race-free auto-spawn (§3)."""

from .client import DaemonClient, DaemonNotRunning
from .spawn import ensure_daemon, spawn_daemon

__all__ = ["DaemonClient", "DaemonNotRunning", "ensure_daemon", "spawn_daemon"]
