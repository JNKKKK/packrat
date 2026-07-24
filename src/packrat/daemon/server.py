"""The daemon HTTP API (§3) — FastAPI on 127.0.0.1 with a loopback token.

Endpoints (all under token auth except ``/health``):
- ``GET  /health``            — liveness + version (unauthenticated).
- ``GET  /daemon``            — pid/port/uptime/in-flight job (``daemon status``).
- ``POST /jobs``              — submit a job; always enqueued (§3 durable queue).
- ``GET  /jobs``              — recent jobs list.
- ``GET  /jobs/{id}``         — one job's detail.
- ``GET  /jobs/{id}/stream``  — SSE progress/state stream (§3).
- ``POST /jobs/{id}/cancel``  — cooperative cancel (§3).
- ``POST /jobs/{id}/prioritize`` — bump a queued job to the front (§11).
- ``GET  /status``            — global rollup snapshot (§11).
- ``GET  /roots``             — registered roots snapshot (§11).
- ``POST /roots``             — register a root; optional ``--scan`` (§8 A1).
- ``POST /scan``              — resolve a root arg + submit a scan job (§8 A2).
- ``POST /merge``             — resolve ``--into`` + submit a merge job (§8 C).
- ``POST /shutdown``          — graceful stop (§11 ``daemon stop``).

:func:`run_daemon` binds the fixed loopback port **first** — the bind is the
single-instance lock of the auto-spawn handshake (§3): if the port is taken, another
daemon already won and this process exits immediately having touched no shared state.
Only *after* winning the bind does it build the app (:func:`build_app` wires the DB,
config, queue, and runs startup reconciliation) and write the token — so a losing
daemon in a spawn race can never corrupt the winner's in-flight job or clobber its
token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue as _queue

#: How often the SSE stream wakes to heartbeat / re-check terminal state (seconds).
#: Bounds how long a still-running-but-quiet stream holds an executor thread between
#: liveness checks; also the max lag before a dropped sentinel is noticed.
_SSE_HEARTBEAT_S = 15.0

from .. import __version__, build as build_mod, config as config_mod, db as db_mod, paths, queries
from ..jobs import JobQueue
from ..jobs.reconcile import reconcile_on_startup
from ..util import now_iso
from . import token as token_mod
from .state import DEFAULT_PORT, HOST, current_state, clear_state

log = logging.getLogger("packrat.daemon")

# Import job type modules for their register_job() side effects.
from ..jobs import scan as _scan  # noqa: E402,F401
from ..jobs import probe as _probe  # noqa: E402,F401
from ..jobs import dedup as _dedup  # noqa: E402,F401
from ..jobs import cleanup as _cleanup  # noqa: E402,F401
from ..jobs import merge as _merge  # noqa: E402,F401
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


def _resolve_root_or_400(database, arg: str, *, require_kind: str, verb: str) -> dict:
    """Resolve a root arg to a row of ``require_kind`` or raise the HTTP error (§8 A1/§11).

    The submit routes that target a specific root (scan/dedup/cleanup/trash-refresh)
    share this: 404 if the arg resolves to no root, 400 if it resolves to the wrong
    kind. Keeping the CLI a thin client, the daemon is the authoritative validator.
    The wrong-kind message names ``require_kind`` (asserted by the API tests).
    """
    from fastapi import HTTPException

    try:
        row = roots_mod.resolve_root(database, arg)
    except roots_mod.RootError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if row["kind"] != require_kind:
        raise HTTPException(
            status_code=400,
            detail=f"{row['name']!r} is a {row['kind']} root; {verb} targets a {require_kind} root",
        )
    return row


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

    # The periodic-job scheduler (§3, now realized — §8 A2b probe is its first client).
    # Constructed right after the queue + reconcile; started/stopped in the lifespan
    # hooks below, symmetric with the queue. It is just another queue *client* (its jobs
    # only enqueue), so §3's single-worker invariant is untouched.
    from ..jobs.scheduler import PeriodicScheduler

    scheduler = PeriodicScheduler(queue, database, _load_config())

    app = FastAPI(title="packrat daemon", version=__version__)
    app.state.token = token
    app.state.db = database
    app.state.queue = queue
    app.state.scheduler = scheduler
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

    @app.get("/jobs/{job_id}/problem-files", dependencies=[Depends(require_token)])
    def job_problem_files(job_id: int):
        """A scan job's undecodable/read-error files (paths + reasons, §12 result card)."""
        return {"problem_files": queries.job_problem_files(job_id)}

    @app.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_token)])
    def cancel_job(job_id: int):
        ok = queue.cancel(job_id)
        return {"cancelled": ok}

    @app.post("/jobs/{job_id}/prioritize", dependencies=[Depends(require_token)])
    def prioritize_job(job_id: int):
        """Bump a queued job to the front of the dequeue order (§11 ``jobs prioritize``)."""
        ok = queue.prioritize(job_id)
        return {"prioritized": ok}

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
                    # Poll with a timeout rather than block forever: a timed `get` lets
                    # us (a) emit a periodic heartbeat comment so a dead/half-open client
                    # is detected (the yield raises when the peer is gone → `finally`
                    # unsubscribes and frees this executor thread), and (b) never leak the
                    # executor thread even if the sentinel is dropped (a full slow-client
                    # queue) — the loop re-checks the job's terminal state each tick.
                    try:
                        ev = await loop.run_in_executor(
                            None, lambda: sub.q.get(timeout=_SSE_HEARTBEAT_S))
                    except _queue.Empty:
                        current = queries.job_detail(job_id)
                        if current and current["status"] not in ("running", "queued"):
                            break  # job ended but our sentinel was dropped — stop cleanly
                        yield ": keepalive\n\n"   # SSE comment; raises if the client is gone
                        continue
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
            row = _resolve_root_or_400(database, arg, require_kind="library", verb="scan")
            params["root_id"] = row["id"]
        return {"job_id": queue.submit("scan", params)}

    @app.post("/probe", dependencies=[Depends(require_token)])
    def submit_probe(body: dict):
        """Resolve a root arg (path/--name) + submit a probe job, or fan out for --all (§8 A2b).

        A single ``<root>`` submits one ``probe`` job (owns its root, dequeue-gated like
        ``scan``). ``--all`` expands here to one ``probe <root>`` per enabled **library**
        root — the per-root policy (§8 A2b decision #2), NOT a single root-less sweep — so
        each gets its own queue entry + gate; the queue's submit-dedup caps the backlog at
        one queued probe per root. Returns ``{job_ids: [...]}`` (the per-root job ids;
        deduped ids may repeat a still-queued probe). A trash root is rejected (400).
        """
        if bool(body.get("all")):
            rows = database.query(
                "SELECT id FROM roots WHERE enabled=1 AND kind='library' ORDER BY id"
            )
            job_ids = [queue.submit("probe", {"root_id": r["id"]}) for r in rows]
            return {"job_ids": job_ids}
        arg = body.get("root")
        if not arg:
            raise HTTPException(status_code=400, detail="probe needs a <root> or --all")
        row = _resolve_root_or_400(database, arg, require_kind="library", verb="probe")
        return {"job_ids": [queue.submit("probe", {"root_id": row["id"]})]}

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
        row = _resolve_root_or_400(database, arg, require_kind="library", verb="dedup")
        params = {
            "root_id": row["id"],
            "confirm": bool(body.get("confirm")),
            "cancel": bool(body.get("cancel")),
            "dry_run": bool(body.get("dry_run")),
            "keep_suggested": bool(body.get("keep_suggested")),
            "prefer_internal": bool(body.get("prefer_internal")),
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
        row = _resolve_root_or_400(database, arg, require_kind="library", verb="cleanup")
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

    @app.post("/merge", dependencies=[Depends(require_token)])
    def submit_merge(body: dict):
        """Resolve ``--into`` to a library root + submit a merge job (§8 C).

        ``--into`` may name a root or a **subfolder** of one (created at copy time), so
        it uses :func:`roots.resolve_dest` (containment), not exact/name match. The
        resolved ``root_id`` (owned root, cross-op guard) + canonical ``dest_path`` are
        frozen into params here so the CLI stays a thin client. A ``--dry-run`` merge is
        enqueued like any other (owns None, writes nothing).
        """
        source = body.get("source")
        into = body.get("into")
        if not source:
            raise HTTPException(status_code=400, detail="merge needs a <source> folder")
        if not into:
            raise HTTPException(status_code=400, detail="merge needs --into <dest>")
        try:
            root, dest_path = roots_mod.resolve_dest(database, into)
        except roots_mod.RootError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        params = {
            "root_id": root["id"],
            "source": source,
            "dest_path": dest_path,
            "dry_run": bool(body.get("dry_run")),
        }
        return {"job_id": queue.submit("merge", params)}

    @app.post("/trash/refresh", dependencies=[Depends(require_token)])
    def submit_trash_refresh(body: dict):
        """Submit a ``trash refresh`` job (§6.1) — absorb + empty the trash roots.

        A ``root`` arg (path/--name) scopes the refresh to that **single** trash
        root (the ``trash refresh <root>`` verb / TUI mascot modal); it must resolve
        to a ``kind='trash'`` root (a library root's files are indexed by ``scan``,
        not consumed) — else 400/404. Omitting it refreshes **every** trash root, the
        original behavior.
        """
        arg = body.get("root")
        params: dict = {}
        if arg:
            row = _resolve_root_or_400(database, arg, require_kind="trash", verb="trash refresh")
            params["root_id"] = row["id"]
        return {"job_id": queue.submit("trash-refresh", params)}

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


def _bind_single_instance(port: int):
    r"""Acquire the single-instance lock by binding the fixed loopback port (§3).

    Returns the bound+listening socket, or ``None`` if the port is already taken
    (another daemon won the auto-spawn race — the caller exits without touching any
    shared state). This is the RACE GATE: binding must happen **before** any
    destructive startup work (reconciliation, queue pump, token/state write), so a
    losing daemon can never flip the winner's running job to interrupted, roll back
    its staging, or clobber its token. We bind here rather than letting uvicorn bind
    inside ``server.run`` precisely so the lock is held before ``build_app`` runs.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # NOTE: deliberately NO SO_REUSEADDR — we WANT a second bind on the same port to
    # fail so the loser detects the winner (SO_REUSEADDR on Windows would let both
    # bind and break the single-instance guarantee).
    try:
        sock.bind((HOST, port))
        sock.listen()
    except OSError as exc:
        log.info("could not bind %s:%d (%s) — another daemon likely won", HOST, port, exc)
        sock.close()
        return None
    return sock


def run_daemon(*, db_file=None, config_path=None, port: int = DEFAULT_PORT) -> int:
    """Run the daemon in the foreground (the detached process's entrypoint).

    Order matters for the auto-spawn race (§3): **bind the loopback port FIRST**
    (the single-instance lock), and only then do any startup work — reconciliation,
    the queue pump, and the token/state write. A daemon that loses the bind exits
    immediately (code 3) having touched no shared state, so it can never corrupt the
    winner's in-flight job or clobber its token. Returns a process exit code.
    """
    import uvicorn

    # 1. RACE GATE: bind the port before anything destructive (§3). Loser → exit.
    lock_sock = _bind_single_instance(port)
    if lock_sock is None:
        return 3

    try:
        # 2. Now that we hold the lock, generate the token and build the app
        #    (reconciliation + queue pump run inside build_app — safe post-lock).
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

        # We already hold the lock, so token/state can be written up front — but keep
        # it in the lifespan hook so it's torn down symmetrically by _on_stop. Since
        # the port is ours, this hook can no longer race a second daemon.
        @app.on_event("startup")
        def _on_start():
            token_mod.write_token(token, paths.token_path())
            current_state().write()
            # Arm the periodic scheduler (§3) — its BackgroundScheduler thread starts
            # here, symmetric with the queue; a fired task only submits jobs.
            app.state.scheduler.start()
            log.info("packrat daemon up on %s:%d (pid=%d)", HOST, port, current_state().pid)

        @app.on_event("shutdown")
        def _on_stop():
            clear_state()
            # Stop the periodic scheduler first (no new work into the teardown),
            # symmetric with queue.shutdown() below.
            app.state.scheduler.shutdown()
            # Signal a running job to checkpoint and join it before closing the DB.
            # Its row is reconciled to 'interrupted' on next start (§3 clean-stop).
            app.state.queue.shutdown()
            app.state.db.close()
            log.info("packrat daemon stopped")

        # Hand uvicorn our pre-bound listening socket so it does NOT bind again
        # (Server.run(sockets=[...]) → startup(sockets=...) skips create_server).
        server.run(sockets=[lock_sock])
    finally:
        try:
            lock_sock.close()
        except OSError:
            pass
    return 0
