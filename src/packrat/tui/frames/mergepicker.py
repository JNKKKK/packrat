"""The MergePickerScreen screen (M6, §12) — see :mod:`packrat.tui.frames.base`."""

from __future__ import annotations

from textual.binding import Binding

from ..framing import screen
from ..screens.merge import merge_body
from ..screens.merge import merge_sources
from ..screens.merge import source_list_rows

from .base import FrameScreen


# ---------------------------------------------------------------------------
# Merge-from picker (§3.3)
# ---------------------------------------------------------------------------
class MergePickerScreen(FrameScreen):
    """Pick a merge SOURCE for a fixed destination root (§3.3).

    ``[Tab]`` toggles the source between a paginated **registered-root** list
    (library roots, dest excluded) and a typed **external folder** path; ``↑/↓``
    picks a root, ``←/→`` pages it, ``[Space]`` toggles ``--dry-run``, typing edits
    the external path, ``[Enter]`` submits ``merge <source> --into <dest>``.
    """

    BINDINGS = [
        Binding("tab", "toggle_source", show=False),
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("left", "page(-1)", show=False),
        Binding("right", "page(1)", show=False),
        Binding("ctrl+d", "toggle_dry_run", show=False),   # both modes; Space types in ext
        Binding("backspace", "backspace", show=False),
        Binding("enter", "merge", show=False),
        Binding("escape", "app.pop_screen", show=False),
    ]

    def __init__(self, dest: dict) -> None:
        super().__init__()
        self.dest = dest
        self.source_mode = "root"     # 'root' | 'ext'
        self.cursor = 0
        self.page = 0
        self.ext_path = ""
        self.dry_run = False

    def _sources(self) -> list[dict]:
        return merge_sources(self.app.snapshot.get("roots", []), self.dest["name"])

    FOOTER_ROOT = ("↑/↓ pick   ←/→ page   [Tab] switch source   "
                   "[Ctrl-D] --dry-run   [Enter] merge   Esc")
    FOOTER_EXT = ("type to edit path   [Tab] switch source   "
                  "[Ctrl-D] --dry-run   [Enter] merge   Esc")

    def frame(self) -> str:
        footer = self.FOOTER_ROOT if self.source_mode == "root" else self.FOOTER_EXT
        geo = self._geo = self.geo_for(footer)
        # DISPLAY masking (dest + source roots) before layout; self.dest stays raw for
        # the merge submit (action_merge). ext_path is the user's own live input — left
        # verbatim so they can see what they're typing.
        dest = self.app.view(self.dest)
        body = merge_body(dest, self.app.view(self._sources()), geo=geo,
                          source_mode=self.source_mode, cursor=self.cursor,
                          page=self.page, ext_path=self.ext_path, dry_run=self.dry_run)
        right = f"{dest['path']} · {dest['kind']}"
        return screen(f"packrat · {dest['name']} · merge from", body, right,
                      footer=footer, width=geo.w, height=geo.h)

    # -- navigation --------------------------------------------------------
    def action_toggle_source(self) -> None:
        self.source_mode = "ext" if self.source_mode == "root" else "root"
        self.refresh_frame()

    def action_move(self, delta: int) -> None:
        if self.source_mode != "root":
            return
        n = len(self._sources())
        self.cursor = max(0, min(self.cursor + delta, n - 1)) if n else 0
        self.page = self.cursor // source_list_rows(self._geo)
        self.refresh_frame()

    def action_page(self, delta: int) -> None:
        if self.source_mode != "root":
            return
        rows = source_list_rows(self._geo)
        n = len(self._sources())
        pages = max(1, -(-n // rows))
        new = max(0, min(self.page + delta, pages - 1))
        if new != self.page:                       # cursor → first item of new page
            self.page = new
            self.cursor = min(new * rows, max(0, n - 1))
        self.refresh_frame()

    def action_toggle_dry_run(self) -> None:
        self.dry_run = not self.dry_run
        self.refresh_frame()

    def action_backspace(self) -> None:
        if self.source_mode == "ext" and self.ext_path:
            self.ext_path = self.ext_path[:-1]
            self.refresh_frame()

    def on_key(self, event) -> None:
        """Type into the external-path field (path mode only). Bound keys pass through."""
        if self.source_mode != "ext":
            return
        ch = event.character
        if ch and ch.isprintable() and len(ch) == 1 and event.key != "space":
            self.ext_path += ch
            self.refresh_frame()
            event.stop()
        elif event.key == "space":
            # In the ext field, Space is a literal char, NOT the dry-run toggle.
            self.ext_path += " "
            self.refresh_frame()
            event.stop()

    def on_paste(self, event) -> None:
        """Paste (Ctrl+V / Ctrl+Shift+V) a path into the external-folder field.

        A clipboard paste is one ``Paste`` event with the whole text (not key
        bursts) — the common way to enter a long path. Path mode only."""
        if self.source_mode != "ext":
            return
        text = event.text.replace("\r", "").replace("\n", "")
        if text:
            self.ext_path += text
            self.refresh_frame()
        event.stop()

    def action_merge(self) -> None:
        dest = self.dest["name"]
        if self.source_mode == "root":
            sources = self._sources()
            if not sources:
                return
            src = sources[self.cursor]
            src_disp, src_arg = src["name"], src["path"]
        else:
            if not self.ext_path.strip():
                return
            src_disp = src_arg = self.ext_path.strip()
        dry = " --dry-run" if self.dry_run else ""
        cmd = f"packrat merge {src_disp} --into {dest}{dry}"
        self.app.run_verb(
            cmd, title="merge",
            submit=lambda: self.app.client.submit_merge(src_arg, dest, dry_run=self.dry_run))
