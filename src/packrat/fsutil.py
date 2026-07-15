r"""Filesystem path helpers — canonical form + long-path-safe I/O (§8 A1, §10.1).

Two concerns kept apart on purpose:

- **Canonical form** (:func:`canonicalize`) — the absolute, normalized string we
  **store** in ``roots.path`` / ``file_instances.path`` and compare for equality
  (§4: "``path`` is stored in the canonical long-path-safe form … so equality is
  well-defined"). It is a *plain* path (no ``\\?\`` prefix) so it stays legible in
  manifests / audit JSON / ``roots list``.

- **Extended form** (:func:`extended`) — the ``\\?\``-prefixed path used only at
  the moment of an actual open/``stat`` on Windows, so files whose path exceeds
  ``MAX_PATH`` (260) still hash/decode. The prefix never enters the DB.

Equality is well-defined because every code path that produces a path for storage
runs it through :func:`canonicalize`, and re-enumerating the same tree yields the
same bytes. Windows compares case-insensitively; we store the on-disk casing and
compare case-insensitively where the plan calls for it (overlap / name checks live
in :mod:`packrat.roots`, not here).
"""

from __future__ import annotations

import os
from pathlib import Path

_IS_WINDOWS = os.name == "nt"


def canonicalize(path: str | os.PathLike) -> str:
    r"""Return the absolute, normalized path we store and compare (§8 A1 step 1).

    No ``\\?\`` prefix — that is added just-in-time by :func:`extended` for I/O.
    Uses :func:`os.path.abspath` (absolute + ``normpath``) which collapses ``.``/
    ``..`` and unifies separators without touching the disk, so it is safe on a
    path that does not (yet) exist.
    """
    return os.path.abspath(os.fspath(path))


def extended(path: str | os.PathLike) -> str:
    r"""Return a long-path-safe string for an actual filesystem operation.

    On Windows, prefix an absolute path with ``\\?\`` (or ``\\?\UNC\`` for a UNC
    share) so the Win32 API bypasses the legacy ``MAX_PATH`` limit. Idempotent and
    a no-op off Windows / on already-prefixed paths.
    """
    p = os.fspath(path)
    if not _IS_WINDOWS or p.startswith("\\\\?\\"):
        return p
    ap = os.path.abspath(p)
    if ap.startswith("\\\\"):
        # UNC path: \\server\share\... -> \\?\UNC\server\share\...
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def paths_equal(a: str, b: str) -> bool:
    """Case-insensitive path equality on Windows, exact elsewhere."""
    if _IS_WINDOWS:
        return os.path.normcase(a) == os.path.normcase(b)
    return a == b


def is_within(child: str, parent: str) -> bool:
    r"""True if ``child`` is ``parent`` or nested inside it (canonical inputs).

    Used by the register overlap check (§8 A1 step 2). Compares on ``normcase`` so
    it is case-insensitive on Windows, and guards the segment boundary so
    ``C:\\foobar`` is not treated as inside ``C:\\foo``.
    """
    c = os.path.normcase(child.rstrip("\\/"))
    p = os.path.normcase(parent.rstrip("\\/"))
    if c == p:
        return True
    return c.startswith(p + os.sep) or c.startswith(p + "/")


def leaf_name(path: str) -> str:
    """The last path component (the default root handle, §8 A1 step 3)."""
    return Path(path).name


def is_network_path(path: str) -> bool:
    """True if ``path`` is on a UNC / network share (no Recycle Bin — §10).

    A best-effort classifier: a leading ``\\\\`` is UNC; a mapped drive letter is
    resolved via Win32 when available. M1 doesn't delete, so this is only used for
    report hints; the delete-path warning lands with dedup/cleanup (M3/M4).
    """
    if path.startswith("\\\\") and not path.startswith("\\\\?\\"):
        return True
    if path.startswith("\\\\?\\UNC\\"):
        return True
    return False
