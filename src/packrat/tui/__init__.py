"""packrat TUI (M6, §12) — the default face of the tool.

A Textual app that renders the daemon's read-model snapshots + SSE progress
stream into the fixed 100×24 interfaces designed in ``docs/M6-tui-mockups.md``.
It adds no backend and issues no privileged operation of its own: every action
maps to an existing CLI verb / daemon endpoint (design tenet §1.6).

Layered per ``docs/M6-component-plan.md``:
- :mod:`packrat.tui.tokens`   — pure values (sizes, glyphs, color roles, Theme).
- :mod:`packrat.tui.layout`   — pure text-grid helpers (``row``/``fit``/elide).
- :mod:`packrat.tui.data`     — the ``DataSource`` liveness seam (queries+SSE+poll).
- :mod:`packrat.tui.nav`      — screen stack + focus→maximize state machine.
- ``components`` / ``screens`` / ``modals`` — the Textual widgets.

The pure layers (``tokens``/``layout``) import **without** Textual, so the mockup
generator reuses them headless (component-plan Resolved #1/#2).
"""

from __future__ import annotations
