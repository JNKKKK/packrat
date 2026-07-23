"""Modals & overlays (component-plan §Modals) — a reusable centered inset.

``Modal`` is a Textual :class:`~textual.screen.ModalScreen` pushed onto the same
screen stack, so it layers over the current screen, ``Esc`` pops it back to exactly
where you were, and the parent keeps its state. It honors the fixed frame (§12): a
centered bordered inset over a dimmed backdrop, never resizing the 100×24 canvas.

Three typed variants compose the same pure builders (no new rendering machinery):
- :class:`ConfirmModal` — a message + ``[y]/[n]`` (or a typed-count field for the §6
  delete-set confirm, where the network permanent-delete warning shows). Returns a
  bool via the screen-dismiss result.
- :class:`MessageModal` — a dismissable notice (a ``RootError``, a transient
  "daemon unreachable"). ``[Enter]``/``Esc`` closes.
- :class:`ChoiceModal` — a small pick-list for a quick choice.

Result flows back by Textual's screen-dismiss (``push_screen(..., callback)``), so a
modal that gates a CLI verb (typed-count confirm → ``cleanup … --confirm``) stays a
linear "ask, then act" flow and the §1.6 rule holds (the modal only gathers input).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from .colorize import colorize
from .framing import box
from .layout import fit, wrap_cells

# Modal inset size — a centered panel well inside the 100×24 frame (§12).
MODAL_W = 60
MODAL_H = 11


def modal_lines(title: str, message: str, footer: str, *, extra: list[str] | None = None) -> list[str]:
    """Compose a modal's bordered inset (pure) — wraps the message within the inset."""
    body = wrap_cells(message, MODAL_W - 4)
    if extra:
        body += [""] + extra
    body += [""]
    fitted = fit(body + [footer], MODAL_H - 2, mode="clip")
    return box(title, fitted.rows, MODAL_W)


def trash_refresh_modal_lines(root_name: str, footer: str) -> list[str]:
    """Compose the trash-root refresh modal's inset (pure): mascot on top, prompt below.

    The packrat-with-a-trash-can mascot (:func:`render.trash_refresh_mascot_lines`)
    sits at the top; beneath it, the question naming ``root_name``, then the footer
    pinned to the bottom row. Same 60×11 inset as :func:`modal_lines`."""
    from . import render
    from .layout import middle_elide
    inner_w = MODAL_W - 4
    mascot = render.trash_refresh_mascot_lines(root_name, width=inner_w)
    # Keep the prompt to ONE line so the footer always survives the clip: the mascot
    # (5) + blank + prompt (1) + blank + footer (1) = 8 ≤ MODAL_H-2 (9). Middle-elide a
    # long NAS root name so its drive + leaf stay legible (§12 path rule).
    prompt = middle_elide(f"Absorb + empty {root_name} now?", inner_w)
    body = mascot + ["", prompt, ""]
    fitted = fit(body + [footer], MODAL_H - 2, mode="clip")
    return box("refresh trash?", fitted.rows, MODAL_W)


