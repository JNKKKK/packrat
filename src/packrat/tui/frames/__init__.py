"""TUI screen controllers (M6, §12) — the Textual screen classes.

Split by screen (one module each) over a shared :mod:`~packrat.tui.frames.base`
(:class:`FrameScreen` + the ``_review_verb``/``_empty_snapshot``/``_open_in_explorer``
helpers). Re-exported here so callers keep importing from ``packrat.tui.frames``.

These own only what Textual is for (key routing, focus, the screen stack); all
geometry/text lives in the pure, no-Textual ``screens``/``render``/``layout`` layer.
They reach live state + actions through ``self.app`` (the PackratApp), so this package
is imported *by* ``app`` — the back-reference is a runtime attribute, never an import.
"""

from __future__ import annotations

from .base import FrameScreen, _empty_snapshot, _open_in_explorer, _review_verb
from .addroot import AddRootScreen
from .dashboard import Dashboard
from .jobcard import JobCard
from .mergepicker import MergePickerScreen
from .queuemax import QueueMax
from .rootdetail import RootDetailScreen
from .rootsmax import RootsMax

__all__ = [
    "FrameScreen", "Dashboard", "RootsMax", "AddRootScreen", "MergePickerScreen",
    "RootDetailScreen", "QueueMax", "JobCard",
    "_review_verb", "_empty_snapshot", "_open_in_explorer",
]
