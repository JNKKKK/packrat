"""Navigation & focus — the packrat-specific state machines (component-plan §Nav).

Textual gives the primitives (``Screen``, ``push_screen``/``pop_screen``, focus);
this module owns the two behaviors Textual can't know about, kept as **pure,
testable** helpers so a screen just drives them:

- :class:`DashboardFocus` — the dashboard's focus→maximize state machine (§focus
  model): ``[r]``/``[q]`` once focuses a box (heavy frame + cursor), again
  maximizes into the full §2/§4 interface; ``Esc`` un-focuses; ``↑/↓`` move the
  cursor in place; ``←/→`` page. The two boxes are peers — focusing one unfocuses
  the other.
- :class:`ActionSet` — a screen's footer actions as ``(key, label, handler,
  disabled)`` declarations; the HintBar renders the labels from the *same* list so
  the hint bar can never drift from the real bindings (§1.6: every handler maps to
  a CLI verb; a deferred verb is ``disabled`` → shown greyed, not bound).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Focus states for the dashboard boxes.
UNFOCUSED = "unfocused"
FOCUSED = "focused"


@dataclass
class DashboardFocus:
    """The dashboard focus→maximize state machine (§focus model).

    ``target`` is the focused box (``None`` | ``'roots'`` | ``'queue'``); a box is
    *focused* when it is ``target``. A second press of its key requests
    *maximize* (the screen pushes §2/§4). Cursors track the selected row per box.
    """

    target: str | None = None
    roots_cursor: int = 0
    queue_cursor: int = 0
    roots_len: int = 0
    queue_len: int = 0

    def press(self, key: str) -> str | None:
        """Handle ``[r]``/``[q]``. Returns ``'maximize:<box>'`` when it should
        maximize (second press on the already-focused box), else ``None``."""
        box = {"r": "roots", "q": "queue"}.get(key)
        if box is None:
            return None
        if self.target == box:
            return f"maximize:{box}"      # second press → maximize
        self.target = box                 # focus (unfocuses the peer)
        return None

    def escape(self) -> bool:
        """``Esc``: un-focus. Returns True if it consumed the key (was focused)."""
        if self.target is not None:
            self.target = None
            return True
        return False

    def move(self, delta: int) -> None:
        """``↑/↓`` within the focused box — move its cursor, clamped in range."""
        if self.target == "roots":
            self.roots_cursor = _clamp(self.roots_cursor + delta, self.roots_len)
        elif self.target == "queue":
            self.queue_cursor = _clamp(self.queue_cursor + delta, self.queue_len)

    @property
    def focused(self) -> bool:
        return self.target is not None


def _clamp(i: int, length: int) -> int:
    if length <= 0:
        return 0
    return max(0, min(i, length - 1))


@dataclass
class Action:
    """One footer action: a key, its hint label, a handler name, and disabled flag.

    ``handler`` names the CLI verb / screen transition it triggers (§1.6 — no
    TUI-only action). ``disabled`` renders the hint greyed and skips the binding
    (a deferred verb, e.g. unregister/rename — §14 #9).
    """

    key: str
    label: str
    handler: str
    disabled: bool = False


@dataclass
class ActionSet:
    """A screen's declared actions; the single source for both bindings and hints."""

    actions: list[Action] = field(default_factory=list)

    def hint_bar(self) -> str:
        """Render the footer hint string from the actions (labels only).

        A disabled action is shown but visually de-emphasized by the caller (the
        pure string keeps the text; color/dim is applied at render, §Theming).
        """
        return "   ".join(a.label for a in self.actions)

    def active_bindings(self) -> list[Action]:
        """The actions that are actually bound (not deferred)."""
        return [a for a in self.actions if not a.disabled]
