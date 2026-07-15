"""Build-mode detection — gates dev-only surfaces (e.g. ``packrat dev clear-db``).

We want dev-only, destructive helpers to be **invisible in a release build** but
available when hacking on packrat. There's no formal release channel yet, so
"dev build" is detected two ways (either suffices):

1. **Explicit override** — ``$PACKRAT_DEV`` set to a truthy value. The escape
   hatch for any environment (CI, a packaged build you want to poke at).
2. **Source checkout heuristic** — the package imports from a working tree that
   still has the project's ``pyproject.toml`` at its root (``src/packrat`` →
   repo root two levels up). An installed wheel has no such file, so this is
   False for ``pip install packrat``.

Kept tiny and dependency-free so any module can call it cheaply.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

_TRUTHY = {"1", "true", "yes", "on"}


def _env_dev() -> bool | None:
    raw = os.environ.get("PACKRAT_DEV")
    if raw is None:
        return None
    return raw.strip().lower() in _TRUTHY


def _is_source_checkout() -> bool:
    """True if this package lives in the packrat source tree (not an installed wheel)."""
    try:
        # src/packrat/build.py → parents[2] is the repo root in a checkout.
        root = Path(__file__).resolve().parents[2]
        pyproject = root / "pyproject.toml"
        if not pyproject.is_file():
            return False
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return data.get("project", {}).get("name") == "packrat"
    except (OSError, ValueError, IndexError):
        return False


def is_dev_build() -> bool:
    """Whether dev-only commands/endpoints should be exposed.

    ``$PACKRAT_DEV`` (truthy/falsey) wins if set; otherwise fall back to the
    source-checkout heuristic. Setting ``PACKRAT_DEV=0`` force-disables dev mode
    even in a checkout (useful for testing the release path).
    """
    env = _env_dev()
    if env is not None:
        return env
    return _is_source_checkout()
