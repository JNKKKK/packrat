"""Guard: the generated mockup frames stay in sync with the checked-in doc.

``docs/_tui_mockup_gen.py`` renders every M6 interface into the fixed 100×24 frame
and now pulls its constants + grid helpers from :mod:`packrat.tui.tokens` /
:mod:`packrat.tui.layout` (component-plan Resolved #1/#2). Two guarantees this test
locks in:

1. **Every generated frame is exactly 100 columns wide and inside a 24-row block.**
   This is §12's "fixed layout" made mechanical — if a future edit widens the
   window, this fails.
2. **The generator's output matches the code fences in ``docs/M6-tui-mockups.md``.**
   So regenerating is required after any change, and the doc can't silently drift
   from the shared tokens.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GEN = REPO / "docs" / "_tui_mockup_gen.py"
DOC = REPO / "docs" / "M6-tui-mockups.md"

from packrat.tui.tokens import H, W  # noqa: E402


def _run_generator() -> str:
    proc = subprocess.run(
        [sys.executable, str(GEN)],
        capture_output=True,
        cwd=str(REPO),
        check=True,
    )
    return proc.stdout.decode("utf-8").replace("\r\n", "\n")


def _fenced_blocks(text: str) -> list[list[str]]:
    """Return the body lines of every ``` fenced block in `text`."""
    blocks = []
    cur: list[str] | None = None
    for line in text.split("\n"):
        if line.strip() == "```":
            if cur is None:
                cur = []
            else:
                blocks.append(cur)
                cur = None
        elif cur is not None:
            cur.append(line)
    return blocks


def test_generator_runs_clean():
    out = _run_generator()
    assert out.strip(), "generator produced no output"


def test_every_frame_is_fixed_width():
    """Each rendered frame line is exactly W cells; each frame is exactly H rows.

    This is the golden §12 invariant: navigating never widens/heightens the frame.
    """
    for block in _fenced_blocks(_run_generator()):
        # A frame is the H-row box drawn by screen(); the doc stacks one per fence.
        assert len(block) == H, f"frame has {len(block)} rows, expected {H}"
        for line in block:
            assert len(line) == W, f"line width {len(line)} != {W}: {line!r}"


def test_generated_frames_match_doc():
    """The doc's fenced frames equal the generator output (regenerate if this fails)."""
    generated = _fenced_blocks(_run_generator())
    doc_blocks = _fenced_blocks(DOC.read_text(encoding="utf-8"))
    # The doc has prose + fenced frames; every generated frame must appear in order.
    assert doc_blocks == generated, (
        "docs/M6-tui-mockups.md is out of sync with the generator — "
        "run `uv run python docs/_tui_mockup_gen.py` and paste the frames."
    )


def test_frames_use_shared_glyphs():
    """Sanity: the shared status-dot glyphs actually appear in the rendered frames."""
    out = _run_generator()
    for glyph in ("◉", "◐", "○", "▸", "▶", "⚠"):
        assert glyph in out, f"glyph {glyph!r} missing from generated frames"
    assert re.search(r"page \d+/\d+", out), "paginator missing from frames"
