"""The daemon HTTP API (§3) — FastAPI on 127.0.0.1 with a loopback token.

Endpoints (all under token auth except ``/health``):
- ``GET  /health``            — liveness + version (unauthenticated).
- ``GET  /daemon``            — pid/port/uptime/in-flight job (``daemon status``).
- ``POST /jobs``              — submit a job; always enqueued (§3 durable queue).
- ``GET  /jobs``              — recent jobs list.
- ``GET  /jobs/{id}``         — one job's detail.
- ``GET  /jobs/{id}/stream``  — SSE progress/state stream (§3).
- ``POST /jobs/{id}/cancel``  — cooperative cancel (§3).
- ``GET  /status``            — global rollup snapshot (§11).
- ``GET  /roots``             — registered roots snapshot (§11).
- ``POST /roots``             — register a root; optional ``--scan`` (§8 A1).
- ``POST /scan``              — resolve a root arg + submit a scan job (§8 A2).
- ``POST /shutdown``          — graceful stop (§11 ``daemon stop``).

The app is built by :func:`build_app`, which wires the DB, config, queue, and runs
startup reconciliation *before* serving (§3). :func:`run_daemon` binds the fixed
loopback port — the bind itself is the single-instance lock of the auto-spawn
handshake (§3): if the port is taken, another daemon already won.
"""

from __future__ import annotations

import asyncio
import json
import logging

from .. import __version__, build as build_mod, config as config_mod, db as db_mod, paths, queries
from ..jobs import JobQueue
from ..jobs.reconcile import reconcile_on_startup
from ..util import now_iso
from . import token as token_mod
from .state import DEFAULT_PORT, HOST, current_state, clear_state

log = logging.getLogger("packrat.daemon")

# Import job type modules for their register_job() side effects.
from ..jobs import scan as _scan  # noqa: E402,F401
from ..jobs import dedup as _dedup  # noqa: E402,F401
from ..jobs import cleanup as _cleanup  # noqa: E402,F401
from ..jobs import trash_refresh as _trash_refresh  # noqa: E402,F401
from ..jobs import untrash as _untrash  # noqa: E402,F401

from .. import roots as roots_mod  # noqa: E402
from pydantic import BaseModel  # noqa: E402


class SubmitJobRequest(BaseModel):
    """Body for ``POST /jobs``. Defined at module scope so FastAPI can resolve
    the annotation under ``from __future__ import annotations`` (a locally-scoped
    model is invisible to ``get_type_hints`` and gets misread as a query param).
    """

    type: str
    params: dict = {}


class RegisterRootRequest(BaseModel):
    """Body for ``POST /roots`` (``roots register``, §8 A1). Module-scoped for the
    same FastAPI type-resolution reason as :class:`SubmitJobRequest`."""

    path: str
    name: str | None = None
    kind: str = "library"
    ignore_globs: list[str] = []
    scan: bool = False
    full: bool = False
    embed: bool = False


