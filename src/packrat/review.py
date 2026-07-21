r"""Shared plumbing for stateful review runs — dedup (M3) and cleanup (M4).

A "review run" stages candidate files as Explorer shortcuts under the target
root's ``_packrat_review\`` area, persists a crash-safe plan (``review_actions``),
pauses for the user, and applies on ``--confirm`` (§8 B, §6.2). This module owns
the pieces both operations share:

- **staging paths** — the ``_packrat_review\`` parent and the per-stage subfolders
  (dedup's ``_exact_dup_to_delete\`` / ``_suspect_recompression\`` /
  ``_with_minor_edits\``; cleanup's ``_perceptually_identified_trash\``). All are
  already in the scan ignore set (:data:`packrat.ignore.REVIEW_DIR`) so scan never
  indexes them.
- **audit trail** (§8.1) — the immutable ``proposed.json`` / ``applied.json`` under
  ``%APPDATA%\packrat\audit\{run_type}\{root}\{run_id}\``, written once and never
  edited, surviving DB loss.

Kept operation-agnostic so ``cleanup --perceptual`` reuses it verbatim in M4.
"""

from __future__ import annotations

import json
import logging
import os

from . import fsutil, paths

log = logging.getLogger("packrat.review")

#: The packrat-owned review area under each root (matches ignore.REVIEW_DIR).
REVIEW_DIR = "_packrat_review"

#: dedup per-stage staging subfolders (§8 B). Stage 1 is default-DELETE (named
#: `…_to_delete`); stages 2 & 3 are default-KEEP (content-named).
EXACT_DUP = "_exact_dup_to_delete"          # stage 1: byte-identical copies
SUSPECT_RECOMPRESSION = "_suspect_recompression"  # stage 2: recompressions + all video near-dups
WITH_MINOR_EDITS = "_with_minor_edits"      # stage 3: photo minor-edits/crops
#: All dedup stage folders, in stage order (index 0 == stage 1).
DEDUP_STAGE_FOLDERS = (EXACT_DUP, SUSPECT_RECOMPRESSION, WITH_MINOR_EDITS)
#: cleanup --perceptual staging subfolder (§6.2, M4).
PERCEPTUAL_TRASH = "_perceptually_identified_trash"


def staging_parent(root_path: str) -> str:
    r"""``<root>\_packrat_review`` — the shared review area (created on demand)."""
    return os.path.join(fsutil.canonicalize(root_path), REVIEW_DIR)


def staging_folder(root_path: str, name: str) -> str:
    r"""``<root>\_packrat_review\<name>`` for a specific staging subfolder."""
    return os.path.join(staging_parent(root_path), name)


def ensure_dir(path: str) -> None:
    """Create a staging directory (long-path safe)."""
    os.makedirs(fsutil.extended(path), exist_ok=True)


def remove_tree(path: str) -> None:
    """Recursively delete a staging subfolder if present (long-path safe).

    Used at finalize (delete the staged folders, keep the ``_packrat_review``
    parent) and by reconciliation's analyze-rollback. Never raises if absent.
    """
    import shutil

    ext = fsutil.extended(path)
    if os.path.isdir(ext):
        shutil.rmtree(ext, ignore_errors=True)


def path_exists(path: str) -> bool:
    """Long-path-safe existence check for a stored (plain) path."""
    return os.path.exists(fsutil.extended(path))


# ---------------------------------------------------------------------------
# audit trail (§8.1)
# ---------------------------------------------------------------------------
def audit_run_dir(run_type: str, root_name: str, run_id: int) -> str:
    r"""``audit\{run_type}\{root_name}\{run_id}\`` — one dir per run (§8.1).

    Created on demand. ``run_type`` ∈ ``dedup`` | ``cleanup-perceptual``.
    """
    base = paths.audit_dir()
    d = base / run_type / _safe_name(root_name) / str(run_id)
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _safe_name(name: str) -> str:
    r"""Sanitize a root name for use as a directory component.

    Keeps alnum + `` ._-`` (others → ``_``), then neutralizes a name that is all dots
    (``.`` / ``..``) — those are path-traversal components that would escape the audit
    dir (a root named ``..`` would write ``audit\{run_type}\..\{run_id}`` one level up).
    """
    keep = "".join(c if c.isalnum() or c in " ._-" else "_" for c in name).strip()
    if not keep or set(keep) <= {"."}:   # empty, ".", "..", "..." → not a usable dir name
        return "root"
    return keep


def write_audit(run_dir: str, filename: str, obj: dict) -> str:
    """Write an immutable audit JSON (``proposed.json`` / ``applied.json``, §8.1).

    **Crash-atomic:** serialize to a sibling ``.tmp`` file, ``fsync`` it, then
    ``os.replace`` it into place — so a crash/power-loss mid-write can never leave a
    truncated or empty ``proposed.json``/``applied.json`` (this is the forensic record
    §8.1 says must survive DB loss; a half-written one would be worse than none). The
    replace is atomic on both NTFS and POSIX, so a reader sees either the old file or
    the complete new one, never a partial.
    """
    p = os.path.join(run_dir, filename)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return p
