"""``packrat`` CLI entrypoint (Typer) — thin client onto the daemon (§3, §11).

Current surface (M1/M2 — dedup/merge/trash land M3+):
- ``packrat roots register|list`` — declare/list roots (§8 A1, §11).
- ``packrat scan`` — walk a root and fingerprint it (§8 A2).
- ``packrat status`` — global rollup / per-root detail (read-only, never blocked, §11).
- ``packrat jobs`` — recent job runs.
- ``packrat cancel`` — cooperatively cancel the running job (§3).
- ``packrat daemon start|stop|restart|status`` — lifecycle/troubleshooting (§11).
- ``packrat smoke-test`` — the §9.1 decode smoke test.
- ``packrat`` (no args) — the TUI placeholder (full TUI is M6, §12).

Every job-submitting command auto-spawns the daemon on first use (§3), submits,
and streams; Ctrl-C detaches the view without stopping the job.
"""

from __future__ import annotations

import sys
import time
from typing import List, Optional

import typer

from .. import __version__, build
from ..daemon.client import BusyResponse, DaemonClient, DaemonError, DaemonNotRunning
from ..daemon.spawn import ensure_daemon
from ..daemon.state import DEFAULT_PORT, read_state, pid_alive
from .stream import stream_job

app = typer.Typer(
    name="packrat",
    help="Local media-collection manager: fingerprint dedup + Explorer merge/trash workflow.",
    no_args_is_help=False,
    add_completion=False,
)

daemon_app = typer.Typer(help="Manage the background daemon (§11).")
app.add_typer(daemon_app, name="daemon")

roots_app = typer.Typer(help="Manage roots: register + list (§8 A1, §11).", invoke_without_command=True)
app.add_typer(roots_app, name="roots")

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


