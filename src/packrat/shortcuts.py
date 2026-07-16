r"""Windows shell primitives for dedup/cleanup staging (§8 B Phase 4/6, §14 #4).

Two operations the review workflow needs, kept behind thin wrappers so their
Windows-only deps import lazily (the runtime stays importable on a non-Windows
dev box, per :mod:`packrat.paths`):

- :func:`create_shortcut` — write a ``.lnk`` pointing at a real file via the
  ``IShellLink`` COM interface. Explorer shows the target's thumbnail/preview for
  such a shortcut, which is the whole point of staging shortcuts instead of copies
  (no extra disk, live preview). Confirmed working from the daemon's worker thread
  (spike, §14 #4): pure COM, **not** ``win32com.client.Dispatch``.
- :func:`recycle` — move a file to the Recycle Bin via ``send2trash``. ⚠ It must
  be given the **plain** canonical path — the ``\\?\`` extended form is rejected
  (spike). On a NAS/SMB share there is no Recycle Bin, so this deletes
  **permanently** (§10); callers warn before confirming.

:func:`read_shortcut_target` resolves a ``.lnk`` back to its target — used only by
tests / diagnostics, not the confirm path (which keys off shortcut *presence*, §8 B
Phase 5).
"""

from __future__ import annotations

import logging

log = logging.getLogger("packrat.shortcuts")


def create_shortcut(lnk_path: str, target_path: str) -> None:
    r"""Create a ``.lnk`` at ``lnk_path`` pointing at ``target_path`` (§8 B Phase 4).

    Both paths are plain (non-extended) canonical strings. COM is initialized for
    the calling thread for the duration of the call (idempotent + refcounted, so
    safe to call many times on the worker thread). Raises on failure — the caller
    treats a shortcut it could not create as a staging error.
    """
    import pythoncom
    from win32com.shell import shell  # type: ignore

    pythoncom.CoInitialize()
    try:
        sl = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink
        )
        sl.SetPath(target_path)
        try:
            import os

            sl.SetWorkingDirectory(os.path.dirname(target_path))
        except Exception:  # noqa: BLE001 - working dir is cosmetic
            pass
        sl.QueryInterface(pythoncom.IID_IPersistFile).Save(lnk_path, 0)
    finally:
        pythoncom.CoUninitialize()


def read_shortcut_target(lnk_path: str) -> str | None:
    """Resolve a ``.lnk``'s target path (tests/diagnostics; not the confirm path)."""
    import pythoncom
    from win32com.shell import shell  # type: ignore

    pythoncom.CoInitialize()
    try:
        sl = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink
        )
        sl.QueryInterface(pythoncom.IID_IPersistFile).Load(lnk_path, 0)
        path, _ = sl.GetPath(shell.SLGP_UNCPRIORITY)
        return path or None
    except Exception as exc:  # noqa: BLE001
        log.debug("could not read shortcut %s: %s", lnk_path, exc)
        return None
    finally:
        pythoncom.CoUninitialize()


def recycle(path: str) -> None:
    r"""Move ``path`` to the Recycle Bin (§8 B Phase 6, §10).

    ``path`` must be the **plain** canonical form (send2trash rejects ``\\?\``).
    On a network/SMB root there is no Recycle Bin → this is a **permanent** delete
    (§10); the caller warns first. Raises ``FileNotFoundError`` if the file is gone
    (the confirm path stats first, so this is a belt-and-suspenders signal).
    """
    from send2trash import send2trash  # type: ignore

    send2trash(path)
