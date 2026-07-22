"""Base screen + shared helpers for the TUI screen controllers (M6, §12).

:class:`FrameScreen` is the common base: a Textual :class:`~textual.screen.Screen`
holding one :class:`~textual.widgets.Static` that shows a composed 100×24 frame from a
pure builder. The concrete screens live in sibling modules of this package; the
:mod:`packrat.tui.frames` package ``__init__`` re-exports them all.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static

from ..colorize import colorize
from ..framing import screen
from ..geometry import Geometry
from ..geometry import REF_H
from ..geometry import REF_W
from ..layout import wrap_hints


def _review_verb(pending: dict) -> str:
    """The CLI verb that confirms/cancels a pending review run (dedup vs cleanup)."""
    return "cleanup" if pending.get("run_type") == "cleanup-perceptual" else "dedup"


def _empty_snapshot() -> dict:
    """A complete, zeroed ``status_snapshot()``-shaped dict for the daemon-down state.

    Every pure builder indexes ``snap["assets"]`` etc. directly (not ``.get``), so an
    empty ``{}`` crashes the dashboard with ``KeyError``. This is the safe default the
    app renders before the first successful fetch / when the daemon is unreachable —
    the frame draws (all zeros, ``daemon ○ down`` in the header), never crashes."""
    return {
        "assets": 0, "photos": 0, "videos": 0, "trashed": 0,
        "size_bytes": 0, "lifetime_deduped": 0,
        "running": None, "queued": [], "interrupted": [],
        "pending_reviews": [], "roots": [],
    }


def _open_in_explorer(path: str) -> None:
    """Open ``path`` in the OS file manager (the [o] review action, a local op).

    Not a daemon call — the TUI observes/controls jobs but reviewing happens in
    Explorer (§12 "observe-and-control, not a file manager"). Returns None (no job
    id), so the notice just says "submitted"."""
    review = f"{path}\\_packrat_review\\"
    import os
    import subprocess
    if os.name == "nt":
        os.startfile(review)  # type: ignore[attr-defined]
    else:  # dev fallback (non-Windows)
        subprocess.Popen(["xdg-open", review])


# ---------------------------------------------------------------------------
# base screen — one Static showing a fixed W×H frame
# ---------------------------------------------------------------------------
class FrameScreen(Screen):
    """A screen that renders exactly one 100×24 frame from a pure builder.

    Subclasses implement :meth:`frame` (→ the composed string) and declare
    ``BINDINGS``; :meth:`refresh_frame` re-renders. The single ``Static`` is sized
    to the fixed frame by ``packrat.tcss`` (§12 — a fixed root container, not
    auto-sizing widgets).
    """

    def compose(self) -> ComposeResult:
        # markup=False: our frames are pre-composed PLAIN text. Textual's default
        # markup parsing would treat the `[R]`/`[Q]`/`[c]` hint brackets as style
        # tags — consuming them (dropping `[R]` from a title) and bleeding a bad
        # span's background into the footer/border. Color is applied by ROLE, not
        # inline markup (§Theming), so markup must be off here.
        yield Static(id="frame", markup=False)

    def on_mount(self) -> None:
        self.refresh_frame()

    def on_resize(self, event) -> None:
        # Responsive (Level B): the frame is laid out to the live terminal size, so
        # re-render on every resize (each frame() rebuilds Geometry via geo_for()).
        self.refresh_frame()

    # the Geometry the last frame() built (footer-aware); action handlers reuse it
    # so their pagination budgets match what was rendered. Set in every frame().
    _geo: Geometry = Geometry(REF_W, REF_H)

    def _term_size(self) -> tuple[int, int]:
        """Live terminal size, clamped to the ≥100×24 reference minimum."""
        size = self.size
        w = max(REF_W, size.width) if size.width else REF_W
        h = max(REF_H, size.height) if size.height else REF_H
        return w, h

    def geo_for(self, footer: str) -> Geometry:
        """Geometry whose ``content_rows`` accounts for a (possibly wrapping) footer.

        A long hint bar wraps to 2+ lines on a narrow terminal (:func:`wrap_hints`),
        which eats content rows — so pagination budgets must subtract them. Screens
        call this with their footer *before* building the body."""
        w, h = self._term_size()
        rows = len(wrap_hints(footer, (w - 2) - 2))   # content width = (w-2)-2
        return Geometry(w, h, footer_rows=rows)

    def refresh_frame(self) -> None:
        self.current_frame = self.frame()      # PLAIN string (tests / snapshotting)
        # Colorize post-layout (§Theming): the plain frame stays the source of
        # truth; only the live widget gets theme role colors applied by pattern.
        # NSFW masking (--nsfw) runs BEFORE colorize on the same string, so the
        # colorizer's offset math sees the already-redacted (cell-width-preserving)
        # frame; the plain current_frame keeps the true text.
        self.query_one("#frame", Static).update(self._colorize(self._mask(self.current_frame)))

    def _mask(self, frame: str) -> str:
        """Post-layout NSFW redaction **backstop** (runs before colorize when ``--nsfw``).

        The primary masking is *pre-layout* — screens feed the read model through
        ``app.view()`` so a keyword is redacted before :func:`middle_elide` can split it
        across a ``…`` (the elision leak). This pass then re-runs :func:`redact` on the
        composed frame to catch any value that reached the frame WITHOUT going through a
        builder (an inline title, a future call site). On a fully pre-masked frame it
        finds nothing to change. Cell-width-preserving, so the colorizer keeps its
        offsets; identity when ``--nsfw`` is off."""
        reds = getattr(self.app, "redactions", lambda: [])()
        if reds:
            from ..nsfw import redact
            return redact(frame, reds)
        return frame

    #: Whether this screen's ``▸`` marks a selectable LIST ROW (→ bold + brighter-white
    #: emphasis). True for every list screen; a screen whose ``▸`` is a *form field*
    #: marker (:class:`AddRootScreen`) sets this False, since a focused field-cursor at
    #: the row start is indistinguishable from a list cursor by text alone.
    EMPHASIZE_SELECTED_ROW = True

    def _colorize(self, frame: str):
        """Plain frame → colorized Rich ``Text`` (overridable for per-frame effects,
        e.g. the dashboard's animated logo-gem gradient).

        Applies the base theme colors, then (when :attr:`EMPHASIZE_SELECTED_ROW`)
        emphasizes the ``▸``-selected list row (bold + brighter white) — a single place
        so EVERY list screen (roots, queue, merge sources, root-detail jobs) gets the
        same focus emphasis. Overrides call ``super()._colorize(frame)`` to inherit it,
        then layer their own effects."""
        from ..colorize import emphasize_selected_row
        text = colorize(frame)
        if self.EMPHASIZE_SELECTED_ROW:
            emphasize_selected_row(text, frame)
        return text

    def poll_reload(self) -> None:
        """Re-fetch this screen's OWN read-model, if any (called on the poll timer).

        The dashboard/queue read straight off ``app.snapshot`` (refreshed centrally),
        so the base is a no-op. Screens with a per-screen fetch (root detail's
        ``status <root>`` + ``root_jobs``) override this to refresh on the poll instead
        of inside :meth:`frame` — so a keypress re-render doesn't re-hit the daemon."""

    @property
    def is_active(self) -> bool:
        """True only when this screen is the top of the stack.

        Key bindings on a lower screen still fire if a modal on top doesn't handle
        the key (Textual bubbles unhandled keys down the stack). Actions that push
        a screen must guard on this, or a background screen can push a modal while
        another modal is already up — which corrupts the screen stack. Guarding the
        *action* is the driver-safe fix (vs. swallowing keys in the overlay)."""
        try:
            return self.app.screen is self
        except Exception:
            return False

    def frame(self) -> str:  # pragma: no cover - overridden
        return ""

    @property
    def now(self) -> str:
        return self.app.now
