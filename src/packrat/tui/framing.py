"""Pure frame/panel composition — the chrome vocabulary.

The primitives the whole TUI shares: ``box`` (a bordered panel — light or heavy),
``hjoin`` (side-by-side panels), and ``screen`` (the outer W×H app frame with the
title bar + a bottom-pinned footer). ``screen`` middle-elides an over-long right
label (e.g. a root path) and measures by display cells so CJK titles stay aligned.

All pure and colorless (they return plain strings / line lists), building on
:mod:`packrat.tui.layout` (``fit_width``/``cell_width``/``middle_elide``) and the
box glyphs from :mod:`packrat.tui.tokens`. The Textual screens display these by
delegation — they never re-derive the box math.
"""

from __future__ import annotations

from .layout import cell_width, middle_elide
from .layout import fit_width as pad
from .layout import wrap_hints
from .tokens import CW as REF_CW
from .tokens import H as REF_H
from .tokens import HEAVY_BOX, LIGHT_BOX


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


def screen(title: str, content: list[str], right: str = "",
           footer: str | list[str] = "",
           *, width: int | None = None, height: int | None = None) -> str:
    """Wrap ``content`` in the W×H frame — the :class:`AppFrame` (§12).

    ``title`` is the top-border label (``packrat · Roots``); ``right`` its
    right-aligned counterpart (``daemon ● up``). The ``footer`` (HintBar) is pinned
    to the bottom, with blank filler *above* it. A **string** footer is wrapped to
    the content width via :func:`wrap_hints` (a long hint bar becomes 2+ lines on a
    narrow terminal instead of truncating); pass a **list** to pin exact rows.

    ``width``/``height`` default to the reference 100×24 (so the frozen mockup
    generator + golden tests are unchanged); the app passes the live terminal size
    for a full-screen responsive frame. Result is exactly ``height`` lines of
    ``width`` cells each.
    """
    cw = (width - 2) if width is not None else REF_CW
    h = height if height is not None else REF_H
    lt = f"─ {title} "
    # The right label (in root detail this is "<path> · <kind>") is the first thing
    # trimmed when the bar overflows: middle-elide it so the drive + "· <kind>" tail
    # stay visible. cell_width (not len) so CJK titles/paths keep the bar aligned.
    if right:
        avail = cw - cell_width(lt) - 2          # 2 = the " " and "─" around `right`
        right = middle_elide(right, max(0, avail))
        rt = f" {right} ─"
    else:
        rt = "─"
    fill = max(0, cw - cell_width(lt) - cell_width(rt))
    top = "┌" + pad(lt + "─" * fill + rt, cw) + "┐"
    rows = h - 2  # available body rows

    foot_lines = (wrap_hints(footer, cw - 2) if isinstance(footer, str) and footer
                  else list(footer))
    inner = list(content)
    if foot_lines:
        keep = max(0, rows - len(foot_lines))
        inner = inner[:keep]                      # leave the last rows for the footer
        inner += [""] * (keep - len(inner))       # fill the gap ABOVE the footer
        inner += foot_lines                       # pinned to the bottom row(s)
    else:
        inner = (inner + [""] * rows)[:rows]
    inner = inner[:rows]
    body = ["│ " + pad(ln, cw - 2) + " │" for ln in inner]
    return "\n".join([top] + body + ["└" + "─" * cw + "┘"])
