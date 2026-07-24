"""``packrat`` CLI entrypoint (Typer) — thin client onto the daemon (§3, §11).

Current surface (M1–M5):
- ``packrat roots register|list`` — declare/list roots (§8 A1, §11).
- ``packrat scan`` — walk a root and fingerprint it (§8 A2).
- ``packrat probe`` — cheap discovery: count new/changed files, no fingerprint (§8 A2b).
- ``packrat dedup`` — 3-stage dedup of one folder (§8 B).
- ``packrat merge`` — copy new files into a folder by exact hash (§8 C).
- ``packrat cleanup`` / ``trash refresh`` / ``untrash`` — the trash model (§6).
- ``packrat status`` — global rollup / per-root detail (read-only, never blocked, §11).
- ``packrat jobs list|cancel|prioritize`` — inspect/steer the work queue (§3, §11);
  ``jobs cancel`` with no id cancels the running job.
- ``packrat daemon start|stop|restart|status`` — lifecycle/troubleshooting (§11).
- ``packrat smoke-test`` — the §9.1 decode smoke test.
- ``packrat`` (no args) — the TUI placeholder (full TUI is M6, §12); ``--nsfw`` masks
  adult-content keywords (EN/CN) in on-screen root names/paths (display-only privacy).

Every job-submitting command auto-spawns the daemon on first use (§3), submits,
and streams; Ctrl-C detaches the view without stopping the job.
"""

from __future__ import annotations

import json
import sys
import time
from typing import List, Optional

import typer

from .. import __version__, build
from ..daemon.client import DaemonClient, DaemonError, DaemonNotRunning
from ..daemon.spawn import ensure_daemon
from ..daemon.state import DEFAULT_PORT, pid_alive, pid_on_port, read_state, terminate_pid
from .stream import stream_job

app = typer.Typer(
    name="packrat",
    help="Local media-collection manager: fingerprint dedup + Explorer merge/trash workflow.",
    no_args_is_help=False,
    add_completion=False,
)

daemon_app = typer.Typer(help="Manage the background daemon.")
app.add_typer(daemon_app, name="daemon")

roots_app = typer.Typer(help="Manage roots: register + list.", invoke_without_command=True)
app.add_typer(roots_app, name="roots")

trash_app = typer.Typer(help="Trash memory: refresh the trash folders.")
app.add_typer(trash_app, name="trash")

jobs_app = typer.Typer(help="Jobs: list, cancel, prioritize.", invoke_without_command=True)
app.add_typer(jobs_app, name="jobs")

# Dev-only commands are registered ONLY in a dev build (source checkout or
# $PACKRAT_DEV) — a release/wheel install never sees the `dev` group at all.
dev_app = typer.Typer(help="Dev-only helpers (hidden in release builds).")
if build.is_dev_build():
    app.add_typer(dev_app, name="dev")


# ---------------------------------------------------------------------------
# daemon lifecycle
# ---------------------------------------------------------------------------
@daemon_app.command("start")
def daemon_start():
    """Explicitly spawn the detached daemon (no-op if already up)."""
    client = DaemonClient()
    if client.is_up():
        typer.echo("daemon already running.")
        raise typer.Exit(0)
    try:
        client = ensure_daemon()
    except TimeoutError as exc:
        typer.echo(f"failed to start daemon: {exc}", err=True)
        raise typer.Exit(1)
    info = client.daemon_status()
    typer.echo(f"daemon started · pid {info['pid']} · port {info['port']} · v{info['version']}")


def _force_kill_orphan(*, reason: str) -> bool:
    """Force-terminate an orphaned daemon by its fixed port, self-healing a stale token (§3).

    The daemon binds a fixed loopback port as its single-instance lock, so whatever
    listens there IS the packrat daemon — safe to kill by port when the token no longer
    matches (e.g. a daemon spawned under a since-deleted ``PACKRAT_HOME`` during testing:
    ``/health`` answers, but the authed ``stop``/``restart`` gets a 401). Returns True if
    an orphan was found and terminated. Prints what it did.
    """
    pid = pid_on_port(DEFAULT_PORT)
    if pid is None:
        return False
    typer.echo(f"{reason} — force-stopping the daemon on port {DEFAULT_PORT} (pid {pid}).")
    if terminate_pid(pid):
        from ..daemon.state import clear_state

        clear_state()  # its state file (if any) is now stale
        typer.echo("daemon force-stopped.")
        return True
    typer.echo(f"could not terminate pid {pid}; stop it manually.", err=True)
    return False


@daemon_app.command("stop")
def daemon_stop():
    """Graceful shutdown: an in-flight job is left `interrupted` (resumable), not cancelled.

    Self-heals a stale token: if the daemon is up but rejects our token (an orphan from
    a since-deleted PACKRAT_HOME), it is force-stopped by its fixed port instead of
    failing with a 401.
    """
    client = DaemonClient()
    if not client.is_up():
        typer.echo("daemon is not running.")
        raise typer.Exit(0)
    try:
        resp = client.shutdown()
    except DaemonError as exc:
        # Up but our token was rejected (401) → orphaned daemon; force-stop by port.
        if _is_auth_error(exc) and _force_kill_orphan(reason="daemon rejected our token"):
            raise typer.Exit(0)
        typer.echo(f"error stopping daemon: {exc}", err=True)
        raise typer.Exit(1)
    except DaemonNotRunning as exc:
        typer.echo(f"error stopping daemon: {exc}", err=True)
        raise typer.Exit(1)
    if resp.get("running_job"):
        typer.echo(
            "stopping daemon — the in-flight job is left interrupted (resumable); "
            "re-run its command to resume."
        )
    else:
        typer.echo("daemon stopping.")


