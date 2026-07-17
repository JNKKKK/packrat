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
from ..daemon.client import DaemonClient, DaemonError, DaemonNotRunning
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

trash_app = typer.Typer(help="Trash memory: refresh the trash folders (§6.1).")
app.add_typer(trash_app, name="trash")

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
        # Queued jobs have no start yet — show enqueue time so the row isn't blank.
        stamp = (j.get("started_at") or j.get("enqueued_at") or "")[:19].replace("T", " ")
        label = j.get("label") or j["type"]
        line = f"  {stamp}  {label:28s} {j['status']:11s} {j.get('done', 0)}/{j.get('total')}"
        result = j.get("result_json")
        if result:
            import json as _json
            try:
                summary = _json.loads(result).get("summary")
            except (ValueError, TypeError):
                summary = None
            if summary:
                line += f"  · {summary}"
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
    detach: bool = typer.Option(False, "--detach", help="Submit and return without streaming."),
    json_out: bool = typer.Option(False, "--json"),
):
    """Dedup one folder as a 3-stage sequence: analyze → --confirm (auto-advances) (§8 B).

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
    try:
        job_id = client.submit_dedup(folder, confirm=confirm, cancel=cancel, dry_run=dry_run,
                                     keep_suggested=keep_suggested)
    except DaemonError as exc:
        typer.echo(f"cannot dedup: {_detail(exc)}", err=True)
        raise typer.Exit(1)
    if detach:
        typer.echo(f"submitted {label} — running in the daemon; `packrat jobs` to check.")
        return
    final = stream_job(client, job_id, label=label)
    typer.echo(f"{label} {final}")
    if json_out:
        import json
        typer.echo(json.dumps(client.get_job(job_id), indent=2))


# ---------------------------------------------------------------------------
# cleanup — remove trashed content from a library folder (§6.2)
# ---------------------------------------------------------------------------
@app.command("cleanup")
def cleanup(
    folder: str = typer.Argument(..., help="A registered library root to clean (path or --name)."),
    trash_exact: bool = typer.Option(False, "--trash-exact", help="Delete files that are byte-identical to trashed content (§6.2)."),
    trash_perceptual: bool = typer.Option(False, "--trash-perceptual", help="Stage recompressed-trash matches for review; also deletes exact matches (§6.2)."),
    undecodable: bool = typer.Option(False, "--undecodable", help="Delete the folder's undecodable files + mark them trashed (§9.1)."),
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
    - `--undecodable`: files whose pixels won't decode (§9.1); deletes them and marks each
      asset trashed. Count → typed confirm → delete. Does not touch the trashed set.

    Trash modes refresh the trash collection for real, even under `--dry-run` (§6.1).
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
        try:
            job_id = client.submit_cleanup(
                folder, mode=mode, confirm=confirm, cancel=cancel, dry_run=dry_run
            )
        except DaemonError as exc:
            typer.echo(f"cannot cleanup: {_detail(exc)}", err=True)
            raise typer.Exit(1)
        if detach:
            typer.echo(f"submitted {label} — running in the daemon; `packrat jobs` to check.")
            return
        final = stream_job(client, job_id, label=label)
        typer.echo(f"{label} {final}")
        if json_out:
            import json
            typer.echo(json.dumps(client.get_job(job_id), indent=2))
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
        import json
        typer.echo(json.dumps(client.get_job(apply_job), indent=2))


def _mode_flag(mode: str) -> str:
    """Map an internal cleanup mode to its CLI flag name (for labels/messages)."""
    return {"exact": "trash-exact", "perceptual": "trash-perceptual",
            "undecodable": "undecodable"}[mode]


# ---------------------------------------------------------------------------
# trash — refresh the registered trash folders (§6.1)
# ---------------------------------------------------------------------------
@trash_app.command("refresh")
def trash_refresh(
    detach: bool = typer.Option(False, "--detach", help="Submit and return without streaming."),
    json_out: bool = typer.Option(False, "--json"),
):
    r"""Absorb whatever is in the registered trash folders into trash memory, then empty them (§6.1).

    Fingerprints each trash-folder file, records/flips its asset to `trashed` (kept
    forever), and moves the file to the Recycle Bin (permanent on NAS/SMB, §10). No
    `--dry-run` — refresh is never a no-op (§6.1); browse the folders in Explorer first
    to preview.
    """
    client = _client_or_spawn()
    try:
        job_id = client.submit_trash_refresh()
    except DaemonError as exc:
        typer.echo(f"cannot refresh trash: {_detail(exc)}", err=True)
        raise typer.Exit(1)
    if detach:
        typer.echo("submitted trash refresh — running in the daemon; `packrat jobs` to check.")
        return
    final = stream_job(client, job_id, label="trash refresh")
    typer.echo(f"trash refresh {final}")
    if json_out:
        import json
        typer.echo(json.dumps(client.get_job(job_id), indent=2))


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
    """Forget content from the trashed-hash set so it's no longer excluded from merges (§6.3).

    You present the file (packrat stores no pixels to preview); untrash hashes it and
    matches by exact content hash. It does NOT restore bytes (that's the Recycle Bin)
    and writes nothing to disk — only DB rows.
    """
    client = _client_or_spawn()
    try:
        job_id = client.submit_untrash(path, dry_run=dry_run)
    except DaemonError as exc:
        typer.echo(f"cannot untrash: {_detail(exc)}", err=True)
        raise typer.Exit(1)
    if detach:
        typer.echo("submitted untrash — running in the daemon; `packrat jobs` to check.")
        return
    final = stream_job(client, job_id, label="untrash")
    typer.echo(f"untrash {final}")
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
