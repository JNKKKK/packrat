"""The AddRootScreen screen (M6, §12) — see :mod:`packrat.tui.frames.base`."""

from __future__ import annotations

from textual.binding import Binding

from ..framing import screen
from ..screens.roots import ADD_ROOT_FIELDS
from ..screens.roots import add_root_body

from .base import FrameScreen


class AddRootScreen(FrameScreen):
    # A form, not a list: its ▸ marks the focused FIELD (and the scan field's marker sits
    # at the row start, where it would otherwise read as a list cursor). Opt out of the
    # selected-row emphasis so a focused field is never bold-highlighted like a list row.
    EMPHASIZE_SELECTED_ROW = False

    BINDINGS = [
        Binding("tab", "next_field", show=False),
        Binding("shift+tab", "prev_field", show=False),
        Binding("space", "toggle", show=False),
        Binding("backspace", "backspace", show=False),
        Binding("enter", "register", show=False),
        Binding("escape", "app.pop_screen", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        # Start blank — the user types the path/name (no pre-filled sample).
        self.path = ""
        self.root_name = ""
        self.kind = "library"     # toggled between library/trash on the Kind field
        self.scan = True          # toggled on the scan field
        self.full = False         # --full re-hash, toggled on the full field
        self.field_idx = 0        # index into ADD_ROOT_FIELDS ([Tab] focus order)

    @property
    def _field(self) -> str:
        return ADD_ROOT_FIELDS[self.field_idx]

    def frame(self) -> str:
        footer = ("[Tab] next field   [Space] toggle   type to edit   "
                  "[Enter] register   Esc cancel")
        geo = self._geo = self.geo_for(footer)
        body = add_root_body(path=self.path, name=self.root_name, kind=self.kind,
                             scan=self.scan, full=self.full, focus_field=self._field, geo=geo)
        return screen("packrat · Roots · add", body, self.app.header_right,
                      footer=footer, width=geo.w, height=geo.h)

    # -- field navigation (§2.2) -------------------------------------------
    def action_next_field(self) -> None:
        self.field_idx = (self.field_idx + 1) % len(ADD_ROOT_FIELDS)
        self.refresh_frame()

    def action_prev_field(self) -> None:
        self.field_idx = (self.field_idx - 1) % len(ADD_ROOT_FIELDS)
        self.refresh_frame()

    def action_toggle(self) -> None:
        """[Space] toggles the focused choice field (Kind radio / scan|full checkbox)."""
        if self._field == "kind":
            self.kind = "trash" if self.kind == "library" else "library"
            self.refresh_frame()
        elif self._field == "scan":
            self.scan = not self.scan
            self.refresh_frame()
        elif self._field == "full":
            self.full = not self.full
            self.refresh_frame()
        # a text field's space is handled by on_key (below), not a toggle.

    def action_backspace(self) -> None:
        if self._field == "path" and self.path:
            self.path = self.path[:-1]
            self.refresh_frame()
        elif self._field == "name" and self.root_name:
            self.root_name = self.root_name[:-1]
            self.refresh_frame()

    def on_key(self, event) -> None:
        """Type into the focused text field (path/name). Bound keys pass through."""
        if self._field not in ("path", "name"):
            return
        ch = event.character
        # only printable single chars; let bindings (tab/enter/esc/backspace) run
        if ch and ch.isprintable() and len(ch) == 1 and event.key not in ("space",):
            self._append(ch)
            event.stop()
        elif event.key == "space" and self._field in ("path", "name"):
            # space is a literal character in a text field (not the toggle binding)
            self._append(" ")
            event.stop()

    def on_paste(self, event) -> None:
        """Paste (Ctrl+V / Ctrl+Shift+V) into the focused text field.

        Textual delivers a clipboard paste as a single ``Paste`` event with the
        whole text (bracketed-paste mode, enabled automatically) — so paste isn't a
        burst of key events and must be handled here, not in ``on_key``."""
        if self._field not in ("path", "name"):
            return
        text = event.text.replace("\r", "").replace("\n", "")   # paths are single-line
        if text:
            self._append(text)
        event.stop()

    def _append(self, text: str) -> None:
        if self._field == "path":
            self.path += text
        else:
            self.root_name += text
        self.refresh_frame()

    def _back(self) -> None:
        """Pop the form back to the Roots interface that opened it.

        Fired via ``run_verb(then=…)`` right after the register toast is posted, so
        pressing [Enter] returns the user to the previous page instead of leaving them
        on a now-submitted form (matching JobCard's back-after-action behavior, §5).
        Guarded on ``is_active`` + a non-empty stack so a bubbled key can't pop the
        wrong screen."""
        if self.is_active and self.app.screen_stack:
            self.app.pop_screen()

    def action_register(self) -> None:
        parts = [f"packrat roots register {self.path}"]
        if self.root_name:
            parts.append(f"--name {self.root_name}")
        # --full only makes sense with a scan of a library root; a trash root is never
        # scanned, so scan/full drop out for it (mirrors the CLI + the form's own note).
        do_scan = self.scan and self.kind == "library"
        if self.kind == "trash":
            parts.append("--kind trash")
        elif self.scan:
            parts.append("--scan")
            if self.full:
                parts.append("--full")
        path, name, kind, full = self.path, self.root_name, self.kind, self.full

        def submit():
            # register_root returns {root, job_id}; report the scan job id if any.
            resp = self.app.client.register_root(
                path, name=name or None, kind=kind,
                scan=do_scan, full=(full and do_scan))
            return resp.get("job_id")

        self.app.run_verb(" ".join(parts), title="register root", submit=submit,
                          then=self._back)