class Modal(ModalScreen):
    """Base overlay: a centered inset within the fixed frame, ``Esc`` to close.

    Subclasses set :attr:`title`, :attr:`message`, :attr:`footer` and (optionally)
    override :meth:`extra_lines`; the base renders the bordered inset and dims the
    backdrop (via ``packrat.tcss``). ``Esc`` dismisses with the default result.
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]
    title = "packrat"
    message = ""
    footer = "Esc close"

    def _render_masked(self, lines: list[str]):
        """Compose modal ``lines`` → colorized Rich ``Text``, NSFW-masked when ``--nsfw``.

        Modals name the root (``clean up <root>``, ``Delete … in <root>?``), so the
        redaction must reach them too — the app's ``(value, masked)`` pairs are applied
        (:func:`packrat.tui.nsfw.redact`) on the joined string just before colorize
        (cell-width-preserving, so the colorizer's offsets hold). A no-op when ``--nsfw``
        is off. (Named ``_render_masked``, not ``_render`` — the latter is a Textual
        ``Widget`` internal.)"""
        frame = "\n".join(lines)
        reds = getattr(self.app, "redactions", lambda: [])()
        if reds:
            from .nsfw import redact
            frame = redact(frame, reds)
        return colorize(frame)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal"):
            # markup=False — pre-composed plain text; brackets are literal (see
            # FrameScreen.compose for why markup parsing corrupts the frame).
            # colorize applies theme role colors post-layout (§Theming).
            yield Static(self._render_masked(modal_lines(
                self.title, self.message, self.footer, extra=self.extra_lines())),
                id="modal-frame", markup=False)

    def extra_lines(self) -> list[str]:
        return []

    def action_cancel(self) -> None:
        self.dismiss(self.default_result())

    def default_result(self):
        return None


class MessageModal(Modal):
    """A dismissable notice (a RootError, a transient error). Enter/Esc closes."""

    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("enter", "cancel", show=False),
    ]

    def __init__(self, message: str, *, title: str = "packrat", footer: str = "[Enter] ok"):
        super().__init__()
        self.message = message
        self.title = title
        self.footer = footer


class ConfirmModal(Modal):
    """A yes/no confirm, or a typed-count confirm for the §6 delete-set gate.

    ``count`` (when set) makes it a typed-count modal: the user types the exact
    number to confirm (the network permanent-delete warning shows in ``extra``);
    dismisses ``True`` only on an exact match. Otherwise ``[y]``/``[n]`` → bool.
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("y", "yes", show=False),
        Binding("n", "cancel", show=False),
        Binding("enter", "submit", show=False),
    ]

    def __init__(self, message: str, *, title: str = "confirm", count: int | None = None,
                 network: int = 0):
        super().__init__()
        self.message = message
        self.title = title
        self.count = count
        self.network = network
        self.footer = (f"type the count ({count}) then [Enter]   Esc cancel"
                       if count is not None else "[y] yes   [n] no   Esc cancel")

    def extra_lines(self) -> list[str]:
        lines = []
        if self.network:
            lines.append(f"⚠ {self.network} on a network share — deleted PERMANENTLY (no Recycle Bin).")
        return lines

    def compose(self) -> ComposeResult:
        yield from super().compose()
        if self.count is not None:
            yield Input(placeholder=str(self.count), id="count-input")

    def on_mount(self) -> None:
        # Focus the count field so typing lands there immediately (the count-confirm
        # is a blocking, deliberate action — the user types the number).
        if self.count is not None:
            self.query_one("#count-input", Input).focus()

    def on_input_submitted(self, event: "Input.Submitted") -> None:
        # A focused Input consumes Enter (posts Submitted) instead of bubbling to
        # the screen binding, so resolve the typed-count result from here.
        self._resolve_count(event.value)

    def action_yes(self) -> None:
        if self.count is None:
            self.dismiss(True)

    def action_submit(self) -> None:
        if self.count is None:
            self.dismiss(True)
            return
        try:
            val = self.query_one("#count-input", Input).value
        except Exception:
            val = ""
        self._resolve_count(val)

    def _resolve_count(self, value: str) -> None:
        self.dismiss(value.strip() == str(self.count))

    def default_result(self):
        return False


class TrashRefreshModal(Modal):
    r"""The trash-root confirm: a packrat-with-a-trash-can mascot + ``[y]/[n]`` (§6.1).

    Shown when the user picks a **trash** root (Dashboard roots box / RootsMax) —
    a trash root has no detail screen, its only action is *refresh the collection*.
    Dismisses ``True`` on ``[y]``/``[Enter]`` (→ ``packrat trash refresh <root>``),
    ``False`` on ``[n]``/``Esc``. A bool result, like :class:`ConfirmModal`, so it
    slots into the app's confirm→act flow.
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("y", "yes", show=False),
        Binding("n", "cancel", show=False),
        Binding("enter", "yes", show=False),
    ]

    def __init__(self, root_name: str):
        super().__init__()
        self.root_name = root_name
        self.footer = "[y] yes   [n] no   Esc cancel"

    def compose(self) -> ComposeResult:
        with Vertical(id="modal"):
            yield Static(
                self._render_masked(trash_refresh_modal_lines(self.root_name, self.footer)),
                id="modal-frame", markup=False)

    def action_yes(self) -> None:
        self.dismiss(True)

    def default_result(self):
        return False


class ChoiceModal(Modal):
    """A small pick-list for a quick choice (a lightweight MergePicker sibling).

    ``prompt`` (optional) is a question line rendered above the options (wrapped to the
    inset), so the box can pose "which…?" rather than relying on the short title alone.
    The cursor starts on option 0, so a plain [Enter] takes the first (default) choice.
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("enter", "choose", show=False),
    ]

    def __init__(self, options: list[str], *, title: str = "choose", prompt: str = ""):
        super().__init__()
        self.options = options
        self.title = title
        self.prompt = prompt
        self.cursor = 0
        self.footer = "↑/↓ select   [Enter] choose   Esc cancel"

    def _option_lines(self) -> list[str]:
        from .tokens import CURSOR
        return [f"{CURSOR if i == self.cursor else ' '} {opt}"
                for i, opt in enumerate(self.options)]

    @property
    def message(self) -> str:
        # With a prompt, the message IS the prompt (wrapped by modal_lines) and the
        # options render as extra_lines below it; without one, the options are the message
        # (the original lightweight-picker behavior).
        return self.prompt if self.prompt else "\n".join(self._option_lines())

    @message.setter
    def message(self, _value):  # base __init__ sets message="" — ignore, we compute it
        pass

    def extra_lines(self) -> list[str]:
        return self._option_lines() if self.prompt else []

    def action_move(self, delta: int) -> None:
        self.cursor = max(0, min(self.cursor + delta, len(self.options) - 1))
        self.query_one("#modal-frame", Static).update(
            self._render_masked(modal_lines(self.title, self.message, self.footer,
                                            extra=self.extra_lines())))

    def action_choose(self) -> None:
        self.dismiss(self.cursor)

    def default_result(self):
        return None
