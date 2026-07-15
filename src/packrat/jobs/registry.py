"""Job type registry (§3).

Each job type declares:
- ``handler``: ``(JobContext) -> None`` — the work, run on the single worker thread.
- ``mutating``: whether it takes the global single-worker slot (§3 guarantee 1).
  Read-only jobs don't exist yet (status/roots are plain HTTP), so all M0 jobs mutate.
- ``owned_root``: ``(params) -> root_id | None`` — the root this job *owns* for
  per-root exclusivity (§3 guarantee 2). ``None`` means it owns no root
  (e.g. ``untrash``, or ``scan --all`` which iterates roots rather than owning one).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .context import JobContext


@dataclass(frozen=True)
class JobSpec:
    type: str
    handler: Callable[[JobContext], None]
    mutating: bool = True
    owned_root: Callable[[dict], int | None] | None = None


_REGISTRY: dict[str, JobSpec] = {}


def register_job(spec: JobSpec) -> None:
    if spec.type in _REGISTRY:
        raise ValueError(f"job type {spec.type!r} already registered")
    _REGISTRY[spec.type] = spec


def get_job_spec(job_type: str) -> JobSpec | None:
    return _REGISTRY.get(job_type)


def known_job_types() -> list[str]:
    return sorted(_REGISTRY)
