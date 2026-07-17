"""Human-readable job labels derived from ``type`` + ``params`` (§12).

Many operations submit several ``jobs`` rows of the *same* ``type`` distinguished
only by a param (dedup analyze / ``--confirm`` / ``--cancel``; a ``--trash-exact``
cleanup is a ``preview`` job then an ``apply`` job). A bare type is ambiguous, so
the queue/history label is ``<verb> <root-name> (<qualifier>)`` where the qualifier
is derived here from ``params`` — a **pure display** rule, no schema field.

Two display rules (§12): the root shows by **name** (not path); a ``(dry-run)`` job
is a preview (callers dim it). The lifecycle *status* (queued/running/done/…) is
rendered separately, so the qualifier stays a stable *noun* that reads correctly in
every state (never "Executing deletion"). ``untrash`` owns no root — it shows the
presented path's leaf; ``merge`` (M5) shows ``<src-leaf> → <dest-root>``.
"""

from __future__ import annotations

import os


def _leaf(path: str | None) -> str:
    if not path:
        return "?"
    return os.path.basename(path.rstrip("/\\").replace("\\", "/")) or path


def job_qualifier(job_type: str, params: dict) -> str:
    """Return the ``(...)`` qualifier body for a job (no surrounding parens), or ``""``.

    The bit that disambiguates same-type jobs. ``root_name`` is *not* included here
    (the caller composes ``<verb> <root> (<qualifier>)``); this is only the action.
    """
    p = params or {}
    dry = p.get("dry_run")

    if job_type == "scan":
        if p.get("all"):
            base = "all roots"
        else:
            base = ""
        flags = []
        if p.get("full"):
            flags.append("full")
        if p.get("embed"):
            flags.append("embed")
        if dry:
            flags.append("dry-run")
        # scan --all → "all roots (full · embed)"; scan <root> → "(full · embed)".
        joined = " · ".join(flags)
        if base and joined:
            return f"{base} · {joined}"
        return base or joined

    if job_type == "dedup":
        if p.get("cancel"):
            return "cancel"
        if p.get("confirm"):
            return "confirm · keep-suggested" if p.get("keep_suggested") else "confirm"
        if dry:
            return "dry-run"
        return "analyze"

    if job_type == "cleanup":
        mode = p.get("mode") or "exact"
        if p.get("cancel"):
            return "perceptual · cancel"
        if p.get("confirm"):
            return "perceptual · confirm"
        if mode == "perceptual":
            return "perceptual · dry-run" if dry else "perceptual · analyze"
        # exact / undecodable: preview job, apply job, or dry-run.
        if dry:
            return f"{mode} · dry-run"
        if p.get("apply"):
            return f"{mode} · delete"
        return f"{mode} · preview"

    if job_type == "trash-refresh":
        return ""

    if job_type == "untrash":
        return "dry-run" if dry else ""

    if job_type == "merge":  # M5
        return "dry-run" if dry else ""

    return "dry-run" if dry else ""


def job_verb(job_type: str) -> str:
    """The leading verb for a job label (``trash-refresh`` → ``trash refresh``)."""
    return "trash refresh" if job_type == "trash-refresh" else job_type


def job_label(job_type: str, params: dict, *, root_name: str | None = None,
              include_root: bool = True) -> str:
    """Compose the full display label ``<verb> [<root>] (<qualifier>)`` (§12).

    ``root_name`` is the resolved root handle (from ``jobs.root_id``); pass
    ``include_root=False`` in the per-root jobs panel where the header already names
    the root (rows there read just ``(exact · delete)``). ``untrash`` ignores
    ``root_name`` and shows the presented path's leaf instead (it owns no root).
    """
    p = params or {}
    verb = job_verb(job_type)
    qual = job_qualifier(job_type, p)

    if job_type == "untrash":
        target = _leaf(p.get("path"))
        label = f"{verb} {target}" if include_root else verb
    elif job_type == "merge":  # M5: "merge <src-leaf> → <dest-root>"
        src = _leaf(p.get("source") or p.get("source_path"))
        dest = root_name or "?"
        label = f"{verb} {src} → {dest}" if include_root else f"{verb} {src} →"
    elif job_type == "scan" and p.get("all"):
        label = verb  # "scan"; qualifier carries "all roots …"
    elif include_root and root_name:
        label = f"{verb} {root_name}"
    else:
        label = verb

    return f"{label} ({qual})" if qual else label
