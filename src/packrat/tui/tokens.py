"""Design tokens — the single source of truth for the M6 TUI (§12).

**Values only, no Textual import.** This module holds pure constants — the
reference window size, column widths, glyphs, semantic color *roles*, and the
role→color :class:`Theme` table — so it is importable headless. ``W``/``H`` are the
*reference* (minimum) size the frame tests pin to; the live app scales up from it
(:mod:`packrat.tui.geometry`). The ``.tcss`` stylesheet consumes the active theme's
roles as Textual CSS variables, so a color changes in exactly one place.

Nothing here depends on Textual; :mod:`packrat.tui.layout` (also pure) and the
widget/screen modules (which *do* import Textual) build on top.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Fixed window (§12 "Fixed layout", a hard requirement) ------------------
# Every interface renders inside this identical region; navigation swaps content
# in place, never resizing the frame. The generator renders every mockup into the
# same W×H frame to mechanically demonstrate the rule.
W = 100  # outer window width  (columns)
H = 24   # outer window height (rows)
CW = W - 2  # content columns inside the outer border (│ … │)

# --- Dashboard column widths (why the mockup columns line up) ---------------
# The dashboard top row is Collection(COLLECTION_W) + gap(1) + Roots(ROOTS_W),
# and must sum to the inner content width screen()/AppFrame pads to (CW-2), or an
# hjoin overflows and clips the right border. ROOTS_W is derived, never hardcoded.
COLLECTION_W = 29
GAP = 1
ROOTS_W = (CW - 2) - COLLECTION_W - GAP

# --- Glyphs (each one terminal cell — align in a monospace TUI font) --------
# Root freshness/health dot (conventions in the mockups doc):
DOT_DEDUPED = "◉"   # ◉ solid  — scanned AND successfully deduped
DOT_SCANNED = "◐"   # ◐ half   — scanned, never a successful dedup
DOT_NEVER = "○"     # ○ hollow — never scanned nor deduped
DOT_TRASH = " "          # trash roots show "(trash)", never a dot
CURSOR = "▸"        # ▸ selection cursor
RUNNING = "▶"       # ▶ running job
WARN = "⚠"          # ⚠ needs attention
ELLIPSIS = "…"      # … one cell (middle-elide), NOT "..." (3 cells)
CHECK = "✓"         # ✓ applied stage / done
CROSS = "✗"         # ✗ error

# Progress-bar cells (§1.4/§4/§5.1 — "███░░░").
BAR_FILL = "█"      # █
BAR_EMPTY = "░"     # ░

# Box-drawing sets — light (unfocused Panel) and heavy (focused Panel).
# (title, left/right corners top+bottom, horizontal, vertical.)
LIGHT_BOX = ("┌", "┐", "└", "┘", "─", "│")  # ┌ ┐ └ ┘ ─ │
HEAVY_BOX = ("┏", "┓", "┗", "┛", "━", "┃")  # ┏ ┓ ┗ ┛ ━ ┃

# --- Liveness cadence (component-plan §Data & liveness) ---------------------
# The light poll timer is the backstop that surfaces work started in another
# terminal (no local SSE); the SSE stream drives the live bar/counts directly.
POLL_INTERVAL_S = 3.0
# Trailing window (seconds) of SSE progress samples the TUI-side ETA averages the
# observed rate over (§ cross-cutting "ETA is computed TUI-side").
ETA_WINDOW_S = 8.0
# Minimum gap (seconds) between live re-renders driven by SSE progress. A scan emits
# one `progress` event PER FILE (hundreds/sec on a local disk); re-laying-out +
# recolorizing the whole frame that often makes the TUI unresponsive. The streamed
# `done`/`total` still update in memory every event (so no data is lost); only the
# repaint is coalesced to this cadence — ~8 fps is smooth without flooding.
STREAM_RENDER_INTERVAL_S = 0.12
# --- Logo animation (dashboard hoard mascot) --------------------------------
# The dashboard re-renders the logo on this tick to shimmer the held gem's color
# gradient; the GEM GLYPH itself swaps (◆→◇→◈) every `LOGO_GEM_SWAP_TICKS` ticks,
# so the color glints continuously while the shape changes more slowly.
LOGO_ANIM_INTERVAL_S = 0.15   # ~7 fps color sweep (cheap: only the top section moves)
LOGO_GEM_SWAP_TICKS = 20      # swap the gem glyph every ~3 s (20 × 0.15)
LOGO_GRADIENT_STEP = 0.045    # gradient phase advanced per tick (full loop ≈ 3.3 s)

# --- Color roles (the token layer, §Theming) --------------------------------
# A widget tags a span with a semantic ROLE, never a raw color; the Theme decides
# the color. This is the closed vocabulary a widget is allowed to reference.
ROLES = (
    "default",        # normal body text
    "dim",            # ‹dry-run› rows, hints, disabled actions, empty-state
    "highlighted",    # the ▸ cursor row in a focused list
    "selected",       # alias of highlighted for a persistent selection
    "running",        # ▶ running job + its progress-bar fill
    "warn",           # ⚠ awaiting-review / attention
    "error",          # failed job status, a RootError in a form
    "success",        # ◉ deduped dot / a clean "done" result
    "accent",         # titles, focused heavy border, key letters in [k] hints
    "muted-border",   # unfocused Panel frame
    "focus-border",   # focused Panel frame
)


@dataclass(frozen=True)
class Theme:
    """One ``role → color`` table (component-plan §Theming, "theme layer").

    Colors are Textual color names / hex strings. Widgets never name a color —
    only a role — so adding a theme or recoloring never touches a widget. The
    ``.tcss`` injects these as CSS variables (``$running`` …); per-span coloring
    maps a :class:`~packrat.tui.layout.Cell`'s ``style`` role through here.
    """

    name: str
    colors: dict[str, str]

    def color(self, role: str) -> str:
        """The concrete color for ``role`` (falls back to ``default``)."""
        return self.colors.get(role, self.colors["default"])


# The v1 theme. A `dark` / `high-contrast` variant is a later table, not new
# machinery (component-plan Non-goals: theming is minimal, a closed set of roles).
DEFAULT_THEME = Theme(
    name="default",
    colors={
        "default": "#d0d0d0",
        "dim": "#6c6c6c",
        "highlighted": "#ffffff",
        "selected": "#ffffff",
        "running": "#5fafff",
        "warn": "#ffcf5f",
        "error": "#ff5f5f",
        "success": "#5fd75f",
        "accent": "#00d7af",
        "muted-border": "#585858",
        "focus-border": "#00d7af",
    },
)


# --- Logo gem gradient (dashboard hoard animation) --------------------------
# The color sweep the mascot's held gem shimmers through — a loop of jewel tones
# (cyan → sky → violet → magenta → rose → gold → back). The dashboard's animation
# timer walks a phase along this loop and interpolates between stops (see
# `colorize.gem_gradient_color`), so the gem glints like a faceted stone. Colorless
# builders/tests never see this; it's applied post-layout only in the live widget.
GEM_GRADIENT = (
    "#00d7af",  # cyan-teal (matches the accent)
    "#5fafff",  # sky blue
    "#af87ff",  # violet
    "#ff5fd7",  # magenta
    "#ff6f91",  # rose
    "#ffcf5f",  # gold
)


def status_dot(kind: str, last_scan_at, last_dedup_at) -> str:
    """The ◉/◐/○ freshness dot for a root (component-plan §StatusDot).

    - trash root       → blank (it renders "(trash)", never a dot);
    - deduped (◉)      → scanned AND successfully deduped (``last_dedup_at`` set —
      the same all-stages-or-already-clean ``completed`` rule as ``root_detail``/§11);
    - scanned only (◐) → scanned, never a successful dedup;
    - never (○)        → never scanned nor deduped (a freshly-registered root).
    """
    if kind == "trash":
        return DOT_TRASH
    if last_dedup_at:
        return DOT_DEDUPED
    if last_scan_at:
        return DOT_SCANNED
    return DOT_NEVER
