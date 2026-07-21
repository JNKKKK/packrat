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
    r"""True if ``path`` is on a UNC / network share (no Recycle Bin — §10).

    Two cases, both meaning "no Recycle Bin → a delete here is PERMANENT" (§10,
    §10.1 — most roots live on the Synology NAS, commonly *mapped drives*):

    - **UNC** (``\\server\share\…`` or the extended ``\\?\UNC\…``) — a leading
      ``\\`` that is not the ``\\?\`` local extended prefix.
    - **Mapped network drive** (``Z:\…``) — a drive letter whose Win32
      :func:`GetDriveTypeW` reports ``DRIVE_REMOTE``. This is the case a pure UNC
      check misses, and it is exactly the "confirm summary must warn on a
      non-recyclable path" gate (§10). Resolved via ctypes (no dependency);
      any failure falls back to False (best-effort, never blocks the delete).
    """
    if path.startswith("\\\\") and not path.startswith("\\\\?\\"):
        return True
    if path.startswith("\\\\?\\UNC\\"):
        return True
    if _IS_WINDOWS:
        return _is_remote_drive(path)
    return False


#: GetDriveType return code for a network-mapped drive (winbase.h DRIVE_REMOTE).
_DRIVE_REMOTE = 4


def _is_remote_drive(path: str) -> bool:
    r"""True if ``path``'s drive letter is a mapped network drive (``DRIVE_REMOTE``).

    Strips any ``\\?\`` extended prefix, extracts the ``X:\`` root, and asks Win32
    ``GetDriveTypeW``. Non-drive-letter paths (already handled UNC, relative, or a
    device path) and any ctypes failure return False."""
    p = path
    if p.startswith("\\\\?\\"):
        p = p[4:]
    # Need a real drive-letter root ("X:") to classify.
    if len(p) < 2 or p[1] != ":" or not p[0].isalpha():
        return False
    root = f"{p[0]}:\\"
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == _DRIVE_REMOTE
    except Exception:  # noqa: BLE001 - classification is best-effort (§10)
        return False
