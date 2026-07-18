"""HTTP client the CLI/TUI use to drive the daemon (§3).

Thin wrapper over ``httpx`` that attaches the loopback token and exposes the
daemon endpoints. SSE streaming is handled by :meth:`DaemonClient.stream_job`,
which yields decoded progress events; a dropped stream is the caller's cue to
reconnect (job state is durable — §3).
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx

from .state import DEFAULT_PORT, HOST
from . import token as token_mod


class DaemonNotRunning(Exception):
    """Raised when the daemon cannot be reached (and auto-spawn is not requested)."""


class DaemonError(Exception):
    """A non-2xx response from the daemon that isn't a structured 'busy'."""


class DaemonClient:
    def __init__(self, *, port: int = DEFAULT_PORT, token: str | None = None, timeout: float = 30.0):
        self.base = f"http://{HOST}:{port}"
        self.token = token or token_mod.read_token()
        self.timeout = timeout

    def _headers(self) -> dict:
        h = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    # -- liveness --------------------------------------------------------
    def health(self) -> dict:
        try:
            r = httpx.get(f"{self.base}/health", timeout=2.0)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, OSError) as exc:
            raise DaemonNotRunning(str(exc)) from exc

    def is_up(self) -> bool:
        try:
            self.health()
            return True
        except DaemonNotRunning:
            return False

    # -- daemon ----------------------------------------------------------
    def daemon_status(self) -> dict:
        return self._get("/daemon")

    def shutdown(self) -> dict:
        return self._post("/shutdown", {})

    def clear_db(self) -> dict:
        """Empty the catalog (dev-only). Raises :class:`DaemonError` if the route
        is absent (release build → 404) or a job is running (409)."""
        return self._post("/dev/clear-db", {})

    # -- jobs ------------------------------------------------------------
    def submit(self, job_type: str, params: dict | None = None) -> int:
        return int(self._post("/jobs", {"type": job_type, "params": params or {}})["job_id"])

    def get_job(self, job_id: int) -> dict:
        return self._get(f"/jobs/{job_id}")

    def list_jobs(self, limit: int = 20) -> list[dict]:
        return self._get(f"/jobs?limit={limit}")["jobs"]

    def queued_jobs(self) -> list[dict]:
        """The durable FIFO backlog (§3/§12), oldest-first, with blocked reasons."""
        return self._get("/jobs/queued")["queued"]

    def root_jobs(self, root_id: int, limit: int = 50) -> list[dict]:
        """One root's current + historical jobs, newest-first (§12 per-root panel)."""
        return self._get(f"/roots/{root_id}/jobs?limit={limit}")["jobs"]

    def cancel_job(self, job_id: int) -> bool:
        return bool(self._post(f"/jobs/{job_id}/cancel", {})["cancelled"])

    def cancel_queued(self) -> int:
        """Drop every queued job from the backlog (§12 ``[x]``); returns count dropped."""
        return int(self._post("/jobs/cancel-queued", {})["dropped"])

    def stream_job(self, job_id: int) -> Iterator[dict]:
        """Yield SSE progress events until the job reaches a terminal state."""
        with httpx.stream(
            "GET", f"{self.base}/jobs/{job_id}/stream",
            headers=self._headers(), timeout=None,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload:
                    continue
                yield json.loads(payload)

    # -- roots + scan ----------------------------------------------------
    def register_root(
        self,
        path: str,
        *,
        name: str | None = None,
        kind: str = "library",
        ignore_globs: list[str] | None = None,
        scan: bool = False,
        full: bool = False,
        embed: bool = False,
    ) -> dict:
        """Register a root (§8 A1). Returns ``{root, job_id}`` (``job_id`` set with --scan).

        A ``RootError`` from the daemon comes back as HTTP 400 → :class:`DaemonError`
        carrying the validation message.
        """
        return self._post(
            "/roots",
            {
                "path": path, "name": name, "kind": kind,
                "ignore_globs": ignore_globs or [],
                "scan": scan, "full": full, "embed": embed,
            },
        )

    def submit_scan(
        self,
        root: str | None = None,
        *,
        all_roots: bool = False,
        full: bool = False,
        embed: bool = False,
        dry_run: bool = False,
        profile: bool = False,
    ) -> int:
        """Submit a scan job (§8 A2); returns the job id.

        Always enqueued (§3 durable queue) — a busy worker / held root no longer
        rejects, so the only failure is a validation error (:class:`DaemonError`).
        """
        return int(self._post(
            "/scan",
            {"root": root, "all": all_roots, "full": full, "embed": embed,
             "dry_run": dry_run, "profile": profile},
        )["job_id"])

    def submit_dedup(
        self,
        folder: str,
        *,
        confirm: bool = False,
        cancel: bool = False,
        dry_run: bool = False,
        keep_suggested: bool = False,
    ) -> int:
        """Submit a dedup job (§8 B); returns the job id (always enqueued, §3)."""
        return int(self._post(
            "/dedup",
            {"root": folder, "confirm": confirm, "cancel": cancel, "dry_run": dry_run,
             "keep_suggested": keep_suggested},
        )["job_id"])

    def submit_cleanup(
        self,
        folder: str,
        *,
        mode: str = "exact",
        confirm: bool = False,
        cancel: bool = False,
        dry_run: bool = False,
        apply: bool = False,
    ) -> int:
        """Submit a cleanup job (§6.2, §9.1); returns the job id (always enqueued, §3).

        ``mode`` ∈ ``exact`` | ``perceptual`` | ``undecodable``.
        """
        return int(self._post(
            "/cleanup",
            {"root": folder, "mode": mode, "confirm": confirm,
             "cancel": cancel, "dry_run": dry_run, "apply": apply},
        )["job_id"])

    def cleanup_preview(self, folder: str, mode: str = "exact") -> dict:
        """Read-only count for a one-shot cleanup mode's confirm (§6.2, §9.1)."""
        return self._get(f"/cleanup/preview?root={folder}&mode={mode}")

    def submit_merge(self, source: str, into: str, *, dry_run: bool = False) -> int:
        """Submit a merge job (§8 C); returns the job id (always enqueued, §3).

        ``into`` is the ``--into`` dest (a root name or a subfolder path); the daemon
        resolves it to the containing library root. A ``RootError`` (dest under no
        library root / a trash root) comes back as HTTP 400 → :class:`DaemonError`.
        """
        return int(self._post(
            "/merge", {"source": source, "into": into, "dry_run": dry_run},
        )["job_id"])

    def submit_trash_refresh(self) -> int:
        """Submit a ``trash refresh`` job (§6.1); returns the job id (always enqueued)."""
        return int(self._post("/trash/refresh", {})["job_id"])

    def submit_untrash(self, path: str, *, dry_run: bool = False) -> int:
        """Submit an ``untrash`` job (§6.3); returns the job id (always enqueued)."""
        return int(self._post("/untrash", {"path": path, "dry_run": dry_run})["job_id"])

    # -- snapshots -------------------------------------------------------
    def status(self, root: str | None = None) -> dict:
        if root:
            return self._get(f"/status?root={root}")
        return self._get("/status")

    def roots(self) -> list[dict]:
        return self._get("/roots")["roots"]

    # -- helpers ---------------------------------------------------------
    def _get(self, path: str) -> dict:
        try:
            r = httpx.get(f"{self.base}{path}", headers=self._headers(), timeout=self.timeout)
        except (httpx.HTTPError, OSError) as exc:
            raise DaemonNotRunning(str(exc)) from exc
        if r.status_code >= 400:
            raise DaemonError(f"{r.status_code}: {r.text}")
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = self._raw_post(path, body)
        if r.status_code >= 400:
            raise DaemonError(f"{r.status_code}: {r.text}")
        return r.json()

    def _raw_post(self, path: str, body: dict) -> httpx.Response:
        try:
            return httpx.post(
                f"{self.base}{path}", json=body, headers=self._headers(), timeout=self.timeout
            )
        except (httpx.HTTPError, OSError) as exc:
            raise DaemonNotRunning(str(exc)) from exc
