"""The RootsMax screen (M6, §12) — see :mod:`packrat.tui.frames.base`."""

from __future__ import annotations

from textual.binding import Binding

from ..framing import screen
from ..screens.roots import roots_body

from .base import FrameScreen
from .addroot import AddRootScreen


# ---------------------------------------------------------------------------
# Roots interface (§2)
# ---------------------------------------------------------------------------
class RootsMax(FrameScreen):
    BINDINGS = [
        Binding("s", "sort", "sort", show=False),
        Binding("a", "add", "add root", show=False),
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("left", "page(-1)", show=False),
        Binding("right", "page(1)", show=False),
        Binding("enter", "open", show=False),
        Binding("escape", "app.pop_screen", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sort_mode = 0
        self.cursor = 0
        self.page = 0

    def _ordered(self) -> list[dict]:
        from .. import render
        return render.sort_roots(self.app.snapshot.get("roots", []), self.sort_mode)

    FOOTER = ("↑/↓ select   [Enter] open detail   ←/→ page   "
              "[s] sort   [a] add root   Esc back")

    def frame(self) -> str:
        geo = self._geo = self.geo_for(self.FOOTER)
        body = roots_body(self.app.view(self.app.snapshot.get("roots", [])), now=self.now,
                          geo=geo, sort_mode=self.sort_mode, cursor=self.cursor, page=self.page)
        return screen("packrat · Roots", body, self.app.header_right,
                      footer=self.FOOTER, width=geo.w, height=geo.h)

    def _colorize(self, frame: str):
        # ◉ is green (deduped) OR yellow (need-dedup) — recolor each list row's dot to its
        # true role after the base pass (the glyph pass can't split one glyph two ways).
        # Uses the SAME sorted+masked roots roots_body rendered (§12 4-state dot).
        from .. import render
        from ..colorize import recolor_dot_legend, recolor_root_dots
        text = super()._colorize(frame)
        roots = render.sort_roots(self.app.view(self.app.snapshot.get("roots", [])),
                                  self.sort_mode)
        recolor_root_dots(text, frame, roots)
        recolor_dot_legend(text, frame)
        return text

    def action_sort(self) -> None:
        self.sort_mode = (self.sort_mode + 1) % 4
        self.cursor = 0
        self.page = 0
        self.refresh_frame()

    def action_move(self, delta: int) -> None:
        n = len(self._ordered())
        self.cursor = max(0, min(self.cursor + delta, n - 1)) if n else 0
        self.page = self.cursor // self._geo.roots_list_rows    # keep the cursor on-page
        self.refresh_frame()

    def action_page(self, delta: int) -> None:
        rows = self._geo.roots_list_rows
        n = len(self._ordered())
        pages = max(1, -(-n // rows))
        new = max(0, min(self.page + delta, pages - 1))
        if new != self.page:                       # move cursor to the new page's first item
            self.page = new
            self.cursor = min(new * rows, max(0, n - 1))
        self.refresh_frame()

    def action_add(self) -> None:
        self.app.push_screen(AddRootScreen())

    def action_open(self) -> None:
        roots = self._ordered()
        if roots:
            self.app.open_root(roots[self.cursor]["name"])