@daemon_app.command("stop")
def daemon_stop():
    """Graceful shutdown: an in-flight job is left `interrupted` (resumable), not cancelled (§3)."""
    client = DaemonClient()
    if not client.is_up():
        typer.echo("daemon is not running.")
        raise typer.Exit(0)
    try:
        resp = client.shutdown()
    except (DaemonError, DaemonNotRunning) as exc:
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

    Useful after upgrading packrat so the daemon picks up new code (§9.2 config is
    reloaded per job, but the *code* only changes on restart). A graceful stop
    leaves any in-flight job `interrupted` (resumable) — re-run its command to
    resume it (§3). Because the daemon holds a fixed-port single-instance lock
    (§3), we wait for the old one to release the port before spawning the new one,
    so the replacement can bind.
    """
    client = DaemonClient()
    if client.is_up():
        try:
            resp = client.shutdown()
        except (DaemonError, DaemonNotRunning) as exc:
            typer.echo(f"error stopping daemon: {exc}", err=True)
            raise typer.Exit(1)
        if resp.get("running_job"):
            typer.echo("stopping daemon — the in-flight job is left interrupted (resumable).")
        # Wait for the old daemon to stop serving and free the port; a new spawn
        # can't bind until it does (the port bind is the single-instance lock, §3).
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
    info = client.daemon_status()
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
    """Print collection state (read-only, never blocked by a running job, §11)."""
    client = _client_or_spawn()
    if root:
        try:
            resp = client.status(root)
        except DaemonError as exc:
            typer.echo(_detail(exc), err=True)
            raise typer.Exit(1)
        d = resp["root_detail"]
        if json_out:
            import json
            typer.echo(json.dumps(d, indent=2))
            return
        typer.echo(f"[{d['id']}] {d['name']}  {d['path']}  ({d['kind']})")
        typer.echo(f"  assets: {d['photos'] + d['videos']} (photos {d['photos']} · videos {d['videos']})")
        typer.echo(f"  files: {d['instances']}")
        typer.echo(f"  last scan: {_short_ts(d.get('last_scan_at'))}")
        typer.echo(f"  last full scan: {_short_ts(d['last_full_scan_at']) if d['last_full_scan_at'] else 'never'}")
        if d.get("pending_review"):
            pr = d["pending_review"]
            typer.echo(f"  ⚠ {pr['run_type']} pending since {pr['created_at']} — --confirm/--cancel to free the root.")
        return
    snap = client.status()
    if json_out:
        import json
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
        typer.echo(f"running: {rj['type']} ({rj['done']}/{rj['total']})")
    for pr in snap.get("pending_reviews", []):
        typer.echo(f"⚠ {pr['run_type']} pending on {pr['root_name']} — --confirm/--cancel to free it.")
    for it in snap.get("interrupted", []):
        typer.echo(f"⚠ interrupted: {it['type']} — re-run its command to resume.")


@roots_app.callback(invoke_without_command=True)
def _roots_root(ctx: typer.Context):
    """Bare ``packrat roots`` is an alias for ``packrat roots list`` (§11)."""
    if ctx.invoked_subcommand is None:
        _roots_list(json_out=False)


@roots_app.command("list")
def roots_list(json_out: bool = typer.Option(False, "--json")):
    """List registered roots (read-only, §11)."""
    _roots_list(json_out=json_out)


def _roots_list(*, json_out: bool) -> None:
    client = _client_or_spawn()
    rs = client.roots()
    if json_out:
        import json
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
    ignore: List[str] = typer.Option([], "--ignore", help="Extra ignore glob (repeatable, §8 A1)."),
    scan: bool = typer.Option(False, "--scan", help="After registering, immediately scan the root."),
    full: bool = typer.Option(False, "--full", help="With --scan, do a full (re-fingerprint) scan."),
    embed: bool = typer.Option(False, "--embed", help="With --scan, also run the CLIP pass (implies --scan; M7)."),
    detach: bool = typer.Option(False, "--detach", help="With --scan, submit and return without streaming."),
    json_out: bool = typer.Option(False, "--json"),
):
    """Declare a folder as a root (metadata-only, instantaneous, §8 A1)."""
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
        import json
        typer.echo(json.dumps(resp, indent=2))
        raise typer.Exit(0)
    typer.echo(f"registered root [{row['id']}] {row['name']}  {row['path']}  ({row['kind']}) — not yet scanned.")
    if resp.get("scan_busy"):
        typer.echo(f"  (scan not started — {resp['scan_busy']})")
    job_id = resp.get("job_id")
    if job_id and not detach:
        final = stream_job(client, job_id, label="scan")
        typer.echo(f"scan {final}")
    elif job_id:
        typer.echo("  scan running in the daemon — `packrat jobs` to check.")


@app.command("jobs")
def jobs(limit: int = typer.Option(20, "--limit"), json_out: bool = typer.Option(False, "--json")):
    """List recent job runs (read-only)."""
    client = _client_or_spawn()
    js = client.list_jobs(limit)
    if json_out:
        import json
        typer.echo(json.dumps(js, indent=2))
        return
    if not js:
        typer.echo("no jobs yet.")
        return
    for j in js:
        started = (j.get("started_at") or "")[:19].replace("T", " ")
        line = f"  {started}  {j['type']:8s} {j['status']:11s} {j['done']}/{j['total']}"
        if j.get("error"):
            line += f"  err: {j['error']}"
        typer.echo(line)


# ---------------------------------------------------------------------------
# scan — walk a registered root and fingerprint it (§8 A2)
# ---------------------------------------------------------------------------
@app.command("scan")
def scan(
    path: Optional[str] = typer.Argument(None, help="A registered root (path or --name). Omit with --all."),
    all_roots: bool = typer.Option(False, "--all", help="Scan every enabled root."),
    full: bool = typer.Option(False, "--full", help="Ignore the fast-path; re-fingerprint everything."),
    embed: bool = typer.Option(False, "--embed", help="Also compute CLIP embeddings (§7; deferred to M7)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Enumerate + report what would be indexed; write nothing."),
    profile: bool = typer.Option(False, "--profile", help="Report where time went: NAS transfer vs CPU vs decode (§10.1)."),
    detach: bool = typer.Option(False, "--detach", help="Submit and return without streaming."),
    json_out: bool = typer.Option(False, "--json"),
):
    """Walk a registered root and fingerprint new/changed files (resumable, §8 A2)."""
    if not all_roots and not path:
        typer.echo("scan needs a <root> path/name, or --all.", err=True)
        raise typer.Exit(2)
    if all_roots and path:
        typer.echo("give a <root> or --all, not both.", err=True)
        raise typer.Exit(2)
    client = _client_or_spawn()
    try:
        job_id = client.submit_scan(
            path, all_roots=all_roots, full=full, embed=embed, dry_run=dry_run, profile=profile
        )
    except BusyResponse as exc:
        _print_busy(exc)
        raise typer.Exit(1)
    except DaemonError as exc:
        typer.echo(f"cannot scan: {_detail(exc)}", err=True)
        raise typer.Exit(1)
    if detach:
        typer.echo("submitted scan — running in the daemon; `packrat jobs` to check.")
        return
    final = stream_job(client, job_id, label="scan")
    typer.echo(f"scan {final}")
    if json_out:
        import json
        typer.echo(json.dumps(client.get_job(job_id), indent=2))


@app.command("cancel")
def cancel(
    job_id: Optional[int] = typer.Argument(None, help="Job id (optional; defaults to the running job)."),
):
    """Cooperatively cancel the running job (§3).

    Only one mutating job runs at a time (§3 guarantee 1), so no id is needed —
    ``packrat cancel`` targets that job. An explicit id is still accepted.
    """
    client = _client_or_spawn()
    if job_id is None:
        rj = client.daemon_status().get("running_job")
        if not rj:
            typer.echo("no running job to cancel.")
            raise typer.Exit(0)
        job_id = rj["id"]
    ok = client.cancel_job(job_id)
    typer.echo("cancel requested." if ok else "that job is not running (nothing to cancel).")


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
    """Run the §9.1 decode→hash→perceptual→embed smoke test over sample files.

    With no argument, reports which deps are available. Pass a folder of samples
    to run the full path, or --generate to synthesize samples first (RAW formats
    can't be synthesized — supply real camera files for those).
    """
    from ..smoke import run_smoke_test

    code = run_smoke_test(samples, json_out=json_out, generate=generate)
    raise typer.Exit(code)


# ---------------------------------------------------------------------------
# no-args → TUI (M6). For M0, print a status-y placeholder.
# ---------------------------------------------------------------------------
@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context, version: bool = typer.Option(False, "--version")):
    if version:
        typer.echo(f"packrat {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is not None:
        return
    # No subcommand: the full TUI is M6 (§12). Until then, show a quick status.
    typer.echo(f"packrat {__version__} — the TUI (no-args entrypoint) lands in M6.")
    typer.echo("meanwhile: `packrat roots`, `packrat scan <root>`, `packrat status`, `packrat jobs`.")
    try:
        client = DaemonClient()
        if client.is_up():
            snap = client.status()
            typer.echo(f"\n· {snap['assets']} assets hoarded · daemon ● up ·")
        else:
            typer.echo("\n· daemon ○ down (auto-spawns on first use) ·")
    except DaemonNotRunning:
        pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _client_or_spawn() -> DaemonClient:
    try:
        return ensure_daemon()
    except TimeoutError as exc:
        typer.echo(f"could not reach or start the daemon: {exc}", err=True)
        raise typer.Exit(1)


def _detail(exc: DaemonError) -> str:
    """Pull the human message out of a ``DaemonError`` ("<code>: <json/text>")."""
    msg = str(exc)
    try:
        import json
        _code, _, body = msg.partition(": ")
        return json.loads(body).get("detail", msg)
    except (ValueError, TypeError):
        return msg


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


def _print_busy(exc: BusyResponse) -> None:
    # ``exc`` already reads "busy: <job>" / "root busy: <holder>" from the daemon.
    if exc.kind == "root":
        typer.echo(f"{exc} — confirm/cancel it first (§3).", err=True)
    else:
        typer.echo(f"{exc} — one mutating operation at a time (§3).", err=True)


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
