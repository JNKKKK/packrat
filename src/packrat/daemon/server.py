"""The daemon HTTP API (§3) — FastAPI on 127.0.0.1 with a loopback token.

Endpoints (all under token auth except ``/health``):
- ``GET  /health``            — liveness + version (unauthenticated).
- ``GET  /daemon``            — pid/port/uptime/in-flight job (``daemon status``).
- ``POST /jobs``              — submit a job; 409 on busy (§3).
- ``GET  /jobs``              — recent jobs list.
- ``GET  /jobs/{id}``         — one job's detail.
- ``GET  /jobs/{id}/stream``  — SSE progress/state stream (§3).
- ``POST /jobs/{id}/cancel``  — cooperative cancel (§3).
- ``GET  /status``            — global rollup snapshot (§11).
- ``GET  /roots``             — registered roots snapshot (§11).
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

from .. import __version__, config as config_mod, db as db_mod, paths, queries
from ..jobs import BusyError, JobQueue
from ..jobs.reconcile import reconcile_on_startup
from ..util import now_iso
from . import token as token_mod
from .state import DEFAULT_PORT, HOST, current_state, clear_state

log = logging.getLogger("packrat.daemon")

# Import job type modules for their register_job() side effects.
from ..jobs import demo as _demo  # noqa: E402,F401

from pydantic import BaseModel  # noqa: E402


class SubmitJobRequest(BaseModel):
    """Body for ``POST /jobs``. Defined at module scope so FastAPI can resolve
    the annotation under ``from __future__ import annotations`` (a locally-scoped
    model is invisible to ``get_type_hints`` and gets misread as a query param).
    """

    type: str
    params: dict = {}


def build_app(token: str, *, db_file=None, config_path=None):
    """Construct the FastAPI app with all runtime wired up.

    ``db_file``/``config_path`` override the default locations (tests).
    """
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.responses import JSONResponse, StreamingResponse

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

    # Startup reconciliation BEFORE serving any request (§3).
    reconcile_on_startup(database)

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
        try:
            job_id = queue.submit(body.type, body.params)
        except BusyError as exc:
            return JSONResponse(
                status_code=409,
                content={"error": "busy", "kind": exc.kind,
                         "message": str(exc), "holder": exc.holder},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"job_id": job_id}

    @app.get("/jobs", dependencies=[Depends(require_token)])
    def list_jobs(limit: int = 20):
        return {"jobs": queries.recent_jobs(limit)}

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

    @app.get("/jobs/{job_id}/stream", dependencies=[Depends(require_token)])
    async def stream_job(job_id: int):
        detail = queries.job_detail(job_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="no such job")

        sub = queue.subscribe(job_id)
        loop = asyncio.get_event_loop()

        async def event_gen():
            # If the job is already terminal, emit its final state and close so a
            # late attach doesn't hang forever (SSE degrades gracefully — §3).
            current = queries.job_detail(job_id)
            if current and current["status"] not in ("running",):
                yield _sse({"job_id": job_id, "type": "state",
                            "status": current["status"],
                            "total": current["total"], "done": current["done"]})
                queue.unsubscribe(sub)
                return
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
    def status():
        return queries.status_snapshot()

    @app.get("/roots", dependencies=[Depends(require_token)])
    def roots():
        return {"roots": queries.roots_snapshot()}

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

    config = uvicorn.Config(app, host=HOST, port=port, log_level="info", loop="asyncio")
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
