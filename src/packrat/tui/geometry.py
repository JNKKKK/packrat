"""Responsive geometry — the one place terminal size becomes layout budgets.

Level-B responsive layout (full-terminal, ≥ the 100×24 reference). The design is a
**surplus model**: every width/height budget is ``reference_value + surplus``,
where ``surplus`` is ``w − W`` (extra columns) or ``h − H`` (extra rows) over the
:data:`REFERENCE` size. So at exactly 100×24 every budget equals the original fixed
constant — the frames are byte-identical to the pre-responsive build (all golden
tests still pass) — and on a larger terminal the flexible columns/lists simply grow.

We assume the terminal is **≥ 100×24** (stated constraint), so surplus is never
negative and there is no min-size / column-hiding to handle: growth is uniform.

A :class:`Geometry` is built once per render from the live terminal size and
threaded through the pure screen builders, which read its budgets instead of the
module-level ``tokens`` constants. ``tokens.W/H/CW/COLLECTION_W`` remain the
**reference** (minimum) constants and the ``Geometry`` defaults.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import tokens

# Reference (minimum) size — the frames the mockups + golden tests are pinned to.
REF_W = tokens.W          # 100
REF_H = tokens.H          # 24


@dataclass(frozen=True)
class Geometry:
    """Layout budgets derived from a terminal size ``(w, h)`` (≥ reference).

    All properties reduce to the original fixed constants at ``(100, 24)`` and grow
    linearly with the surplus above it. Widths grow with ``dw = w − 100``; row
    budgets grow with ``dh = h − 24``.
    """

    w: int = REF_W
    h: int = REF_H
    footer_rows: int = 1        # rows the pinned hint bar occupies (wraps → >1)

    # --- surplus over the reference size --------------------------------
    @property
    def dw(self) -> int:
        return max(0, self.w - REF_W)

    @property
    def dh(self) -> int:
        # Height surplus over the reference, adjusted for a multi-row footer: a
        # 2-line footer eats one content row, so the growable surplus shrinks by
        # the extra footer rows (keeps every section's budgets summing to the frame).
        return max(0, self.h - REF_H - (self.footer_rows - 1))

    # --- frame widths ----------------------------------------------------
    @property
    def cw(self) -> int:
        """Content columns inside the outer border (``│ … │``)."""
        return self.w - 2

    @property
    def content_w(self) -> int:
        """Usable text width inside the frame body (``"│ " + text + " │"``)."""
        return self.cw - 2                      # ref: 96

    @property
    def body_rows(self) -> int:
        """Rows between the top and bottom frame borders."""
        return self.h - 2                       # ref: 22

    @property
    def content_rows(self) -> int:
        """Body rows available to content, above the pinned (possibly multi-row) footer."""
        return self.body_rows - self.footer_rows   # ref: 21 (1-row footer)

    # --- dashboard layout (§1) ------------------------------------------
    # The dashboard stacks THREE full-width sections top→bottom:
    #   1. top: hjoin(logo, collection box) — logo left, collection right
    #   2. roots box (full width)
    #   3. queue box (full width)
    @property
    def collection_w(self) -> int:
        """The Collection stats box — fixed width (its content is fixed-width)."""
        return tokens.COLLECTION_W              # 29

    @property
    def logo_w(self) -> int:
        """The logo panel width (left of the Collection box in the top section)."""
        return self.content_w - self.collection_w - 1   # ref: 66

    # Top section is a fixed height: the Collection box is 6 stats (assets/photos/
    # videos/size/trashed/deduped) + 2 borders = 8 rows, and the logo is padded to
    # match. It does not grow with height.
    TOP_ROWS = 8

    @property
    def roots_w(self) -> int:
        """The dashboard Roots box width (full content width now)."""
        return self.content_w                   # ref: 96

    @property
    def queue_w(self) -> int:
        """The dashboard/maximized Queue box width (full content width)."""
        return self.content_w                   # ref: 96

    @property
    def row_w_compact(self) -> int:
        """Width of a dashboard RootRow (inside the full-width Roots box)."""
        return self.roots_w - 4                 # box borders + padding

    @property
    def queue_row_w(self) -> int:
        """Width of a dashboard queue-preview row (inside the Queue box)."""
        return self.queue_w - 4

    # --- dashboard section heights --------------------------------------
    # Top is fixed (TOP_ROWS=7). Below it the roots + queue boxes split the rest.
    # Overhead: roots box = 2 borders + 1 DOTKEY line = 3; queue box = 2 borders.
    # So the two list interiors sum to content_rows − TOP_ROWS − 5.
    @property
    def _dash_split(self) -> int:
        return max(2, self.content_rows - self.TOP_ROWS - 5)   # interiors combined; ref 9

    @property
    def dash_roots_rows(self) -> int:
        return (self._dash_split + 1) // 2       # ref 5 (gets the odd row)

    @property
    def dash_queue_rows(self) -> int:
        return self._dash_split - self.dash_roots_rows   # ref 4

    # --- maximized-list row budgets ------------------------------------
    @property
    def roots_list_rows(self) -> int:
        # §2.1 fills the frame: content_rows − header − (legend+pager line) − rule.
        return self.content_rows - 3             # ref 18

    @property
    def jobs_rows(self) -> int:
        return 4 + self.dh                       # §3 root-detail Jobs list (ref 4)

    # §4 queue: running(2) + blank(1) + queued-header(1) + queued_rows + blank(1)
    # + recent-header(1) + recent_rows == content_rows. So the two windows split
    # content_rows − 6 (pagers moved onto the header lines, no standalone rows).
    @property
    def _queue_split(self) -> int:
        return max(2, self.content_rows - 6)      # ref 15

    @property
    def queued_rows(self) -> int:
        return (self._queue_split + 1) // 2       # ref 8 (gets the odd row)

    @property
    def recent_rows(self) -> int:
        return self._queue_split - self.queued_rows   # ref 7


REFERENCE = Geometry(REF_W, REF_H)
