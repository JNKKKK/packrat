"""Pure frame/panel composition — the chrome vocabulary (component-plan §Chrome).

Extracted verbatim from ``docs/_tui_mockup_gen.py`` so the doc frames and the
runtime widgets share **one** set of primitives (component-plan Why-build-it #2):
``box`` (a bordered Panel — light or heavy), ``hjoin`` (side-by-side panels),
and ``screen`` (the fixed W×H :class:`AppFrame`, footer pinned to the bottom).

All pure and colorless (they return plain strings / line lists), building on
:mod:`packrat.tui.layout` (``fit_width`` == the generator's ``pad``) and the
box glyphs + ``W``/``H``/``CW`` from :mod:`packrat.tui.tokens`. The Textual
``Panel``/``AppFrame`` widgets wrap these by delegation — they never re-derive
the box math (component-plan Resolved #2).
"""

from __future__ import annotations

from .layout import fit_width as pad
from .tokens import CW, H, HEAVY_BOX, LIGHT_BOX


def box(title: str, lines: list[str], width: int, right: str = "", heavy: bool = False) -> list[str]:
    """A bordered panel ``width`` cells wide (heavy frame = focused, §focus model).

    ``title`` sits in the top border (``┌─ [R]oots ─┐``); ``right`` is an optional
    right-aligned top-border label (a paginator in a focused box, §1.3). Body
    ``lines`` are padded to the inner width. Returns the list of rows.
    """
    tl, tr, bl, br, h, v = HEAVY_BOX if heavy else LIGHT_BOX
    lt = f"{h} {title} "
    rt = f" {right} {h}" if right else h
    core = lt + h * max(0, (width - 2 - len(lt) - len(rt))) + rt
    rows = [tl + pad(core, width - 2) + tr]
    for ln in lines:
        rows.append(v + " " + pad(ln, width - 4) + " " + v)
    rows.append(bl + h * (width - 2) + br)
    return rows


def hjoin(a: list[str], b: list[str], gap: int = 1) -> list[str]:
    """Place two panel blocks side by side (Collection + Roots on the dashboard)."""
    hgt = max(len(a), len(b))
    wa, wb = len(a[0]), len(b[0])
    a = a + [" " * wa] * (hgt - len(a))
    b = b + [" " * wb] * (hgt - len(b))
    return [a[i] + " " * gap + b[i] for i in range(hgt)]


def screen(title: str, content: list[str], right: str = "", footer: str = "") -> str:
    """Wrap ``content`` in the fixed W×H frame — the :class:`AppFrame` (§12).

    ``title`` is the top-border label (``packrat · Roots``); ``right`` its
    right-aligned counterpart (``daemon ● up``). A ``footer`` (the HintBar) is
    pinned to the **last** body row, with blank filler *above* it, so content sits
    at the top and hints at the bottom. Result is exactly H lines of W cells each.
    """
    lt = f"─ {title} "
    rt = f" {right} ─" if right else "─"
    top = "┌" + pad(lt + "─" * max(0, (CW - len(lt) - len(rt))) + rt, CW) + "┐"
    rows = H - 2  # available body rows
    inner = list(content)
    if footer:
        inner = inner[: rows - 1]                 # leave the last row for the footer
        inner += [""] * (rows - 1 - len(inner))   # fill the gap ABOVE the footer
        inner.append(footer)                      # pinned to the bottom row
    else:
        inner = (inner + [""] * rows)[:rows]
    body = ["│ " + pad(ln, CW - 2) + " │" for ln in inner]
    return "\n".join([top] + body + ["└" + "─" * CW + "┘"])