@daemon_app.command("restart")
def daemon_restart():
    """Stop the running daemon (if any) and start a fresh one.

    Useful after upgrading packrat so the daemon picks up new code (config is
    reloaded per job, but the *code* only changes on restart). A graceful stop
    leaves any in-flight job `interrupted` (resumable) — re-run its command to
    resume it. Because the daemon holds a fixed-port single-instance lock, we wait
    for the old one to release the port before spawning the new one, so the
    replacement can bind.
    """
    client = DaemonClient()
    if client.is_up():
        forced = False
        try:
            resp = client.shutdown()
        except DaemonError as exc:
            # Up but our token was rejected (401) → orphaned daemon; force-stop by port
            # so the restart self-heals a stale token rather than dying on the 401.
            if _is_auth_error(exc) and _force_kill_orphan(reason="daemon rejected our token"):
                forced = True
            else:
                typer.echo(f"error stopping daemon: {exc}", err=True)
                raise typer.Exit(1)
        except DaemonNotRunning as exc:
            typer.echo(f"error stopping daemon: {exc}", err=True)
            raise typer.Exit(1)
        else:
            if resp.get("running_job"):
                typer.echo("stopping daemon — the in-flight job is left interrupted (resumable).")
        # Wait for the old daemon to stop serving and free the port; a new spawn
        # can't bind until it does (the port bind is the single-instance lock, §3).
        # (A force-kill already confirmed the process is gone, so the wait is quick.)
        if not forced:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if not DaemonClient().is_up():
                    break
                time.sleep(0.2)
            else:
                typer.echo("old daemon did not stop within 15s; not restarting.", err=True)
                raise typer.Exit(1)
    else:
        typer.echo("daemon was not running.")
    try:
        client = ensure_daemon()
    except TimeoutError as exc:
        typer.echo(f"failed to start daemon: {exc}", err=True)
        raise typer.Exit(1)
    info = client.daemon_status()
    typer.echo(f"daemon restarted · pid {info['pid']} · port {info['port']} · v{info['version']}")


