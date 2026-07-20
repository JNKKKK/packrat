"""packrat TUI (M6, §12) — the default face of the tool.

A Textual app that renders the daemon's read-model snapshots + SSE progress
stream into a full-terminal set of interfaces (§12). It adds no backend and issues
no privileged operation of its own: every action maps to an existing CLI verb /
daemon endpoint (design tenet §1.6).

Layered as a **pure render core + thin Textual widgets**:
- :mod:`packrat.tui.tokens`    — pure values (sizes, glyphs, color roles, Theme).
- :mod:`packrat.tui.layout`    — pure text-grid helpers (``row``/``fit``/elide, CJK-aware).
- :mod:`packrat.tui.geometry`  — terminal size → responsive layout budgets.
- :mod:`packrat.tui.framing`   — pure frame/panel/box composition.
- :mod:`packrat.tui.render`    — pure ``dict → line`` content renderers.
- :mod:`packrat.tui.data`      — the ``DataSource`` liveness seam (queries+SSE+poll).
- :mod:`packrat.tui.nav`       — focus→maximize state machine + action declarations.
- ``screens`` / ``modals`` / ``app`` — the Textual widgets + screens.

The pure layers import **without** Textual, so they render headless (and are
golden-frame testable as plain strings).
"""

from __future__ import annotations