def build_app(token: str, *, db_file=None, config_path=None):
    """Construct the FastAPI app with all runtime wired up.

    ``db_file``/``config_path`` override the default locations (tests).
    """
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.responses import StreamingResponse

    # Ensure config exists (auto-create with defaults, §9.2) and init the schema.
    config_mod.ensure_config(config_path)
    db_mod.init_db(db_file).close()
    # The daemon's single shared write connection (accessed from the API thread
    # and the worker thread; serialized by Database's lock — §3 single writer).
    shared_conn = db_mod.connect(db_file, check_same_thread=False)
    database = db_mod.Database(shared_conn)

    def _load_config():
        return config_mod.load_config(config_path)

    queue = JobQueue(database, config_loader=_load_config)

    # Startup reconciliation BEFORE serving any request (§3): flip stale running
    # rows to interrupted + carve out queued destructive applies, then drain the
    # durable queued backlog (pump starts the first runnable job).
    reconcile_on_startup(database)
    queue.pump()

    app = FastAPI(title="packrat daemon", version=__version__)
    app.state.token = token
    app.state.db = database
    app.state.queue = queue
    app.state.started_at = now_iso()

    def require_token(authorization: str | None = Header(default=None)):
        expected = f"Bearer {token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="invalid or missing token")

    # -- unauthenticated liveness ------------------------------------
    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__}

    # -- daemon status -----------------------------------------------
    @app.get("/daemon", dependencies=[Depends(require_token)])
    def daemon_status():
        running_id = queue.running_job_id()
        running = queries.job_detail(running_id) if running_id else None
        return {
            "pid": current_state().pid,
            "port": DEFAULT_PORT,
            "version": __version__,
            "started_at": app.state.started_at,
            "running_job": running,
        }

    # -- jobs --------------------------------------------------------
    @app.post("/jobs", dependencies=[Depends(require_token)])
    def submit_job(body: SubmitJobRequest):
        # §3: every mutating submission is ENQUEUED (durable backlog) — never rejected
        # for a busy worker or held root (that is decided at dequeue). Only an unknown
        # job type is a client error.
        try:
            job_id = queue.submit(body.type, body.params)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"job_id": job_id}

    @app.get("/jobs", dependencies=[Depends(require_token)])
    def list_jobs(limit: int = 20):
        return {"jobs": queries.recent_jobs(limit)}

    # Static /jobs/* routes are declared BEFORE the /jobs/{job_id} catch-all so
    # FastAPI (first-match order) doesn't route "queued"/"cancel-queued" into the
    # int path param.
    @app.get("/jobs/queued", dependencies=[Depends(require_token)])
    def list_queued():
        """The durable FIFO backlog (§3/§12), oldest-first, with blocked reasons."""
        return {"queued": queries.status_snapshot().get("queued", [])}

    @app.post("/jobs/cancel-queued", dependencies=[Depends(require_token)])
    def cancel_queued():
        """Drop every queued job from the backlog (TUI ``[x]``); running one untouched."""
        return {"dropped": queue.cancel_all_queued()}

    @app.get("/jobs/{job_id}", dependencies=[Depends(require_token)])
    def get_job(job_id: int):
        detail = queries.job_detail(job_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="no such job")
        return detail

    @app.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_token)])
    def cancel_job(job_id: int):
        ok = queue.cancel(job_id)
        return {"cancelled": ok}

    @app.get("/roots/{root_id}/jobs", dependencies=[Depends(require_token)])
    def root_jobs(root_id: int, limit: int = 50):
        """One root's current + historical jobs, newest-first (§12 per-root panel)."""
        return {"jobs": queries.root_jobs(root_id, limit)}

    @app.get("/jobs/{job_id}/stream", dependencies=[Depends(require_token)])
    async def stream_job(job_id: int):
        detail = queries.job_detail(job_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="no such job")

        sub = queue.subscribe(job_id)
        loop = asyncio.get_event_loop()

        async def event_gen():
            # If the job is already TERMINAL, emit its final state and close so a
            # late attach doesn't hang forever (SSE degrades gracefully — §3).
            current = queries.job_detail(job_id)
            if current and current["status"] not in ("running", "queued"):
                yield _sse({"job_id": job_id, "type": "state",
                            "status": current["status"],
                            "total": current["total"], "done": current["done"]})
                queue.unsubscribe(sub)
                return
            # A QUEUED job: emit its waiting/blocked state up front (so the client
            # can render `queued · waiting for worker` / `queued · blocked: …`) and
            # KEEP the stream open — the queue broadcasts `running` when it starts.
            if current and current["status"] == "queued":
                holder = queue.blocked_reason(current["type"], json.loads(current["params_json"] or "{}"))
                yield _sse({"job_id": job_id, "type": "state", "status": "queued",
                            "blocked": holder})
            try:
                while True:
                    ev = await loop.run_in_executor(None, sub.q.get)
                    if ev is None:  # sentinel: job finished, subscribers closed
                        break
                    yield _sse(ev.to_dict())
            finally:
                queue.unsubscribe(sub)

        return StreamingResponse(event_gen(), media_type="text/event-stream")

    # -- read-only snapshots -----------------------------------------
    @app.get("/status", dependencies=[Depends(require_token)])
    def status(root: str | None = None):
        if root:
            detail = queries.root_detail(root)
            if detail is None:
                raise HTTPException(status_code=404, detail=f"no root at path or named {root!r}")
            return {"root_detail": detail}
        return queries.status_snapshot()

    @app.get("/roots", dependencies=[Depends(require_token)])
    def roots():
        return {"roots": queries.roots_snapshot()}

    @app.post("/roots", dependencies=[Depends(require_token)])
    def register_root(body: RegisterRootRequest):
        """Register a folder as a root (§8 A1); optionally kick off a scan (--scan)."""
        try:
            row = roots_mod.register(
                database, body.path, name=body.name, kind=body.kind,
                ignore_globs=body.ignore_globs or None,
            )
        except roots_mod.RootError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        job_id = None
        if body.scan and row["kind"] == "library":
            # §3: the scan is enqueued (it drains when the worker frees / the root
            # clears) — never rejected, so there is no "scan_busy" path anymore.
            job_id = queue.submit(
                "scan",
                {"root_id": row["id"], "full": body.full, "embed": body.embed},
            )
        return {"root": row, "job_id": job_id}

    @app.post("/scan", dependencies=[Depends(require_token)])
    def submit_scan(body: dict):
        """Resolve a root arg (path/--name, §11) and submit a scan job (§8 A2).

        ``--all`` submits with no ``root_id`` (owns no root; iterates + skips busy
        roots). A manual scan resolves the arg here so the CLI stays a thin client.
        """
        is_all = bool(body.get("all"))
        params = {
            "all": is_all,
            "full": bool(body.get("full")),
            "embed": bool(body.get("embed")),
            "dry_run": bool(body.get("dry_run")),
            "profile": bool(body.get("profile")),
        }
        if not is_all:
            arg = body.get("root")
            if not arg:
                raise HTTPException(status_code=400, detail="scan needs a <root> or --all")
            try:
                row = roots_mod.resolve_root(database, arg)
            except roots_mod.RootError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            if row["kind"] == "trash":
                raise HTTPException(
                    status_code=400,
                    detail=f"{row['name']!r} is a trash root; scan never indexes trash folders",
                )
            params["root_id"] = row["id"]
        return {"job_id": queue.submit("scan", params)}

    @app.post("/dedup", dependencies=[Depends(require_token)])
    def submit_dedup(body: dict):
        """Resolve a root arg (path/--name) + submit a dedup job (§8 B).

        Modes are carried in params: default analyze, or ``--confirm``/``--cancel``
        (act on the pending run) / ``--dry-run`` (analyze without staging). A trash
        root is rejected here so the CLI stays a thin client.
        """
        arg = body.get("root")
        if not arg:
            raise HTTPException(status_code=400, detail="dedup needs a <folder> (path or --name)")
        try:
            row = roots_mod.resolve_root(database, arg)
        except roots_mod.RootError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if row["kind"] != "library":
            raise HTTPException(
                status_code=400,
                detail=f"{row['name']!r} is a {row['kind']} root; dedup targets a library root",
            )
        params = {
            "root_id": row["id"],
            "confirm": bool(body.get("confirm")),
            "cancel": bool(body.get("cancel")),
            "dry_run": bool(body.get("dry_run")),
            "keep_suggested": bool(body.get("keep_suggested")),
        }
        return {"job_id": queue.submit("dedup", params)}

    @app.post("/cleanup", dependencies=[Depends(require_token)])
    def submit_cleanup(body: dict):
        """Resolve a root arg (path/--name) + submit a cleanup job (§6.2, §9.1).

        ``mode`` ∈ ``exact`` | ``perceptual`` | ``undecodable`` (one required for a
        fresh op — the CLI enforces exactly-one via its three flags). Sub-verbs carried
        in params: preview (no sub-verb) / ``apply`` (one-shot delete, submitted after
        the typed count-confirm) / ``confirm`` / ``cancel`` (perceptual run) / ``dry_run``.
        A trash root is rejected here so the CLI stays a thin client.
        """
        arg = body.get("root")
        if not arg:
            raise HTTPException(status_code=400, detail="cleanup needs a <folder> (path or --name)")
        try:
            row = roots_mod.resolve_root(database, arg)
        except roots_mod.RootError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if row["kind"] != "library":
            raise HTTPException(
                status_code=400,
                detail=f"{row['name']!r} is a {row['kind']} root; cleanup targets a library root",
            )
        mode = body.get("mode") or "exact"
        if mode not in ("exact", "perceptual", "undecodable"):
            raise HTTPException(status_code=400, detail=f"unknown cleanup mode {mode!r}")
        params = {
            "root_id": row["id"],
            "mode": mode,
            "confirm": bool(body.get("confirm")),
            "cancel": bool(body.get("cancel")),
            "dry_run": bool(body.get("dry_run")),
            "apply": bool(body.get("apply")),
        }
        return {"job_id": queue.submit("cleanup", params)}

    @app.get("/cleanup/preview", dependencies=[Depends(require_token)])
    def cleanup_preview(root: str, mode: str = "exact"):
        """Read-only count for a one-shot cleanup mode's typed confirm (§6.2, §9.1)."""
        prev = queries.cleanup_exact_preview(root, mode)
        if prev is None:
            raise HTTPException(status_code=404, detail=f"no root at path or named {root!r}")
        return prev

    @app.post("/trash/refresh", dependencies=[Depends(require_token)])
    def submit_trash_refresh(body: dict):
        """Submit a ``trash refresh`` job (§6.1) — absorb + empty the trash roots."""
        return {"job_id": queue.submit("trash-refresh", {})}

    @app.post("/untrash", dependencies=[Depends(require_token)])
    def submit_untrash(body: dict):
        """Submit an ``untrash`` job (§6.3) — forget content from trash memory by hash.

        ``<path>`` is arbitrary bytes to hash (NOT a root), passed through verbatim.
        """
        path = body.get("path")
        if not path:
            raise HTTPException(status_code=400, detail="untrash needs a <path> to hash")
        params = {"path": path, "dry_run": bool(body.get("dry_run"))}
        return {"job_id": queue.submit("untrash", params)}

    # -- dev-only helpers (registered only in a dev build) -----------
    if build_mod.is_dev_build():

        @app.post("/dev/clear-db", dependencies=[Depends(require_token)])
        def dev_clear_db():
            """Empty every catalog table (dev-only). Refuses while a job runs.

            Registered only when :func:`packrat.build.is_dev_build` is true, so a
            release build never exposes this route at all.
            """
            if queue.running_job_id() is not None:
                raise HTTPException(
                    status_code=409,
                    detail="a job is running; stop/cancel it before clearing the DB",
                )
            counts = database.clear_catalog()
            return {"cleared": counts, "total_rows": sum(counts.values())}

    # -- shutdown ----------------------------------------------------
    @app.post("/shutdown", dependencies=[Depends(require_token)])
    async def shutdown():
        # Graceful stop (§3): signal a running job to checkpoint (cooperative
        # cancel would set 'cancelled'; a clean stop wants 'interrupted'). We
        # request the running job to stop cooperatively but reconciliation on
        # next start lands it as 'interrupted' — matching §3's "stop is a
        # resumable interruption, not a cancel." We stop serving after replying.
        running_id = queue.running_job_id()
        # Schedule the actual process exit shortly after the response is sent.
        asyncio.get_event_loop().call_later(0.2, _stop_server, app)
        return {"stopping": True, "running_job": running_id}

    return app


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _stop_server(app):
    server = getattr(app.state, "_uvicorn_server", None)
    if server is not None:
        server.should_exit = True


