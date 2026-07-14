"""Loopback API token (§3).

The daemon writes a random token to ``%APPDATA%\\packrat\\token`` *before* it
accepts requests, so clients authenticate against a live server. Clients read it
to authorize. Not a security boundary against local users — it just prevents
other processes from stumbling onto the loopback API.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from .. import paths


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def write_token(token: str, path: Path | None = None) -> Path:
    p = path or paths.token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write then restrict; on Windows the ACL is inherited, good enough for v1.
    p.write_text(token, encoding="utf-8")
    return p


def read_token(path: Path | None = None) -> str | None:
    p = path or paths.token_path()
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