@daemon_app.command("status")
def daemon_status():
    """Is the daemon running? pid, uptime, bound port, in-flight job (read-only)."""
    client = DaemonClient()
    if not client.is_up():
        state = read_state()
        if state and pid_alive(state.pid):
            typer.echo(f"daemon state file present (pid {state.pid}) but API not responding.")
        else:
            typer.echo("daemon is not running.")
        raise typer.Exit(0)
    try:
        info = client.daemon_status()
    except DaemonError as exc:
        # Up (/health answered) but our token is rejected → an orphaned daemon from a
        # since-deleted PACKRAT_HOME. Report it clearly instead of a raw 401 traceback;
        # `packrat daemon restart` (or stop) self-heals it by force-stopping the port.
        if _is_auth_error(exc):
            pid = pid_on_port(DEFAULT_PORT)
            pid_note = f" (pid {pid})" if pid else ""
            typer.echo(f"● up on port {DEFAULT_PORT}{pid_note}, but it rejects our token — "
                       "an orphaned daemon (stale token).")
            typer.echo("  run `packrat daemon restart` to force-stop it and start a fresh one.")
            raise typer.Exit(0)
        typer.echo(f"error querying daemon: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"● up · pid {info['pid']} · port {info['port']} · v{info['version']} · since {info['started_at']}")
    rj = info.get("running_job")
    if rj:
        typer.echo(f"  running: {rj['type']} ({rj['done']}/{rj['total']})")
    else:
        typer.echo("  no running job.")


# ---------------------------------------------------------------------------
# read-only snapshots
# ---------------------------------------------------------------------------
@app.command("status")
def status(
    root: Optional[str] = typer.Argument(None, help="A registered root (path/--name) for its detail."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
):
    """Print collection state (read-only, never blocked by a running job)."""
    client = _client_or_spawn()
    if root:
        try:
            resp = client.status(root)
        except DaemonError as exc:
            typer.echo(_detail(exc), err=True)
            raise typer.Exit(1)
        d = resp["root_detail"]
        if json_out:
            typer.echo(json.dumps(d, indent=2))
            return
        typer.echo(f"[{d['id']}] {d['name']}  {d['path']}  ({d['kind']})")
        typer.echo(f"  assets: {d['photos'] + d['videos']} (photos {d['photos']} · videos {d['videos']})")
        typer.echo(f"  files: {d['instances']}")
        typer.echo(f"  last scan: {_short_ts(d.get('last_scan_at'))}")
        # Last SUCCESSFUL dedup (all stages, or already-clean) — §11 "deduped <age>".
        dd = d.get("last_dedup_at")
        typer.echo(f"  last dedup: {_short_ts(dd)}" if dd else "  last dedup: never")
        cc = d.get("last_cleanup_at")
        if cc:
            typer.echo(f"  last cleanup: {_short_ts(cc)}")
        if d.get("pending_review"):
            pr = d["pending_review"]
            typer.echo(f"  ⚠ {pr['run_type']} pending since {pr['created_at']} — "
                       f"{_review_count_summary(pr)}")
            typer.echo(f"    review: {d['path']}\\_packrat_review\\ · "
                       f"`packrat {_review_verb(pr)} {d['name']} --confirm` (or --cancel).")
        rj = d.get("running_job")
        if rj:
            typer.echo(f"  ▶ running: {rj.get('label') or rj['type']} "
                       f"({rj.get('done', 0)}/{rj.get('total')})")
        qj = d.get("queued_jobs", [])
        if qj:
            typer.echo(f"  queued: {len(qj)} job(s) for this root —")
            for q in qj:
                holder = q.get("blocked")
                why = f"blocked: {holder['what']}" if holder else "waiting for worker"
                typer.echo(f"    {q.get('label') or q['type']} · {why}")
        _print_last_scan(d)
        return
    snap = client.status()
    if json_out:
        typer.echo(json.dumps(snap, indent=2))
        return
    typer.echo(f"assets: {snap['assets']}  (photos {snap['photos']} · videos {snap['videos']})")
    typer.echo(f"trashed: {snap['trashed']}")
    roots = snap.get("roots", [])
    if roots:
        typer.echo("roots:")
        for r in roots:
            scan_note = _scan_recency(r)
            typer.echo(
                f"  [{r['id']}] {r['name']}  {r['path']}  "
                f"({r['kind']}, {r['asset_count']} assets{scan_note})"
            )
    else:
        typer.echo("roots: none registered yet — `packrat roots register <path>`.")
    if snap.get("running"):
        rj = snap["running"]
        typer.echo(f"running: {rj.get('label') or rj['type']} ({rj['done']}/{rj['total']})")
    queued = snap.get("queued", [])
    if queued:
        typer.echo(f"queued: {len(queued)} job(s) in the backlog —")
        for q in queued:
            holder = q.get("blocked")
            why = f"blocked: {holder['what']}" if holder else "waiting for worker"
            typer.echo(f"  {q.get('label') or q['type']} · {why}")
    for pr in snap.get("pending_reviews", []):
        typer.echo(f"⚠ {pr['run_type']} pending on {pr['root_name']} — {_review_count_summary(pr)} "
                   f"(`packrat {_review_verb(pr)} {pr['root_name']} --confirm`/--cancel).")
    for it in snap.get("interrupted", []):
        typer.echo(f"⚠ interrupted: {it['type']} — re-run its command to resume.")


@roots_app.callback(invoke_without_command=True)
def _roots_root(ctx: typer.Context):
    """Bare ``packrat roots`` is an alias for ``packrat roots list``."""
    if ctx.invoked_subcommand is None:
        _roots_list(json_out=False)


@roots_app.command("list")
def roots_list(json_out: bool = typer.Option(False, "--json")):
    """List registered roots (read-only)."""
    _roots_list(json_out=json_out)


def _roots_list(*, json_out: bool) -> None:
    client = _client_or_spawn()
    rs = client.roots()
    if json_out:
        typer.echo(json.dumps(rs, indent=2))
        return
    if not rs:
        typer.echo("no roots registered yet — `packrat roots register <path>`.")
        return
    for r in rs:
        scan_note = _scan_recency(r)
        typer.echo(
            f"  [{r['id']}] {r['name']}  {r['path']}  "
            f"({r['kind']}, {r['asset_count']} assets, {r.get('instance_count', 0)} files{scan_note})"
        )


@roots_app.command("register")
def roots_register(
    path: str = typer.Argument(..., help="Folder to register as a root."),
    name: Optional[str] = typer.Option(None, "--name", help="Root handle; must be globally unique."),
    kind: str = typer.Option("library", "--kind", help="library|trash."),
    ignore: List[str] = typer.Option([], "--ignore", help="Extra ignore glob (repeatable)."),
    scan: bool = typer.Option(False, "--scan", help="After registering, immediately scan the root."),
    full: bool = typer.Option(False, "--full", help="With --scan, do a full (re-fingerprint) scan."),
    embed: bool = typer.Option(False, "--embed", help="With --scan, also run the CLIP pass (implies --scan; M7)."),
    detach: bool = typer.Option(False, "--detach", help="With --scan, submit and return without streaming."),
    json_out: bool = typer.Option(False, "--json"),
):
    """Declare a folder as a root (metadata-only, instantaneous)."""
    client = _client_or_spawn()
    do_scan = scan or embed  # --embed implies --scan (§8 A1)
    try:
        resp = client.register_root(
            path, name=name, kind=kind, ignore_globs=list(ignore),
            scan=do_scan, full=full, embed=embed,
        )
    except DaemonError as exc:
        typer.echo(f"cannot register: {_detail(exc)}", err=True)
        raise typer.Exit(1)
    row = resp["root"]
    if json_out:
        typer.echo(json.dumps(resp, indent=2))
        raise typer.Exit(0)
    typer.echo(f"registered root [{row['id']}] {row['name']}  {row['path']}  ({row['kind']}) — not yet scanned.")
    job_id = resp.get("job_id")
    if job_id and not detach:
        final = stream_job(client, job_id, label="scan")
        typer.echo(f"scan {final}")
        _exit_if_failed(final)
    elif job_id:
        typer.echo("  scan running in the daemon — `packrat jobs` to check.")


@jobs_app.callback(invoke_without_command=True)
def _jobs_root(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Bare ``packrat jobs`` lists recent runs (alias for ``packrat jobs list``)."""
    if ctx.invoked_subcommand is None:
        _jobs_list(limit=limit, json_out=json_out)


@jobs_app.command("list")
def jobs_list(limit: int = typer.Option(20, "--limit"), json_out: bool = typer.Option(False, "--json")):
    """List recent job runs (read-only)."""
    _jobs_list(limit=limit, json_out=json_out)


def _jobs_list(*, limit: int, json_out: bool) -> None:
    client = _client_or_spawn()
    js = client.list_jobs(limit)
    if json_out:
        typer.echo(json.dumps(js, indent=2))
        return
    if not js:
        typer.echo("no jobs yet.")
        return
    for j in js:
        # Queued jobs have no start yet — show enqueue time so the row isn't blank.
        stamp = (j.get("started_at") or j.get("enqueued_at") or "")[:19].replace("T", " ")
        label = j.get("label") or j["type"]
        line = f"  [{j['id']}] {stamp}  {label:28s} {j['status']:11s} {j.get('done', 0)}/{j.get('total')}"
        result = j.get("result_json")
        if result:
            try:
                summary = json.loads(result).get("summary")
            except (ValueError, TypeError):
                summary = None
            if summary:
                line += f"  · {summary}"
        if j.get("error"):
            line += f"  err: {j['error']}"
        typer.echo(line)


@jobs_app.command("cancel")
def jobs_cancel(
    job_id: Optional[int] = typer.Argument(
        None, help="Job id to cancel. Omit to cancel the currently-running job."),
):
    """Cancel a job. With no id, cancels the currently-running job.

    Only one mutating job runs at a time, so **no id is needed** to stop the running
    one — ``packrat jobs cancel`` targets it. Pass an explicit id to cancel a specific
    job (e.g. a *queued* one).

    A **running** job gets a cooperative stop at its next checkpoint (lands
    ``cancelled``; for merge/review this discards the resumable plan). A **queued** job
    is dropped from the backlog (never ran). A terminal job is a no-op.
    """
    client = _client_or_spawn()
    if job_id is None:
        rj = client.daemon_status().get("running_job")
        if not rj:
            typer.echo("no running job to cancel.")
            raise typer.Exit(0)
        job_id = rj["id"]
    ok = client.cancel_job(job_id)
    typer.echo(f"job {job_id}: cancel requested." if ok
               else f"job {job_id} is not running or queued (nothing to cancel).")


@jobs_app.command("prioritize")
def jobs_prioritize(
    job_id: int = typer.Argument(..., help="Queued job id to move to the front of the queue."),
):
    """Move a queued job to the front of the queue.

    Bumps the job ahead of every other queued job, so it runs **next** when the worker
    frees. If its owned root is held (a pending review / open merge), it stays at the
    front but **blocked** — a lower-priority *runnable* job can still pass it (dequeue is
    runnable-first, so this never deadlocks). Only a queued job can be prioritized.
    """
    client = _client_or_spawn()
    ok = client.prioritize_job(job_id)
    typer.echo(f"job {job_id} moved to the front of the queue." if ok
               else f"job {job_id} is not queued (only a queued job can be prioritized).")


# ---------------------------------------------------------------------------
# scan — walk a registered root and fingerprint it (§8 A2)
# ---------------------------------------------------------------------------
@app.command("scan")
def scan(
    path: Optional[str] = typer.Argument(None, help="A registered root (path or --name). Omit with --all."),
    all_roots: bool = typer.Option(False, "--all", help="Scan every enabled root."),
    full: bool = typer.Option(False, "--full", help="Ignore the fast-path; re-fingerprint everything."),
    embed: bool = typer.Option(False, "--embed", help="Also compute CLIP embeddings (deferred to M7)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Enumerate + report what would be indexed; write nothing."),
    profile: bool = typer.Option(False, "--profile", help="Report where time went: NAS transfer vs CPU vs decode."),
    detach: bool = typer.Option(False, "--detach", help="Submit and return without streaming."),
    json_out: bool = typer.Option(False, "--json"),
):
    """Walk a registered root and fingerprint new/changed files (resumable)."""
    if not all_roots and not path:
        typer.echo("scan needs a <root> path/name, or --all.", err=True)
        raise typer.Exit(2)
    if all_roots and path:
        typer.echo("give a <root> or --all, not both.", err=True)
        raise typer.Exit(2)
    client = _client_or_spawn()
    _run_streamed_job(
        client,
        lambda: client.submit_scan(
            path, all_roots=all_roots, full=full, embed=embed, dry_run=dry_run, profile=profile
        ),
        verb="scan", label="scan", detach=detach, json_out=json_out,
    )


# ---------------------------------------------------------------------------
# probe — cheap discovery: are there new files here worth a scan? (§8 A2b)
# ---------------------------------------------------------------------------
@app.command("probe")
def probe(
    path: Optional[str] = typer.Argument(None, help="A registered library root (path or --name). Omit with --all."),
    all_roots: bool = typer.Option(False, "--all", help="Probe every enabled library root."),
    detach: bool = typer.Option(False, "--detach", help="Submit and return without streaming."),
    json_out: bool = typer.Option(False, "--json"),
):
    """Walk a root and count new/changed files WITHOUT fingerprinting (fast; §8 A2b).

    Probe is scan's cheap discovery half: it enumerates the root and counts files a scan
    would (re)fingerprint — no hashing, decode, or PDQ, no catalog writes beyond a
    per-root "new files waiting" signal the TUI shows as a status dot. Runs every 24 h
    per root automatically; this verb triggers one now. `--all` probes every enabled
    library root (one job each). Press `[s]`/`packrat scan` to actually fingerprint.
    """
    if not all_roots and not path:
        typer.echo("probe needs a <root> path/name, or --all.", err=True)
        raise typer.Exit(2)
    if all_roots and path:
        typer.echo("give a <root> or --all, not both.", err=True)
        raise typer.Exit(2)
    client = _client_or_spawn()
    try:
        job_ids = client.submit_probe(path, all_roots=all_roots)
    except DaemonError as exc:
        typer.echo(f"cannot probe: {_detail(exc)}", err=True)
        raise typer.Exit(1)
    if json_out:
        typer.echo(json.dumps({"job_ids": job_ids}, indent=2))
        return
    if not job_ids:
        typer.echo("no enabled library roots to probe.")
        return
    # --all (or --detach): fan-out submits N jobs — report + return, don't stream N at once.
    if all_roots or detach or len(job_ids) != 1:
        typer.echo(f"submitted {len(job_ids)} probe job(s) — running in the daemon; "
                   "`packrat status` to see results.")
        return
    final = stream_job(client, job_ids[0], label="probe")
    typer.echo(f"probe {final}")
    _exit_if_failed(final)


# ---------------------------------------------------------------------------
# dedup — analyze/stage/confirm one registered folder (§8 B)
# ---------------------------------------------------------------------------
@app.command("dedup")
def dedup(
    folder: str = typer.Argument(..., help="A registered library root (path or --name)."),
    confirm: bool = typer.Option(False, "--confirm", help="Apply the current stage, then advance to the next."),
    cancel: bool = typer.Option(False, "--cancel", help="Discard the whole run's staging; delete nothing."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute all 3 stages + print the plan; stage nothing."),
    keep_suggested: bool = typer.Option(
        False, "--keep-suggested",
        help="With --confirm on stage 2: keep ONLY each group's suggested lead, delete the rest — "
             "ignoring your shortcut edits. (Groups with no suggested lead are spared.)",
    ),
    prefer_internal: bool = typer.Option(
        False, "--prefer-internal",
        help="Prefer keeping THIS root's copy over a duplicate in another root: stage 1 deletes the "
             "external copy (not the internal one), and stage-2 keep-lead ties go to the internal copy. "
             "Locked when the run opens; carries across --confirm. (Default: the external copy is kept.)",
    ),
    detach: bool = typer.Option(False, "--detach", help="Submit and return without streaming."),
    json_out: bool = typer.Option(False, "--json"),
):
    """Dedup one folder as a 3-stage sequence: analyze → --confirm (auto-advances).

    Stages, one at a time under `<root>\\_packrat_review\\`: 1 `_exact_dup_to_delete\\`
    (default DELETE — remove a shortcut to SPARE), 2 `_suspect_recompression\\` and
    3 `_with_minor_edits\\` (default KEEP — remove a shortcut to DELETE). `--confirm`
    applies the current stage and advances to the next non-empty one; `--cancel`
    discards the whole run. On stage 2, `--confirm --keep-suggested` trusts packrat's
    `_suggested` lead per group and deletes every other member, ignoring your edits.
    """
    if confirm and cancel:
        typer.echo("give --confirm or --cancel, not both.", err=True)
        raise typer.Exit(2)
    if keep_suggested and not confirm:
        typer.echo("--keep-suggested only applies with --confirm.", err=True)
        raise typer.Exit(2)
    client = _client_or_spawn()
    label = ("dedup --confirm --keep-suggested" if confirm and keep_suggested
             else "dedup --confirm" if confirm else "dedup --cancel" if cancel else "dedup")
    if prefer_internal:
        label += " --prefer-internal"
    _run_streamed_job(
        client,
        lambda: client.submit_dedup(folder, confirm=confirm, cancel=cancel, dry_run=dry_run,
                                    keep_suggested=keep_suggested, prefer_internal=prefer_internal),
        verb="dedup", label=label, detach=detach, json_out=json_out,
    )


# ---------------------------------------------------------------------------
# merge — copy new files into a destination folder (§8 C)
# ---------------------------------------------------------------------------
@app.command("merge")
def merge(
    source: str = typer.Argument(..., help="Transient temp folder to merge from (never modified)."),
    into: str = typer.Option(..., "--into", help="Destination folder; must resolve inside a library root."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print classification counts / would-copy list; copy nothing."),
    detach: bool = typer.Option(False, "--detach", help="Submit and return without streaming."),
    json_out: bool = typer.Option(False, "--json"),
):
    r"""Copy into <dest> only the files new to the whole collection, by exact hash.

    `merge <source> --into <dest>`: refresh the trash collection → classify each source
    file by exact hash (dup-in-source / trashed / exact-known / new) → copy the `new`
    files (hash-verified, structure-mirrored under <dest>) and register them. Source is
    read-only; dest is copy-only. No near-dup detection or review — that's `dedup`, run
    afterward. One shot; resumable from its plan on crash.

    `--dry-run` previews the classification counts without copying — but still refreshes
    and empties the trash collection for real.
    """
    client = _client_or_spawn()
    label = "merge --dry-run" if dry_run else "merge"
    _run_streamed_job(
        client,
        lambda: client.submit_merge(source, into, dry_run=dry_run),
        verb="merge", label=label, detach=detach, json_out=json_out,
    )


# ---------------------------------------------------------------------------
# cleanup — remove trashed content from a library folder (§6.2)
# ---------------------------------------------------------------------------
@app.command("cleanup")
def cleanup(
    folder: str = typer.Argument(..., help="A registered library root to clean (path or --name)."),
    trash_exact: bool = typer.Option(False, "--trash-exact", help="Delete files that are byte-identical to trashed content."),
    trash_perceptual: bool = typer.Option(False, "--trash-perceptual", help="Stage recompressed-trash matches for review; also deletes exact matches."),
    undecodable: bool = typer.Option(False, "--undecodable", help="Delete the folder's undecodable files + mark them trashed."),
    confirm: bool = typer.Option(False, "--confirm", help="Apply a pending --trash-perceptual run (exact + still-staged perceptual)."),
    cancel: bool = typer.Option(False, "--cancel", help="Discard the pending --trash-perceptual run; delete nothing."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be deleted/staged; delete nothing (trash modes still refresh trash)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the typed confirmation (one-shot modes)."),
    detach: bool = typer.Option(False, "--detach", help="Submit and return without streaming."),
    json_out: bool = typer.Option(False, "--json"),
):
    r"""Delete junk from <folder>. Requires one mode: --trash-exact / --trash-perceptual / --undecodable.

    - `--trash-exact`: files byte-identical to trashed content. Refresh trash → count →
      typed confirm → delete to Recycle Bin.
    - `--trash-perceptual`: also catch recompressed trash copies, staged under
      `<root>\_packrat_review\_perceptually_identified_trash\` for review (delete-default:
      a staged shortcut WILL be deleted — remove it to spare), then `--confirm`/`--cancel`.
      Deletes exact matches too, at confirm.
    - `--undecodable`: files whose pixels won't decode; deletes them and marks each
      asset trashed. Count → typed confirm → delete. Does not touch the trashed set.

    Trash modes refresh the trash collection for real, even under `--dry-run`.
    """
    # --confirm/--cancel act on the pending perceptual run (no mode needed, like dedup).
    if confirm and cancel:
        typer.echo("give --confirm or --cancel, not both.", err=True)
        raise typer.Exit(2)
    modes = [("exact", trash_exact), ("perceptual", trash_perceptual), ("undecodable", undecodable)]
    chosen = [name for name, on in modes if on]
    if not (confirm or cancel):
        if len(chosen) != 1:
            typer.echo(
                "cleanup requires exactly one mode: --trash-exact, --trash-perceptual, "
                "or --undecodable.", err=True)
            raise typer.Exit(2)
    elif chosen and chosen != ["perceptual"]:
        typer.echo("--confirm/--cancel apply to a pending --trash-perceptual run; "
                   "don't combine them with --trash-exact/--undecodable.", err=True)
        raise typer.Exit(2)
    mode = chosen[0] if chosen else "perceptual"  # confirm/cancel target the perceptual run
    client = _client_or_spawn()

    # Stateful perceptual verbs (analyze / --confirm / --cancel) and any --dry-run:
    # submit one job and stream it (no CLI count-confirm — perceptual stages for review).
    if mode == "perceptual" or confirm or cancel or dry_run:
        label = ("cleanup --cancel" if cancel else "cleanup --confirm" if confirm
                 else "cleanup --dry-run" if dry_run else f"cleanup --{_mode_flag(mode)}")
        _run_streamed_job(
            client,
            lambda: client.submit_cleanup(
                folder, mode=mode, confirm=confirm, cancel=cancel, dry_run=dry_run
            ),
            verb="cleanup", label=label, detach=detach, json_out=json_out,
        )
        return

    # One-shot modes (exact / undecodable): preview (count) → typed confirm → apply.
    flag = _mode_flag(mode)
    try:
        prev_job = client.submit_cleanup(folder, mode=mode)  # preview: report, act on nothing
    except DaemonError as exc:
        typer.echo(f"cannot cleanup: {_detail(exc)}", err=True)
        raise typer.Exit(1)
    prev_final = stream_job(client, prev_job, label=f"cleanup --{flag} (preview)")
    if prev_final != "done":
        typer.echo(f"cleanup preview {prev_final} — not deleting.", err=True)
        raise typer.Exit(1)

    prev = client.cleanup_preview(folder, mode=mode)
    n, n_net = prev["count"], prev.get("network", 0)
    what = "undecodable file(s)" if mode == "undecodable" else "file(s) match trashed content"
    if n == 0:
        typer.echo(f"cleanup {prev['name']}: no {what} — nothing to delete.")
        return
    net = (f"\n  ⚠ {n_net} of them are on a network share and will be deleted PERMANENTLY "
           f"(no Recycle Bin)." if n_net else "")
    if not yes:
        note = " (their assets will be marked trashed)" if mode == "undecodable" else ""
        typer.echo(f"{n} {what} in {prev['name']} will be moved to the Recycle Bin{note}.{net}")
        ans = typer.prompt(f"Type the count ({n}) to confirm deletion")
        if ans.strip() != str(n):
            typer.echo("count mismatch — aborted, nothing deleted.")
            raise typer.Exit(1)
    apply_job = client.submit_cleanup(folder, mode=mode, apply=True)
    final = stream_job(client, apply_job, label=f"cleanup --{flag}")
    typer.echo(f"cleanup {final}")
    if json_out:
        typer.echo(json.dumps(client.get_job(apply_job), indent=2))
    _exit_if_failed(final)


def _mode_flag(mode: str) -> str:
    """Map an internal cleanup mode to its CLI flag name (for labels/messages)."""
    return {"exact": "trash-exact", "perceptual": "trash-perceptual",
            "undecodable": "undecodable"}[mode]


# ---------------------------------------------------------------------------
# trash — refresh the registered trash folders (§6.1)
# ---------------------------------------------------------------------------
@trash_app.command("refresh")
def trash_refresh(
    root: Optional[str] = typer.Argument(
        None, help="A registered trash root (path or --name) to refresh. Omit to refresh ALL trash roots."),
    detach: bool = typer.Option(False, "--detach", help="Submit and return without streaming."),
    json_out: bool = typer.Option(False, "--json"),
):
    r"""Absorb whatever is in the registered trash folders into trash memory, then empty them.

    Fingerprints each trash-folder file, records/flips its asset to `trashed` (kept
    forever), and moves the file to the Recycle Bin (permanent on NAS/SMB). No
    `--dry-run` — refresh is never a no-op; browse the folders in Explorer first
    to preview.

    Pass a `<root>` (path or --name) to refresh just that one trash root; with no
    argument, every registered trash root is refreshed as one logical set.
    """
    client = _client_or_spawn()
    label = f"trash refresh {root}" if root else "trash refresh"
    _run_streamed_job(
        client,
        lambda: client.submit_trash_refresh(root),
        verb="refresh trash", label=label, detach=detach, json_out=json_out,
    )


# ---------------------------------------------------------------------------
# untrash — forget content from trash memory by presenting the file (§6.3)
# ---------------------------------------------------------------------------
@app.command("untrash")
def untrash(
    path: str = typer.Argument(..., help="A file (or folder, recursive) to hash — need NOT be a registered root."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be forgotten/reactivated; change nothing."),
    detach: bool = typer.Option(False, "--detach", help="Submit and return without streaming."),
    json_out: bool = typer.Option(False, "--json"),
):
    """Forget content from the trashed-hash set so it's no longer excluded from merges.

    You present the file (packrat stores no pixels to preview); untrash hashes it and
    matches by exact content hash. It does NOT restore bytes (that's the Recycle Bin)
    and writes nothing to disk — only DB rows.
    """
    client = _client_or_spawn()
    _run_streamed_job(
        client,
        lambda: client.submit_untrash(path, dry_run=dry_run),
        verb="untrash", label="untrash", detach=detach, json_out=json_out,
    )


# ---------------------------------------------------------------------------
# dev-only helpers (the `dev` group is registered only in a dev build)
# ---------------------------------------------------------------------------
@dev_app.command("clear-db")
def dev_clear_db(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the typed confirmation."),
):
    """Empty the ENTIRE catalog — every root, asset, instance, and job (dev only).

    Wipes all catalog rows (schema preserved) via the daemon under its write lock.
    Destructive and irreversible; intended for resetting a dev database between
    test runs. Refuses while a job is running.
    """
    client = _client_or_spawn()
    if not yes:
        typer.echo("This ERASES the whole packrat catalog (roots, assets, instances, jobs).")
        typer.echo("The files on disk are untouched; only the database is cleared.")
        confirm = typer.prompt("Type 'clear' to confirm")
        if confirm.strip().lower() != "clear":
            typer.echo("aborted — nothing cleared.")
            raise typer.Exit(1)
    try:
        resp = client.clear_db()
    except DaemonError as exc:
        typer.echo(f"clear-db failed: {_detail(exc)}", err=True)
        raise typer.Exit(1)
    typer.echo(f"catalog cleared — {resp['total_rows']} row(s) deleted across {len(resp['cleared'])} table(s).")


# ---------------------------------------------------------------------------
# smoke test (§9.1)
# ---------------------------------------------------------------------------
@app.command("smoke-test")
def smoke_test(
    samples: str = typer.Argument(None, help="Folder of one sample per extension (see --help)."),
    generate: bool = typer.Option(
        False, "--generate", "-g",
        help="Synthesize samples first (into <samples> or a temp dir), then run.",
    ),
    json_out: bool = typer.Option(False, "--json"),
):
    """Run the decode→hash→perceptual→embed smoke test over sample files.

    With no argument, reports which deps are available. Pass a folder of samples
    to run the full path, or --generate to synthesize samples first (RAW formats
    can't be synthesized — supply real camera files for those).
    """
    from ..smoke import run_smoke_test

    code = run_smoke_test(samples, json_out=json_out, generate=generate)
    raise typer.Exit(code)


# ---------------------------------------------------------------------------
# no-args → TUI (M6, §12) — the default face of the tool.
# ---------------------------------------------------------------------------
@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version"),
    offline: bool = typer.Option(
        False, "--offline",
        help="Run the TUI on sample data (no daemon) — a demo/preview mode."),
    nsfw: bool = typer.Option(
        False, "--nsfw",
        help="Mask adult-content keywords (EN/CN) in root names/paths on screen — "
             "a display-only privacy redaction for screen-sharing."),
):
    if version:
        typer.echo(f"packrat {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is not None:
        return
    # No subcommand → launch the TUI (§12). It auto-spawns the daemon (like every
    # other verb) and renders a `daemon ○ down` state rather than crashing if it
    # can't reach one; `--offline` renders bundled sample data with no daemon.
    from ..tui.app import run as run_tui

    run_tui(offline=offline, nsfw=nsfw)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _client_or_spawn() -> DaemonClient:
    try:
        return ensure_daemon()
    except TimeoutError as exc:
        typer.echo(f"could not reach or start the daemon: {exc}", err=True)
        raise typer.Exit(1)


def _run_streamed_job(client, submit, *, verb: str, label: str, detach: bool, json_out: bool):
    """Submit a job, then either detach or stream it to completion (§3, §11).

    The shared tail of every mutating CLI verb: call ``submit()`` (a thunk that returns
    the new job id) and map a :class:`DaemonError` to ``cannot <verb>: …`` + exit 1;
    with ``--detach`` print the submitted notice and return; else stream the progress,
    echo the terminal status, dump ``--json`` detail, and propagate a failed status as a
    non-zero exit. ``verb`` names the op for the error line; ``label`` is the stream/echo
    label (often the same, but e.g. ``dedup --confirm`` differs).
    """
    try:
        job_id = submit()
    except DaemonError as exc:
        typer.echo(f"cannot {verb}: {_detail(exc)}", err=True)
        raise typer.Exit(1)
    if detach:
        typer.echo(f"submitted {label} — running in the daemon; `packrat jobs` to check.")
        return
    final = stream_job(client, job_id, label=label)
    typer.echo(f"{label} {final}")
    if json_out:
        typer.echo(json.dumps(client.get_job(job_id), indent=2))
    _exit_if_failed(final)


def _is_auth_error(exc: DaemonError) -> bool:
    """True if a ``DaemonError`` is a 401 (token rejected) — the orphaned-daemon signal (§3).

    ``DaemonError`` messages are ``"<status>: <body>"`` (see ``client._post``); a leading
    ``401`` means ``/health`` answered but our token didn't match, i.e. a daemon from a
    since-deleted ``PACKRAT_HOME``. Callers self-heal by force-stopping it by port.
    """
    return str(exc).startswith("401")


def _detail(exc: DaemonError) -> str:
    """Pull the human message out of a ``DaemonError`` ("<code>: <json/text>")."""
    msg = str(exc)
    try:
        _code, _, body = msg.partition(": ")
        parsed = json.loads(body)
        # A well-formed FastAPI error body is {"detail": …}; tolerate a list/str/other
        # JSON body (would otherwise AttributeError on .get) by falling back to the raw.
        return parsed.get("detail", msg) if isinstance(parsed, dict) else msg
    except (ValueError, TypeError):
        return msg


#: Streamed-job terminal statuses that mean the work did NOT succeed. `detached` (a
#: Ctrl-C view detach — the job keeps running) is deliberately EXCLUDED: it's not a
#: failure, so it stays exit 0.
_FAILED_STATUSES = {"error", "cancelled", "interrupted"}


def _exit_if_failed(final: str) -> None:
    """Exit non-zero when a streamed job ended in a non-success terminal state (§11).

    ``stream_job`` returns the durable terminal status; a mutating command must
    propagate a failed/cancelled/interrupted job as a non-zero exit so scripts and CI
    (``packrat scan && packrat dedup``) don't treat a failed job as success. A clean
    Ctrl-C ``detached`` and a ``done`` both stay exit 0.
    """
    if final in _FAILED_STATUSES:
        raise typer.Exit(1)


def _review_verb(pr: dict) -> str:
    """The CLI verb that confirms/cancels a pending review run (§11)."""
    return "cleanup" if pr.get("run_type") == "cleanup-perceptual" else "dedup"


def _review_count_summary(pr: dict) -> str:
    """A one-line actionable count for a pending review run (§11 status).

    dedup: ``N to delete (exact) · G groups / M members (near-dup, default-keep)``.
    cleanup: ``X exact-trash (will delete) · P perceptual candidates (staged)``.
    """
    c = pr.get("counts") or {}
    if pr.get("run_type") == "cleanup-perceptual":
        return (f"{c.get('exact', 0)} exact-trash (will delete) · "
                f"{c.get('perceptual', 0)} perceptual candidate(s) (staged)")
    return (f"{c.get('to_delete_exact', 0)} to delete (exact) · "
            f"{c.get('groups', 0)} group(s) / {c.get('members', 0)} member(s) (near-dup, default-keep)")


def _scan_recency(r: dict) -> str:
    """A short scan-recency suffix for a root row.

    Uses ``last_scan_at`` (max ``last_seen_at`` — bumped by *any* scan) as the
    primary signal, so a plain incremental scan no longer reads as "never
    scanned". ``never scanned`` means no scan has touched a file here yet. Full
    scans (``scan --full``, the integrity backstop) aren't distinguished in this
    one-line view — see ``packrat status <root>`` for the last-full-scan detail.
    """
    if r.get("kind") == "trash":
        return ""
    last = r.get("last_scan_at")
    return f", scanned {_short_ts(last)}" if last else ", never scanned"


def _short_ts(ts: str | None) -> str:
    """Trim an ISO timestamp to ``YYYY-MM-DD HH:MM`` for compact display."""
    if not ts:
        return "never"
    return ts[:16].replace("T", " ")


def _print_last_scan(d: dict) -> None:
    """Render the root's most-recent persisted scan result + problem files (§scan-results)."""
    ls = d.get("last_scan")
    if not ls:
        return
    flags = "".join(f" --{k}" for k in ("full", "embed") if ls.get(k))
    # Prefer the live current undecodable count (re-derived from the catalog) over the
    # frozen last-scan number, so it agrees with the (also-live) problem-files list after
    # a `cleanup --undecodable` or a decoder-upgrade rescan.
    undec = d.get("undecodable_current", ls.get("undecodable", 0))
    typer.echo(
        f"  last scan result ({_short_ts(ls.get('created_at'))}{flags}): "
        f"{ls.get('new', 0)} new · {ls.get('exact_dup', 0)} exact-dup · "
        f"{ls.get('backfilled', 0)} filled-in · {ls.get('matches_trashed', 0)} identified-trash · "
        f"{undec} undecodable (now) · {ls.get('errors', 0)} errors"
    )
    problems = d.get("problem_files", [])
    if problems:
        typer.echo(f"  problem files ({len(problems)}):")
        for pf in problems:
            typer.echo(f"    [{pf['problem']}] {pf['path']}")
            if pf.get("detail"):
                typer.echo(f"        {pf['detail']}")


def main() -> None:
    # Windows consoles default to a legacy codepage (cp1252) that mangles the
    # UTF-8 glyphs we print (·, ●, ⚠). Reconfigure stdio to UTF-8 so output is
    # clean regardless of the active codepage.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    app()


if __name__ == "__main__":
    sys.exit(main())  # type: ignore[func-returns-value]
