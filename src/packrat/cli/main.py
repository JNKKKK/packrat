"""``packrat`` CLI entrypoint (Typer) — thin client onto the daemon (§3, §11).

M0 surface (the runtime, not the operations — scan/dedup/merge land M1+):
- ``packrat daemon start|stop|status`` — lifecycle/troubleshooting (§11).
- ``packrat status`` — global rollup snapshot (read-only, never blocked, §11).
- ``packrat jobs`` — recent job runs.
- ``packrat demo`` — submit the M0 demo job and stream it (exercises submit /
  stream / Ctrl-C-detach / ``--detach`` / busy rejection).
- ``packrat smoke-test`` — the §9.1 decode smoke test.
- ``packrat`` (no args) — the TUI placeholder (full TUI is M6, §12).

Every job-submitting command auto-spawns the daemon on first use (§3), submits,
and streams; Ctrl-C detaches the view without stopping the job.
"""

from __future__ import annotations

import sys

import typer

from .. import __version__
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
            f"stopping daemon — job {resp['running_job']} left interrupted (resumable); "
            "re-run its command to resume."
        )
    else:
        typer.echo("daemon stopping.")


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
        typer.echo(f"  running: job {rj['id']} {rj['type']} ({rj['done']}/{rj['total']})")
    else:
        typer.echo("  no running job.")


# ---------------------------------------------------------------------------
# read-only snapshots
# ---------------------------------------------------------------------------
@app.command("status")
def status(json_out: bool = typer.Option(False, "--json", help="Machine-readable output.")):
    """Print collection state (read-only, never blocked by a running job, §11)."""
    client = _client_or_spawn()
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
            typer.echo(f"  [{r['id']}] {r['name']}  {r['path']}  ({r['kind']}, {r['asset_count']} assets)")
    else:
        typer.echo("roots: none registered yet — `packrat roots register <path>` (M1).")
    if snap.get("running"):
        rj = snap["running"]
        typer.echo(f"running: job {rj['id']} {rj['type']} ({rj['done']}/{rj['total']})")
    for it in snap.get("interrupted", []):
        typer.echo(f"⚠ interrupted: job {it['id']} {it['type']} — re-run its command to resume.")


@app.command("roots")
def roots(json_out: bool = typer.Option(False, "--json")):
    """List registered roots (read-only, §11). (Registration lands in M1.)"""
    client = _client_or_spawn()
    rs = client.roots()
    if json_out:
        import json
        typer.echo(json.dumps(rs, indent=2))
        return
    if not rs:
        typer.echo("no roots registered yet — `packrat roots register <path>` arrives in M1.")
        return
    for r in rs:
        typer.echo(f"  [{r['id']}] {r['name']}  {r['path']}  ({r['kind']}, {r['asset_count']} assets)")


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
        line = f"  [{j['id']}] {j['type']:8s} {j['status']:11s} {j['done']}/{j['total']}"
        if j.get("error"):
            line += f"  err: {j['error']}"
        typer.echo(line)


# ---------------------------------------------------------------------------
# demo job — exercises the M0 runtime end-to-end
# ---------------------------------------------------------------------------
@app.command("demo")
def demo(
    steps: int = typer.Option(10, "--steps"),
    delay: float = typer.Option(0.2, "--delay"),
    detach: bool = typer.Option(False, "--detach", help="Submit and return without streaming."),
):
    """Submit the M0 demo job and stream its progress (Ctrl-C detaches, §3)."""
    client = _client_or_spawn()
    try:
        job_id = client.submit("demo", {"steps": steps, "delay_s": delay})
    except BusyResponse as exc:
        _print_busy(exc)
        raise typer.Exit(1)
    typer.echo(f"submitted demo job {job_id}.")
    if detach:
        typer.echo("running in the daemon — `packrat jobs` to check, TUI to watch/stop.")
        return
    final = stream_job(client, job_id, label=f"demo#{job_id}")
    typer.echo(f"job {job_id}: {final}")


@app.command("cancel")
def cancel(job_id: int):
    """Cooperatively cancel a running job (§3)."""
    client = _client_or_spawn()
    ok = client.cancel_job(job_id)
    typer.echo("cancel requested." if ok else "job is not the running one (nothing to cancel).")


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
    typer.echo("meanwhile: `packrat status`, `packrat daemon status`, `packrat jobs`, `packrat demo`.")
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
