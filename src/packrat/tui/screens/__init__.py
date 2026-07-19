"""Screen body builders (§1–§5) — pure ``dict → list[str]`` frame content.

Each module builds one interface's body lines from the read-model dicts; the
Textual screens in :mod:`packrat.tui.app` display them in the fixed frame. Kept
pure so the frames are golden-testable without a Textual pilot.
"""

from __future__ import annotations