def run_daemon(*, db_file=None, config_path=None, port: int = DEFAULT_PORT) -> int:
    """Run the daemon in the foreground (the detached process's entrypoint).

    Binds the loopback port (single-instance lock). Writes the token *before*
    accepting requests, then the daemon-state file. Returns a process exit code.
    """
    import uvicorn

    # 1. Generate + write the token BEFORE we start serving (§3 handshake).
    token = token_mod.generate_token()

    app = build_app(token, db_file=db_file, config_path=config_path)

    # log_config=None: don't let uvicorn install its own handlers. Its loggers
    # then propagate to the root logger, whose date-rotating handler
    # (packrat.daemon.__main__._setup_logging) owns daemon.log — so access/error
    # lines land in the same midnight-rotated file as packrat's own logs.
    config = uvicorn.Config(
        app, host=HOST, port=port, log_level="info", loop="asyncio", log_config=None
    )
    server = uvicorn.Server(config)
    app.state._uvicorn_server = server

    # uvicorn binds the socket in server.run(); if the port is taken, another
    # daemon already won the race — we exit non-zero and the client connects to
    # the winner (§3 bind-or-connect). Write token/state only after a successful
    # bind, via the lifespan startup hook below.
    @app.on_event("startup")
    def _on_start():
        token_mod.write_token(token, paths.token_path())
        current_state().write()
        log.info("packrat daemon up on %s:%d (pid=%d)", HOST, port, current_state().pid)

    @app.on_event("shutdown")
    def _on_stop():
        clear_state()
        # Signal a running job to checkpoint and join it before closing the DB.
        # Its row is reconciled to 'interrupted' on next start (§3 clean-stop).
        app.state.queue.shutdown()
        app.state.db.close()
        log.info("packrat daemon stopped")

    try:
        server.run()
    except OSError as exc:
        # Address already in use → a daemon already owns the port. Not an error
        # from the user's perspective; the client will connect to the winner.
        log.info("could not bind %s:%d (%s) — another daemon likely won", HOST, port, exc)
        return 3
    return 0
