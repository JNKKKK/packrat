"""packrat TUI — the default face of the tool.

A Textual app that renders the daemon's read-model snapshots + SSE progress
stream into a full-terminal set of interfaces. It adds no backend and issues
no privileged operation of its own: every action maps to an existing CLI verb /
daemon endpoint.

Layered as a **pure render core + thin Textual widgets**:
- :mod:`packrat.tui.tokens`    — pure values (sizes, glyphs, color roles, Theme).
- :mod:`packrat.tui.layout`    — pure text-grid helpers (``row``/``fit``/elide, CJK-aware).
- :mod:`packrat.tui.geometry`  — terminal size → responsive layout budgets.
- :mod:`packrat.tui.framing`   — pure frame/panel/box composition.
- :mod:`packrat.tui.render`    — pure ``dict → line`` content renderers.
- :mod:`packrat.tui.data`      — pure relative-time + TUI-side ETA helpers.
- :mod:`packrat.tui.nav`       — focus→maximize state machine.
- ``screens`` (pure ``dict → line`` builders) / ``modals`` / ``frames`` (a package of
  Textual screen classes, one module per screen over a shared ``frames.base``) /
  ``app`` (the :class:`~packrat.tui.app.PackratApp` + entrypoint).

The pure layers (everything except ``modals`` / ``frames`` / ``app``) import
**without** Textual, so they render headless (and are golden-frame testable as plain
strings).
"""

from __future__ import annotations
