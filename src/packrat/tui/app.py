"""The packrat TUI application (M6, §12) — the Textual :class:`App` + its wiring.

Holds the live read-model state (snapshots refreshed on a poll timer + job-finished
SSE) and the daemon client; the individual screen classes live in
:mod:`packrat.tui.frames` (imported here). Actions map to CLI verbs / daemon endpoints
(§1.6) — the TUI issues no privileged op of its own.
"""

from __future__ import annotations

import time

from textual import work
from textual.app import App
from textual.binding import Binding

from . import demo, fixtures
from .colorize import colorize
from .data import EtaEstimator, reltime
from .frames import Dashboard, FrameScreen, RootDetailScreen, _empty_snapshot
from .framing import screen
from .modals import ConfirmModal, TrashRefreshModal

# ---------------------------------------------------------------------------
# the app
# ---------------------------------------------------------------------------
class PackratApp(App):
    """The packrat TUI application (§12).

    Holds the live read-model state (refreshed on a poll timer + job-finished SSE)
    and the daemon client. ``offline`` mode renders from :mod:`packrat.tui.fixtures`
    so the UI is runnable/demoable without a daemon (and drives the golden tests).
    """

    CSS_PATH = "packrat.tcss"
    # Ctrl-Q is the hard quit (priority=True → fires from ANY screen/widget, incl. a
    # modal or a focused Input). We deliberately do NOT bind Ctrl-C: Windows Terminal
    # sends Ctrl+Shift+C (copy) as the same byte as Ctrl+C, so binding Ctrl+C would
    # hijack the copy shortcut. Esc backs out / quits from the top screen (per-screen
    # `escape` bindings); Ctrl-Q is the anywhere-quit.
    BINDINGS = [Binding("ctrl+q", "quit", "quit", show=False, priority=True)]

    def __init__(self, *, client=None, offline: bool = False, now: str | None = None,
                 nsfw: bool = False):
        # ansi_color=True disables Textual's ANSIToTruecolor filter, which would
        # otherwise rewrite our `background: ansi_default` (the ansi=-1 sentinel)
        # into a concrete opaque RGB fill. With the filter off, the transparent
        # background is emitted as `\x1b[49m` (reset-to-terminal-default), so the
        # terminal's own acrylic/"glass" background shows through (§12 chrome).
        super().__init__(ansi_color=True)
        self.client = client
        self.offline = offline or client is None
        # Display-only NSFW keyword redaction (--nsfw): masks adult keywords found in
        # the live roots' name/path, wherever those literal values appear in the
        # composed frame (just before colorize), never in the real names/paths the
        # daemon acts on. See packrat.tui.nsfw. `_redactions` is rebuilt only when the
        # roots change (keyed by their name/path signature) — not per keypress/anim tick.
        self.nsfw = nsfw
        self._redaction_sig: tuple | None = None
        self._redaction_cache: list[tuple[str, str]] = []
        # `now` drives every relative time (reltime): last-scan, job ages, card headers.
        # ONLINE it must track the wall clock (`_now=None` → the property returns live
        # now_iso()); a FIXED value is used only when explicitly pinned (tests) or in the
        # offline demo, whose sample timestamps are relative to fixtures.REFERENCE_NOW.
        # (Regression: defaulting to REFERENCE_NOW online froze the clock at a fixture
        # date, so a just-finished job rendered as a future calendar date.)
        if now is not None:
            self._now = now
        elif self.offline:
            self._now = fixtures.REFERENCE_NOW
        else:
            self._now = None   # live wall clock (see the `now` property)
        # A COMPLETE zeroed snapshot, not `{}` — the pure builders index required keys
        # directly, so an empty dict would KeyError before the first fetch / when the
        # daemon is down (the fallback client at run() exists precisely for that case).
        self.snapshot: dict = _empty_snapshot()
        self.recent: list[dict] = []
        self.header_right = "daemon ● up"
        # Live-progress plumbing (§3 SSE + §cross-cutting TUI-side ETA). The poll timer
        # is only the backstop; the running job's bar/ETA are driven by an SSE stream
        # (`_stream_running`) whose samples feed `_eta`. `_streamed_job_id`/`_stream_alive`
        # guard against double-subscribing and let a dropped stream reconnect (§3).
        self._eta = EtaEstimator()
        self._streamed_job_id: int | None = None
        self._stream_alive = False
        self._last_stream_render = 0.0     # coalesces per-file SSE repaints (see below)

    @property
    def now(self) -> str:
        """The reference 'now' for relative-time rendering (§12).

        A pinned value (tests) or the offline demo's fixed reference when set;
        otherwise the LIVE wall clock, so online timestamps age against real time.
        """
        if self._now is not None:
            return self._now
        from ..util import now_iso
        return now_iso()

    def redactions(self) -> list[tuple[str, str]]:
        """The current ``(value, masked)`` redaction pairs, or ``[]`` when ``--nsfw`` is
        off / nothing is sensitive.

        Sourced from the live roots' name/path (:func:`packrat.tui.nsfw.build_redactions`)
        and **memoized** against a signature of those name/path values, so we rebuild the
        keyword scan only when the roots actually change — not on every keypress, poll,
        or logo-animation tick (which re-render the frame)."""
        if not self.nsfw:
            return []
        roots = self.snapshot.get("roots", [])
        sig = tuple((r.get("name"), r.get("path")) for r in roots)
        if sig != self._redaction_sig:
            from .nsfw import build_redactions
            self._redaction_cache = build_redactions(roots)
            self._redaction_sig = sig
        return self._redaction_cache

    def view(self, obj):
        """A **render-only** masked copy of read-model ``obj`` (dict/list/scalar).

        Deep-copies ``obj`` with the sensitive root name/path values redacted on every
        string leaf (:func:`packrat.tui.nsfw.mask_obj`) — fed to the pure builders so a
        keyword is masked **before** layout, closing the middle/end-elision leak (a path
        cut by ``…`` after masking splits ``░``, not the keyword; post-layout redaction
        couldn't match the broken value). The screens pass ``view(...)`` to their builders
        for DISPLAY but keep the raw ``snapshot``/detail/job dicts for ACTIONS, so
        navigation and submits still route on the true name. Identity (same object, no
        copy) when ``--nsfw`` is off — the builders see the unmodified read model."""
        from .nsfw import mask_obj
        return mask_obj(obj, self.redactions())

    def notify(self, message: str, *, title: str = "", **kw) -> None:
        """App toast — NSFW-masked (title + body) when ``--nsfw`` is on.

        Action toasts echo the CLI verb they ran (``packrat scan <root>`` etc.), so
        they'd otherwise leak a root name/path the frame masks. Masking here keeps the
        redaction consistent across the whole UI; a no-op when ``--nsfw`` is off (the
        common case, incl. every test)."""
        reds = self.redactions()
        if reds:
            from .nsfw import redact
            message, title = redact(message, reds), redact(title, reds)
        super().notify(message, title=title, **kw)

    def on_mount(self) -> None:
        self.refresh_data()
        self.push_screen(Dashboard())
        if not self.offline:
            from .tokens import POLL_INTERVAL_S
            self.set_interval(POLL_INTERVAL_S, self.refresh_data)

    # -- data ---------------------------------------------------------------
    def refresh_data(self) -> None:
        """Re-fetch the snapshot + recent jobs (poll backstop / job-finished trigger).

        ONLINE the fetch is two blocking httpx calls, so it runs in a WORKER THREAD
        (:meth:`_fetch_online`) and marshals the result back to the UI thread — a slow
        or hung daemon must never freeze keyboard input / rendering on the poll timer.
        OFFLINE (in-memory demo data, no I/O) applies inline. Tests that call this
        directly on a running app get the async worker; the synchronous apply path
        (:meth:`_apply_data`) is what actually mutates state + re-renders."""
        if self.offline:
            self._apply_data(demo.status_snapshot(running=True), demo.recent_jobs(),
                             "v0.1.0 · daemon ● up")
            return
        # Online: fetch off the UI thread. If no app loop is running (a bare unit test
        # driving refresh_data on an un-mounted app), fall back to a synchronous fetch.
        if self._app_loop_running():
            self._fetch_online()
        else:
            self._apply_data(*self._blocking_fetch())

    def _app_loop_running(self) -> bool:
        """True when the Textual event loop is up (so worker threads can marshal back)."""
        try:
            return bool(self.is_running)   # Textual App: True while run()/run_test() is active
        except Exception:  # noqa: BLE001 - be conservative: no loop → synchronous path
            return False

    def _blocking_fetch(self) -> tuple[dict, list, str]:
        """The blocking daemon fetch (status + recent jobs). Returns (snapshot, recent,
        header_right); degrades to a zeroed snapshot + 'down' header if unreachable."""
        try:
            return self.client.status(), self.client.list_jobs(20), "v0.1.0 · daemon ● up"
        except Exception:
            # Daemon unreachable/erroring: a zeroed snapshot (so the frame still draws)
            # + a 'down' header — never crash / leave a partial dict a builder KeyErrors on.
            return _empty_snapshot(), [], "v0.1.0 · daemon ○ down"

    @work(thread=True, exclusive=True, group="poll-fetch")
    def _fetch_online(self) -> None:
        """Worker-thread poll fetch (blocking httpx), applied back on the UI thread."""
        snap, recent, header = self._blocking_fetch()
        try:
            self.call_from_thread(self._apply_data, snap, recent, header)
        except Exception:
            pass   # app tearing down

    def _apply_data(self, snapshot: dict, recent: list, header_right: str) -> None:
        """Install a freshly-fetched read-model + re-render (UI thread only)."""
        self.snapshot = snapshot
        self.recent = recent
        self.header_right = header_right
        # Feed the SSE-less poll path into the ETA estimator + keep the live stream
        # subscribed to whatever job is now running (fix: the "live" bar was poll-only).
        self._track_running()
        # Re-render the top screen if it's mounted (detail screens reload their own
        # per-root data on the poll, NOT on every keypress — see FrameScreen.poll_reload).
        if self.screen_stack and isinstance(self.screen, FrameScreen):
            self.screen.poll_reload()
            self.screen.refresh_frame()

    # -- live progress: SSE stream + TUI-side ETA (§3 / §cross-cutting) ------
    def _track_running(self) -> None:
        """Keep the live SSE stream attached to the current running job + inject ETA.

        Called on every poll refresh. **Online only** — offline demo/fixture jobs carry
        a fixed ``_eta_s`` in the sample data and need no stream/estimator. When the
        running job changes (or none runs) the estimator resets; otherwise the poll
        sample folds in and the derived ETA is written onto the running row so the pure
        builders (:func:`render.progress_bar`) render ``ETA …``. Starting the stream is
        idempotent (guarded by ``_stream_alive``); a dropped stream reconnects here."""
        if self.offline:
            return
        running = self.snapshot.get("running")
        if not running:
            self._streamed_job_id = None
            self._eta.reset()
            return
        jid = running.get("id")
        if jid != self._streamed_job_id:      # a new running job → fresh estimate + stream
            self._streamed_job_id = jid
            self._eta.reset()
            self._stream_alive = False
        self._observe(running)
        # Attach the live stream if the client supports SSE and none is attached. A
        # dropped/failed stream clears `_stream_alive`, so the NEXT poll re-attaches
        # (§3 reconnect) — the poll cadence is the backoff, no tight retry loop.
        if not self._stream_alive and jid is not None and hasattr(self.client, "stream_job"):
            self._stream_running(jid)

    def _observe(self, running: dict) -> None:
        """Fold one ``done`` sample into the estimator + stamp the derived ETA on the row."""
        done = running.get("done")
        if done is not None:
            self._eta.observe(time.monotonic(), done)
        running["_eta_s"] = self._eta.eta_s(running.get("total"))

    @work(thread=True, exclusive=True, group="job-stream")
    def _stream_running(self, job_id: int) -> None:
        """Subscribe to the running job's SSE stream, pushing live progress + ETA.

        Runs in a worker thread — ``client.stream_job`` is a blocking httpx generator.
        Each progress/state event updates the running row's ``done``/``total`` + the
        TUI-side ETA and re-renders on the UI thread (via :meth:`call_from_thread`); a
        terminal event triggers a full refetch so history/result cards appear. A dropped
        or unreachable stream just ends the worker — the poll backstop re-attaches (§3,
        job state is durable). ``exclusive`` cancels any prior stream in the group."""
        self._stream_alive = True
        finished = False                      # True only on a clean terminal event
        try:
            for ev in self.client.stream_job(job_id):
                if self._streamed_job_id != job_id:
                    break                     # a newer job took over — let this stream die
                etype = ev.get("type")
                if etype in ("progress", "state") and ev.get("done") is not None:
                    self.call_from_thread(self._apply_stream_progress, job_id, ev)
                if etype in ("done", "error") or ev.get("status") in (
                        "done", "error", "cancelled", "interrupted"):
                    finished = True
                    break
        except Exception:
            pass                              # dropped / daemon gone → poll reconnects
        finally:
            self._stream_alive = False
            # Refetch immediately ONLY on a clean job-finished event (history/result
            # card appear at once). A mid-stream DROP does NOT refetch here — that would
            # re-attach instantly and tight-loop if the daemon keeps erroring; the poll
            # timer reconnects on its own cadence (§3 durable state).
            if finished:
                try:
                    self.call_from_thread(self.refresh_data)
                except Exception:
                    pass                      # app tearing down

    def _apply_stream_progress(self, job_id: int, ev: dict) -> None:
        """Fold one SSE progress event into the running row + re-render (UI thread).

        The in-memory counters + ETA update on EVERY event (no data lost), but the
        repaint is COALESCED to ``STREAM_RENDER_INTERVAL_S`` — a scan fires one event
        per file (hundreds/sec), and re-laying-out the whole frame that often is what
        made the TUI laggy (issue #1). Between repaints the next poll tick still shows
        the latest value, so nothing stalls visually."""
        running = self.snapshot.get("running")
        if not running or running.get("id") != job_id:
            return
        if ev.get("done") is not None:
            running["done"] = ev["done"]
        if ev.get("total") is not None:
            running["total"] = ev["total"]
        self._observe(running)
        from .tokens import STREAM_RENDER_INTERVAL_S
        now = time.monotonic()
        if now - self._last_stream_render < STREAM_RENDER_INTERVAL_S:
            return                            # coalesce: skip this repaint, data already updated
        self._last_stream_render = now
        if self.screen_stack and isinstance(self.screen, FrameScreen):
            self.screen.refresh_frame()

    def sorted_roots(self) -> list[dict]:
        from . import render
        return render.sort_roots(self.snapshot.get("roots", []), 0)

    def root_path(self, name: str) -> str | None:
        """The on-disk path of a root by name (from the current snapshot), or None."""
        for r in self.snapshot.get("roots", []):
            if r.get("name") == name:
                return r.get("path")
        return None

    def _root_kind(self, name: str) -> str | None:
        """The ``kind`` (library|trash) of a root by name (current snapshot), or None."""
        for r in self.snapshot.get("roots", []):
            if r.get("name") == name:
                return r.get("kind")
        return None

    def open_root(self, name: str) -> None:
        """Open a root the right way for its kind (§6.1 — trash has no detail screen).

        A **library** root opens its RootDetailScreen (scan/dedup/merge/cleanup). A
        **trash** root has no detail — its only meaningful action is *refresh the
        collection* — so it opens the packrat-with-a-trash-can confirm modal instead;
        confirming maps to ``packrat trash refresh <root>``. Centralized so every
        entry point (Dashboard roots box, RootsMax list) treats trash roots alike."""
        if self._modal_on_top():
            return
        if self._root_kind(name) == "trash":
            self._confirm_trash_refresh(name)
        else:
            self.push_screen(RootDetailScreen(name))

    def _confirm_trash_refresh(self, name: str) -> None:
        """Push the trash mascot modal; on [y] submit ``trash refresh <name>`` (§6.1)."""
        def after(ok):
            if ok:
                self.run_verb(f"packrat trash refresh {name}", title="trash refresh",
                              submit=lambda: self.client.submit_trash_refresh(name))

        self.push_screen(TrashRefreshModal(name), after)

    def root_detail(self, name: str):
        """Return ``(detail_dict, jobs)`` for a root by name (offline → demo)."""
        if self.offline:
            return demo.root_detail(name), demo.root_jobs(name)
        try:
            d = self.client.status(name).get("root_detail")
            jobs = self.client.root_jobs(d["id"]) if d else []
            if d is not None:
                self._inject_live_progress(d.get("running_job"))
            return d, jobs
        except Exception:
            return None, []

    def _inject_live_progress(self, job: dict | None) -> None:
        """Copy the live ``done``/``total``/``_eta_s`` onto a per-view running-job dict.

        The SSE stream + TUI-side ETA are tracked against ``snapshot["running"]`` only;
        the root-detail view fetches its ``running_job`` from a *separate* daemon call
        that never carries the estimate. When it's the SAME job we're streaming, mirror
        the freshest counters + ETA onto it so root detail shows the same live bar/ETA
        as the dashboard/Queue (not a stale, ETA-less snapshot)."""
        if not job:
            return
        live = self.snapshot.get("running")
        if live and live.get("id") == job.get("id"):
            for k in ("done", "total", "_eta_s"):
                if live.get(k) is not None:
                    job[k] = live[k]

    def job_problem_files(self, job: dict) -> list[dict]:
        """A scan job's undecodable/read-error files (paths + reasons, §12 card)."""
        if self.offline:
            return demo.job_problem_files(job["id"])
        try:
            return self.client.job_problem_files(job["id"])
        except Exception:
            return []

    # -- actions (§1.6: every action maps to a CLI verb) --------------------
    def _modal_on_top(self) -> bool:
        """True if a Modal is already the active screen — the re-entrancy guard.

        A background screen's key binding can still fire while a modal is open (a
        modal only *focuses* its own widgets; Textual bubbles keys it doesn't bind
        down the stack). If that background action pushed another modal, rapid
        dismissal underflows the stack (the ``No screens on stack`` crash / hang).
        Refusing to push a second modal here is the single, driver-safe choke point.
        """
        from .modals import Modal
        try:
            return isinstance(self.screen, Modal)
        except Exception:
            return False

    def run_verb(self, cmd: str, *, title: str = "would run", submit=None,
                 then=None) -> None:
        """Run an action that maps to a CLI verb (§1.6), reporting via a **toast**.

        ``cmd`` is the human display string (the CLI verb it corresponds to).
        ``submit`` is a zero-arg callable that performs the real daemon call
        (``client.submit_*``) and returns a job id; pass it for actions that
        actually submit work. ``then`` is an optional zero-arg callable fired right
        after the toast is posted — e.g. the JobCard passes ``self._back`` so an
        action pops the card back to the interface that opened it (§5).

        These are the actions that **do not need a confirmation** (a confirm-gated
        action goes through :meth:`confirm_verb` → the ConfirmModal first). So the
        result is a non-blocking Textual toast, never a modal popup:
        - **offline** demo: no daemon → an info toast showing ``cmd`` (the walkable
          "what this would run"); ``submit`` is not called.
        - **online**: call ``submit()`` and show an info toast "submitted — job #N",
          then refresh so the new job appears; on an exception, a **red error toast**
          (the command failed to even submit) instead of crashing.

        **Modal guard:** a background screen's action key can bubble down the stack and
        reach here while a modal is open (Textual only *focuses* the modal's widgets),
        which would fire a real daemon submit underneath the modal. Refuse when a modal
        is on top. (A confirm-gated action is safe: :meth:`confirm_verb` calls this from
        its post-dismiss callback, when no modal is on top.)
        """
        if self._modal_on_top():
            return
        if self.offline or submit is None:
            self.notify(cmd, title=title, severity="information")
            if then:
                then()
            return
        try:
            job_id = submit()
        except Exception as exc:
            self.notify(f"{cmd}\n{exc}", title="couldn't run", severity="error")
            if then:
                then()
            return
        self.refresh_data()
        note = f"submitted — job #{job_id}" if job_id else "submitted"
        self.notify(f"{cmd}\n{note} — watch it in the Queue.",
                    title="submitted", severity="information")
        if then:
            then()

    def confirm_verb(self, question: str, cmd: str, *, count: int | None = None,
                     network: int = 0, submit=None, then=None) -> None:
        """Confirm (y/n or typed-count), then run the verb (§1.6 — gather then act).

        On confirm, delegates to :meth:`run_verb` (offline → notice; online →
        ``submit()`` + result notice). ``submit`` is the real daemon call; ``then``
        is forwarded to :meth:`run_verb` and so fires only after a confirmed action's
        toast — declining (``n``) posts no toast and runs no ``then``.
        """
        if self._modal_on_top():
            return
        # The chained action (after confirm) runs once THIS modal has dismissed —
        # so run_verb there sees no modal on top and is allowed (ask→act sequence).
        def after(ok):
            if ok:
                self.run_verb(cmd, title="confirmed", submit=submit, then=then)

        self.push_screen(ConfirmModal(question, count=count, network=network), after)


def run(*, offline: bool = False, nsfw: bool = False) -> None:
    """Launch the TUI (the ``packrat`` no-args entrypoint, §12).

    ``nsfw`` enables the display-only adult-keyword redaction (``--nsfw``) — root
    names/paths are masked on screen only (:mod:`packrat.tui.nsfw`), never in the
    real state the daemon acts on."""
    client = None
    if not offline:
        from ..daemon.client import DaemonClient
        from ..daemon.spawn import ensure_daemon
        try:
            client = ensure_daemon()
        except Exception:
            client = DaemonClient()  # render daemon-down state rather than crash
    PackratApp(client=client, offline=offline, nsfw=nsfw).run()

