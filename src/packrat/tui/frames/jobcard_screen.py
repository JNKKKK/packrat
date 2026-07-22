"""The JobCard screen (M6, §12) — see :mod:`packrat.tui.frames.base`."""

from __future__ import annotations

from textual.binding import Binding

from ..data import reltime
from ..data import result_of
from ..framing import screen
from ..screens import jobcard

from .base import FrameScreen, _open_in_explorer


# ---------------------------------------------------------------------------
# Job result / detail card (§5)
# ---------------------------------------------------------------------------
class JobCard(FrameScreen):
    BINDINGS = [
        Binding("up", "scroll(-1)", show=False),
        Binding("down", "scroll(1)", show=False),
        Binding("c", "cancel", show=False),
        Binding("o", "open_review", show=False),
        Binding("g", "confirm_review", show=False),
        Binding("k", "cancel_review", show=False),
        Binding("escape", "app.pop_screen", show=False),
    ]

    def __init__(self, job: dict) -> None:
        super().__init__()
        self.job = job
        self._problems: list[dict] = []   # a scan card's undecodable/read-error files
        self.problems_scroll = 0

    def on_mount(self) -> None:
        # A terminal scan card lists its problem files (paths + reasons, §12); fetch
        # them once (they don't change for a finished job) BEFORE the first frame.
        self._load_problems()
        super().on_mount()

    def _op(self) -> str:
        return result_of(self.job).get("op") or self.job["type"]

    def _load_problems(self) -> None:
        if self.job.get("status") in ("running", "error", "interrupted"):
            return
        if self._op() == "scan":
            self._problems = self.app.job_problem_files(self.job)

    def _review_ui(self) -> str | None:
        """This card's live review-action mode — ``'current'`` (owns the pending stage:
        open/confirm/cancel), ``'advanced'`` (a later stage is pending: open/cancel
        only), or ``None`` (no review actions). Reconciled by the data layer against the
        live ``review_runs`` row, so a stale analyze/confirm card never offers actions
        for a stage that was already confirmed or a run that finished (§8 B)."""
        return jobcard.review_ui(self.job)

    def _pending(self) -> bool:
        """True while this card owns a live pending stage (open + confirm + cancel)."""
        return self._review_ui() == "current"

    def _verb(self) -> str:
        """The review CLI verb this card's op maps to — ``cleanup`` or ``dedup``.

        A paused ``cleanup --trash-perceptual`` also lands here (its analyze now emits
        ``review_status='pending'``), so confirm/cancel must NOT be hardcoded to dedup."""
        return "cleanup" if self._op() == "cleanup" else "dedup"

    def _submit_review(self, root: str, **kw):
        """Deferred confirm/cancel call for this card's review (dedup or cleanup)."""
        if self._verb() == "cleanup":
            return self.app.client.submit_cleanup(root, mode="perceptual", **kw)
        return self.app.client.submit_dedup(root, **kw)

    def frame(self) -> str:
        j = self.job
        right = reltime(j.get("finished_at") or j.get("started_at"), self.now)
        if j.get("status") == "running":
            footer = "[c] cancel job   Esc back"
        elif self._pending():
            footer = "[o] open review   [g] confirm stage   [k] cancel run   Esc back"
        elif self._review_ui() == "advanced":
            # This stage was already confirmed; the run advanced to a later stage. Offer
            # opening that stage's folder + cancelling the run — but NOT confirm (it would
            # apply a different stage than this card shows).
            footer = "[o] open review   [k] cancel run   Esc back"
        elif self._problems:
            footer = "↑/↓ scroll problem files   Esc back"
        else:
            footer = "Esc back"
        geo = self._geo = self.geo_for(footer)
        # DISPLAY masking before layout: the card label + result summary embed the root
        # name, and a scan card's problem-file PATHS are the worst elision case. Raw
        # self.job stays the source for actions (cancel/review, root_name lookups).
        vj, vproblems = self.app.view(j), self.app.view(self._problems)
        body = jobcard.card_body(vj, now=self.now, problem_files=vproblems,
                                 problems_scroll=self.problems_scroll, geo=geo)
        return screen(jobcard.card_title(vj), body, right,
                      footer=footer, width=geo.w, height=geo.h)

    def action_scroll(self, delta: int) -> None:
        # ↑/↓ scroll the problem-file window (no-op when the card has none).
        budget = jobcard.problem_budget(self.job, self._problems, self._geo)
        max_scroll = max(0, len(self._problems) - budget)
        new = max(0, min(self.problems_scroll + delta, max_scroll))
        if new != self.problems_scroll:
            self.problems_scroll = new
            self.refresh_frame()

    def _back(self) -> None:
        """Pop this card back to the interface that opened it (§5).

        Every JobCard action reports via a toast (``run_verb``/``confirm_verb``) and
        then returns here so the user lands back on the screen they came from — the
        root detail's Jobs panel or the Queue — rather than staring at a now-stale
        card. Guarded on ``is_active`` so a stray key on a lower screen can't pop the
        wrong screen, and re-checked because the card may already be gone."""
        if self.is_active and self.app.screen_stack:
            self.app.pop_screen()

    def action_cancel(self) -> None:
        if self.job.get("status") == "running":
            jid = self.job["id"]
            self.app.confirm_verb(f"Cancel running {self.job['label']} (#{jid})?",
                                  f"packrat jobs cancel {jid}",
                                  submit=lambda: self.app.client.cancel_job(jid),
                                  then=self._back)

    def _reviewable(self) -> bool:
        """Open/cancel apply while the run is still open — this card's stage (``current``)
        or a later one it advanced to (``advanced``). Confirm is ``current``-only."""
        return self._review_ui() in ("current", "advanced")

    def action_open_review(self) -> None:
        if self._reviewable():
            root = self.job.get("root_name", "")
            path = self.app.root_path(root)
            submit = (lambda: _open_in_explorer(path)) if path else None
            target = f"{path}\\_packrat_review\\" if path else f"<{root} review folder>"
            self.app.run_verb(f"explorer {target}", title="open in Explorer",
                              submit=submit, then=self._back)

    def _review_network_count(self, root: str) -> int:
        """How many of the pending review's current-stage delete candidates sit on a
        network share (permanent delete — §10). Read from the live root detail; any
        failure → 0 (no warning, never blocks the confirm)."""
        try:
            detail, _ = self.app.root_detail(root)
            return int(((detail or {}).get("pending_review") or {}).get("counts", {}).get("network", 0))
        except Exception:  # noqa: BLE001 - the warning is best-effort
            return 0

    def action_confirm_review(self) -> None:
        if self._pending():
            root = self.job.get("root_name", "")
            verb = self._verb()
            self.app.confirm_verb(f"Confirm this {verb} stage for {root}?",
                                  f"packrat {verb} {root} --confirm",
                                  network=self._review_network_count(root),
                                  submit=lambda: self._submit_review(root, confirm=True),
                                  then=self._back)

    def action_cancel_review(self) -> None:
        if self._reviewable():
            root = self.job.get("root_name", "")
            verb = self._verb()
            self.app.confirm_verb(f"Cancel the whole {verb} run for {root}?",
                                  f"packrat {verb} {root} --cancel",
                                  submit=lambda: self._submit_review(root, cancel=True),
                                  then=self._back)
