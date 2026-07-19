# M6 — TUI component layout library (plan, review before implementing)

A small, packrat-specific **component + layout kit** for the M6 Textual TUI. Its job is to
**standardize** the UI vocabulary the mockups already fixed (`docs/M6-tui-mockups.md`) and to make
PLAN **§12's hard requirements enforceable in one place** — the fixed **100×24** frame, "scroll
*within* a panel, never resize the frame", and long-value **elide-vs-wrap**.

The kit covers the four things that make packrat's TUI a live, navigable app rather than a static
render, each in its own section: **rendering** (tokens + `row()`/`fit()` + the content widgets),
**navigation & focus** (screen stack + the focus→maximize state machine), **liveness** (a
`DataSource` seam over queries + SSE + poll), and **interaction** (form controls + modal). Rendering
is the largest piece, but the other three are load-bearing — a keyboard-driven, live-updating app is
mostly navigation and data flow.

> **This is not a layout engine.** Textual already gives us containers, CSS grid/flex, focus,
> scrolling, screens, reactives, and mouse. We do **not** reimplement any of that. This kit is a thin
> layer of packrat-shaped widgets + design tokens + pure text-grid helpers, plus the packrat-specific
> *state machines* (focus→maximize, running→terminal card) Textual can't know about. Anything
> general-purpose is Textual's job; we only encode packrat's conventions on top.

## Why build it

1. **Single enforcement point for §12.** The 100×24 canvas, per-panel scrolling, and
   elide/wrap rules live once in `AppFrame` / `Panel` / `PagedList` instead of being re-derived per
   screen. That is a correctness win, not just tidiness — §12 calls fixed layout a *hard
   requirement*, and "if an interface can't fit, trim it — don't grow the window."
2. **The generator is already the spec.** `docs/_tui_mockup_gen.py` is a de-facto component
   library: `screen()` / `box()` / `hjoin()` / `pager_line()` / `pad()` + the `◉◐○ ▸ ▶ ⚠` glyphs are
   exactly our recurring vocabulary. We extract those settled primitives into runtime widgets
   rather than re-designing.
3. **Golden-frame snapshot tests.** Because both the doc frames and the live widgets share one
   vocabulary *and one fixture source* (below), each widget's plain-text render can be asserted
   against the corresponding 100×24 frame in `docs/M6-tui-mockups.md`. One assertion keeps the doc
   honest *and* the UI standardized. (Textual ships `pytest-textual-snapshot`; pure widgets compare
   as strings.)
4. **Components ⇄ data contracts (§1.6).** The TUI is pure presentation over the daemon read-model.
   Typing each widget to the *actual* query payload makes "widget ⇄ data shape" explicit and stops
   the TUI inventing state the CLI doesn't have.
5. **One home for the state machines.** The focus→maximize interaction and the running→terminal card
   swap are packrat-specific behavior with real edge cases (Esc back-out depth, reconnect, a job that
   finished while detached). Centralizing them in `nav.py`/`data.py` keeps every screen from
   re-deriving them subtly differently.

## Non-goals

- No responsive/reflowing layout, no resize handling (§12: extra terminal space is margin).
- No general layout engine. Theming **is** in scope but deliberately minimal: a closed set of
  semantic color **roles** + one `Theme` table in `tokens.py` + one `.tcss` — not an open theming
  framework, per-widget color knobs, or user-editable themes in v1 (a `dark`/`high-contrast` theme is
  a later table, not new machinery — see § Theming).
- No new daemon calls or state — every widget renders an existing read-model shape (below); the
  `DataSource` layer *subscribes to* those queries, it doesn't add new ones.
- No TUI-only action (§1.6): action triples only *label + bind* keys that map to existing CLI verbs;
  a deferred verb is shown disabled.

---

## Component inventory

Each row: the widget, the mockup section it comes from, the generator primitive it replaces, and
the **daemon read-model shape** it renders (from `src/packrat/queries.py`). "Data shape" is the dict
the query already returns — the widget takes exactly that.

**Chrome / layout primitives**

| Widget | Mockup | Generator primitive | Data shape (from `queries.py`) |
|---|---|---|---|
| `AppFrame` | all | `screen(title, content, right, footer)` | — (chrome only) |
| `Panel` | §1–§5 boxes | `box(title, lines, width, right, heavy)` | — (container; `heavy=focus`) |
| `HintBar` | every footer | the `footer=` string | — (static `[key] label` list) |
| `TitleBar` | every top border | `screen()`/`box()` title logic | header: `GET /health` + `/daemon`; title = `packrat · <name>` |
| `PagedList` | §1.3/1.4/2.1/3/4 | list rows + `pager_line(width,cur,total)` | any `list[dict]` + an `empty=` message |

**Content widgets (bound to a read-model shape)**

| Widget | Mockup | Generator primitive | Data shape (from `queries.py`) |
|---|---|---|---|
| `Logo` | §1 | `LOGO` + `assets hoarded` line | `status_snapshot()["assets"]` |
| `CollectionBox` | §1 | `collbox(lastscan)` | `status_snapshot()`: `assets, photos, videos, trashed`; `Last scan` = max over `roots[].last_scan_at` |
| `RootRow` | §1/§2 | `rootrows()` | `roots_snapshot()` row (incl. `photos,videos,last_dedup_at`) + derived `dot` |
| `JobRow` | §1.4/§4 | queue line strings | `queued_jobs()` / `recent_jobs()` row: `id,type,status,label,blocked,total,done,…` |
| `ProgressBar` | §1.4/§4/§5.1 | `███░░░ 67% … ETA 4m` inline | SSE `progress` event: `{done,total,message}`; ETA TUI-derived |
| `ResultCard` | §5.1–§5.6 | `screen(...)` bodies | `recent_jobs`/`job_detail` row; running → SSE; terminal → `result_json` |
| `RootDetail` | §3 | `d1`/`d2` bodies | `root_detail(root)` (full dict, below) |
| `StatusDot` | conventions | `◉◐○` glyph pick | `(kind, last_scan_at, last_dedup_at)` → `◉/◐/○` or *(trash → no dot)* |

**Form / input controls (shared across the form-bearing interfaces)**

| Widget | Mockup | Generator primitive | Notes |
|---|---|---|---|
| `RadioGroup` | §2.2 Kind, §3.3 Source | `(•)/( )` rows | single-choice; `[Tab]`/arrows move, drives which sub-panel shows |
| `Checkbox` | §2.2 scan, §3.3 `--dry-run` | `[x]/[ ]` | boolean flag toggles |
| `PathInput` | §2.2 Path, §3.3 External | the `▸ …___` field | free-typed path (or name) field with the `▸` focus cursor |
| `AddRootForm` | §2.2 | `alines` | composes RadioGroup+Checkbox+PathInput → `register_root(path,name,kind,scan,full,embed)` |
| `MergePicker` | §3.3 | `d3`/`d3b` | RadioGroup (Registered root ↔ External) → a `PagedList` of library roots **or** a `PathInput` → `merge <source> --into <this>` |
| `Modal` (+ `ConfirmModal` / `MessageModal` / `ChoiceModal`) | Open Q#4 + future | — | reusable centered overlay within the fixed frame; variants for confirm / notice / quick-pick. See [Modals & overlays](#modals--overlays) |

### Concrete data shapes the widgets bind to (already shipped)

- `status_snapshot()` → `assets, photos, videos, trashed, running, queued[], interrupted[],
  pending_reviews[], roots[]`. **`CollectionBox` + `Logo`** read the four counts; `Last scan` is
  derived from `max(roots[].last_scan_at)` (no top-level scan-time field).
- `roots_snapshot()` → per root `id,name,path,kind,enabled,last_full_scan_at,asset_count,
  photos,videos,instance_count,last_scan_at,last_dedup_at`. **`RootRow`** draws the ◉/◐/○ dot from
  `(last_scan_at, last_dedup_at)` and the `[s]` sort cycle uses `photos`/`videos`. (`photos`/`videos`/
  `last_dedup_at` were added to the query for M6 — resolving the mockups' Open Q#1; order stays `id`
  ascending for CLI compatibility, TUI sorts client-side.)
- `root_detail(root)` → `id,name,path,kind,…,photos,videos,instances,pending_review{…,counts},
  last_dedup_at,last_cleanup_at,running_job,queued_jobs[],last_scan,undecodable_current,
  problem_files[]`. **`RootDetail`** renders this whole dict; the Jobs list is `root_jobs(root_id)`.
- `queued_jobs()` / `recent_jobs(limit)` / `root_jobs(root_id,limit)` → job rows shaped by
  `_job_dict`: `id,type,root_id,status,total,done,…,error,result_json,label,blocked,root_name`.
  **`JobRow`** renders `label` + status/`blocked`; **`ResultCard`** switches on `result_json` op /
  falls back to `status`+`error` (the §5 "every job show-able" contract).
- **SSE stream** (`client.stream_job`, see `cli/stream.py`) → events `progress{done,total,message}`,
  `log{message}`, `state/done/error{status,blocked}`. **`ProgressBar`** + the running **`ResultCard`
  (§5.1)** render from this; on `done`/`error` they swap to the terminal `result_json` view.

### `StatusDot` — the three states + the trash case

```python
def status_dot(kind: str, last_scan_at, last_dedup_at) -> str:
    if kind == "trash":        return " "        # trash roots show "(trash)", never a dot
    if last_dedup_at:          return "◉"        # scanned AND successfully deduped
    if last_scan_at:           return "◐"        # scanned only, never a successful dedup
    return "○"                                    # never scanned nor deduped
```

Trash roots are a real fourth branch, not an oversight: every mockup renders `_Trash … (trash)` with
**no dot** and no counts. `RootRow` therefore asks `StatusDot` for the glyph (blank for trash) and
substitutes `(trash)` for the count column when `kind=='trash'`. The `(last_scan_at, last_dedup_at)`
truthiness rule is the same all-stages-or-already-clean success rule as `root_detail`/§11.

---

## Layout helpers (text-grid math)

Textual's CSS covers *widget-box* layout. These two pure helpers cover the **text-cell** concerns
inside a fixed monospace panel, which CSS does not do for us — horizontal cell alignment and vertical
height budgeting:

### 1. Text alignment within a row (flexbox-like)

A one-line **row composer** that places labelled cells in a fixed character width with justify modes
— the equivalent of a single flex `row`, but measured in **terminal cells**, not pixels, so columns
line up in the monospace grid the mockups assume.

```python
# packrat.tui.layout
@dataclass
class Cell:
    text: str
    width: int | None = None          # FIXED cell: exactly this many cells (the RootRow norm)
    grow: int = 0                     # GROW cell: shares leftover space, weighted by `grow`
    align: str = "left"               # 'left' | 'right' | 'center' — within the cell
    elide: str = "end"                # 'none' | 'middle' | 'end' — how to shrink an over-width cell
    style: str | None = None          # SEMANTIC class name (e.g. 'running', 'warn', 'dim') —
                                      #   a color ROLE, never a raw color. Layout ignores it entirely;
                                      #   only the render step maps it to a theme color. See Theming.

def row(width: int, cells: list[Cell], *, gap: int = 1, justify: str = "pack") -> str:
    """Compose one fixed-width line from cells (result is ALWAYS exactly `width`).

    Sizing: fixed cells (`width=`) keep their width; the remaining space (minus `gap`
    between cells) is divided among `grow` cells in proportion to their `grow` weight.
    `justify` places the cells when their fixed widths don't fill the row and there are
    no grow cells: 'pack' (left, default) | 'between' (spread edge-to-edge, e.g. HintBar)
    | 'center'. Each cell's own text is positioned by its `align`; an over-width cell is
    shrunk by its `elide`. Guarantee: `len(row(width, …)) == width`, always.

    Color is orthogonal: `style` is a semantic class, NOT applied here. `row()` measures
    and returns PLAIN text (so width math and golden-frame tests stay colorless); the
    component's render step wraps each cell's span in its theme color from `style` (see
    Theming). One text, two layers: geometry (here) and color (there).
    """
```

Two sizing idioms, both from the real mockups:
- **Fixed-column rows are the norm** (this is *why* mockup columns align). `RootRow` = `[cursor
  width=1][name width=9][path width=20, elide=middle][dot width=1][count width=7, align=right]` —
  exactly the `f"{nm:<9} {pth:<20} {dot} {cnt:>7}"` in `rootrows()`, now with the path cell
  middle-eliding at its fixed 20 (per §12) instead of hard-truncating. `JobRow` = `[id width=4][label
  width=…][status/blocked, align=right]`.
- **`grow` is the exception, for a single flexible cell** — e.g. a wide label that should absorb
  slack next to fixed columns. Multiple `grow` cells split leftover by weight (`grow=2` gets twice
  `grow=1`); the common case is one grow cell. RootRow does **not** use grow — its columns are fixed,
  which is what keeps every row's dot/count aligned.
- **`justify`/`align` for non-column rows**: `HintBar` uses `justify='between'` (or `'pack'`, as
  today); `pager_line(width) == row(width, [Cell(s, align='center')], justify='center')`.

This is deliberately **1-D** (one line of cells) — all the fixed grid needs. Vertical stacking stays
Textual's job (containers). That single `len == width` invariant is what makes "never widen the
window" mechanical rather than vigilant.

#### `elide='middle'` — long paths on one line (the §12 path rule)

A path too long for its cell is collapsed **from the middle**, keeping the **head (drive + start)**
and the **tail (leaf)** visible — the two ends carry the identity; the middle folders are the
throwaway. This is §12's "middle-elide so the drive and leaf stay visible":

```python
def middle_elide(text: str, width: int, ellipsis: str = "…") -> str:
    """Collapse `text` from the middle to exactly `width` cells (keep head + tail)."""
    if len(text) <= width:
        return text
    if width <= len(ellipsis):
        return ellipsis[:width]
    keep = width - len(ellipsis)
    head = (keep + 1) // 2          # bias the extra cell to the head (drive side)
    tail = keep - head
    return text[:head] + ellipsis + (text[-tail:] if tail else "")
```

Worked example (a real VCB-Studio release path), collapsed to a 50-cell column:

```
W:\[Nekomoe kissaten&VCB-Studio] Yahari Ore no Seishun Lovecome wa Machigatte Iru. Kan [Ma10p_1080p]   (100 cells)
                                        ↓  elide='middle', width=50
W:\[Nekomoe kissaten&VCB-…e Iru. Kan [Ma10p_1080p]                                                     (exactly 50)
```

Notes:
- **One `…` glyph** (U+2026), one cell — matches the mockups' convention (not `...`, which costs 3
  cells and mis-measures the grid).
- **Head-biased split** — the odd leftover cell goes to the head, so the drive/prefix (`W:\[Nekomoe
  kissaten&VCB-`) stays as intact as possible; the leaf/qualifier (`[Ma10p_1080p]`) is the tail.
- **Compact rows only.** Middle-elide is for the fixed-width **list** columns (dashboard/Roots
  `RootRow`, `JobRow`). The roomy **detail/card** views (§3, §5) instead **wrap** the full path over
  multiple lines (helper #2) — §3 always shows the untruncated path. So elide never hides a path the
  user can't otherwise get to; the full value is one drill-in away.
- **`elide='end'`** (trailing `…`) stays available for labels/messages where only the start matters;
  `middle` is the default for the `path` cell.

### 2. Multi-line height budgeting (fit content to the panel, never grow the frame)

The frame is fixed at 24 rows; each panel gets a **row budget**. Long text must **wrap within** its
panel and, if it still overflows, **scroll within** the panel (§12) — it must never push the frame
taller. A height calculator makes this explicit *before* render:

```python
def wrap_cells(text: str, width: int) -> list[str]:
    """Word-wrap to `width` cells (monospace), hard-breaking tokens longer than width."""

@dataclass
class Fitted:
    rows: list[str]        # EXACTLY `budget` lines, padded — safe to drop straight into a Panel
    overflow: int          # how many source lines didn't fit (0 if all shown)
    scrollable: bool       # True if mode='scroll' and overflow>0 (a PagedList should page)
    total_pages: int       # ceil(len(lines)/page_size); feeds pager_line(width, cur, total_pages)

def fit(lines: list[str], budget: int, *, mode='scroll'|'truncate'|'clip', page: int = 0) -> Fitted:
    """Fit `lines` into `budget` rows (`Fitted.rows` is always exactly `budget`).
      - 'scroll'   → page through all lines (PagedList/detail Jobs); render the `page`-th window.
      - 'truncate' → keep budget-1 lines + a '… N more' marker (compact previews, e.g. §1.2 queue).
      - 'clip'     → hard cut (last resort).
    """
```

Where each mode is used, straight from the mockups:
- **Compact list rows** (dashboard §1.2 queue box) → `truncate`: the box is a *fixed-height
  preview* (documented in §1.2/§4 notes — logo+collection eat 15 rows, so ≤4 items fit); the 5th+
  live in the maximized view. This helper is what encodes "show N, defer the rest" so a 5th row can
  never silently eat the box's bottom border again (the bug we hit).
- **Roomy detail / card views** (§3 root detail, §5 cards) → **wrap** a long NAS path onto multiple
  lines (vertical space is cheaper here — §12), full path always shown in §3.
- **Scrollable panels** (§2.1 roots, §3 Jobs, §4 queue) → `scroll` + `page i/N` paginator: the list
  scrolls a window inside its fixed-height panel; `PagedList` computes `total = ceil(len/budget)`
  and feeds `pager_line(width, cur, total)`.

**Height budget arithmetic (matches the generator).** `AppFrame` owns `H=24` → `H-2=22` body rows;
a pinned `HintBar` reserves 1 (`screen()` today: `inner[:rows-1]` + footer). A `Panel` reserves 2
for its top/bottom border. So a maximized list panel's usable rows = `22 − 1(hint) − 2(border) −
header/rule lines`. `fit(..., budget)` receives exactly that number; if `content > budget` it
*scrolls or truncates* — it can return **more** rows only over the frame's dead body, which is the
signal §12 says to trim, and the snapshot test catches it.

---

## Theming (color roles & classes)

Text and borders carry color, assigned by **semantic class** — like CSS: a widget tags a span
`highlighted` / `running` / `warn`, and the *theme* decides what color that role is. This keeps the
one-source-of-truth discipline (a color changes in one place, everywhere it means "warning" updates)
and makes light/dark or a future recolor a theme swap, not a widget edit.

**The hard rule: color is a separate layer from geometry.** `row()`/`fit()`/`middle_elide` measure
and return **plain, colorless text** — width math and the golden-frame snapshot tests must never see
color markup (an ANSI/markup byte would break `len == width` and every string assertion). A cell
carries a `style` *class name* only; the component's render step maps class → theme color and wraps
the span **after** layout. So the pipeline is always: **lay out plain text → color the spans**. Two
layers, one text.

- **Roles, not raw colors (the token layer).** `tokens.py` defines a small closed set of **semantic
  roles** — the vocabulary a widget is allowed to reference:

  | Role | Used for (from the mockups) |
  |---|---|
  | `default` | normal body text |
  | `dim` | `‹dry-run›` rows, secondary/hint text, disabled actions, empty-state messages |
  | `highlighted` / `selected` | the `▸` cursor row in a focused list |
  | `running` | `▶` running job + its progress bar fill |
  | `warn` | `⚠` awaiting-review / attention |
  | `error` | failed job status, a `RootError` in a form |
  | `success` | `◉` deduped dot / a clean "done" result |
  | `accent` | titles, the focused-panel heavy border, key letters in `[k]` hints |
  | `muted-border` / `focus-border` | unfocused vs focused `Panel` frame |

- **Themes map roles → colors (the theme layer).** A `Theme` is one table `role → color` (Textual
  color / hex). `tokens.py` ships a `DEFAULT_THEME` (and can add `dark`/`high-contrast`/… later);
  the active theme is chosen once at app start. Widgets **never name a color** — only a role — so
  adding a theme or recoloring never touches a widget. This is the "define `highlighted` once, assign
  the class to text" you asked for.
- **How a class reaches the screen.** Two equivalent routes, both post-layout:
  - **Textual-native (preferred for whole widgets):** the role is a CSS class in `packrat.tcss`
    (`.running { color: $running; }`), with the theme's roles injected as Textual CSS variables
    (`$running`, `$warn`, …). Panel borders use the same variables (`border: heavy $focus-border;`),
    so **colored borders** are just a role on the frame — no new mechanism.
  - **Per-span (for mixed-color text in one built line):** after `row()` returns the plain string,
    the widget wraps each cell's slice in a Textual `Text`/markup span tagged with its `style` role →
    theme color. Because this happens on the *already-measured* string, widths are untouched.
- **Golden-frame tests stay colorless — and gain a color assertion.** The snapshot compares the
  **plain** render (color stripped / never applied), so the 100×24 frames in the mockups doc are
  unaffected. Color correctness is a *separate*, cheaper test: assert the **role map** — e.g. a
  running `JobRow`'s status span has role `running`, a `‹dry-run›` row has `dim` — without asserting
  concrete hex, so a theme retune never breaks tests. (Roles are stable; colors are free to change.)

---

## Navigation & focus

The mockups are not a set of static frames — they are one app you *move through*: dashboard →
`[r]` focus a box → `[r]` again maximize → `Enter` a detail → `Enter` a result card → `Esc` back.
`row()`/`fit()` draw a frame; **this section owns which frame is showing and where the keys go.** It
is as load-bearing as the layout helpers, and packrat-specific (Textual gives the primitives —
`Screen`, `push_screen`/`pop_screen`, `focus` — but the state machine below is ours).

- **Screen stack (`ScreenStack`).** Each maximized interface is a Textual `Screen`: `Dashboard`,
  `RootsMax` (§2.1), `AddRootForm` (§2.2), `RootDetail` (§3), `MergePicker` (§3.3), `QueueMax` (§4),
  `JobCard` (§5), plus `Modal` overlays (see [Modals & overlays](#modals--overlays)). `Enter`
  **pushes** (drill in), `Esc` **pops** (back out) — a single stack gives correct multi-level back-out
  (card → detail → dashboard, or modal → its opener) for free, and every screen renders in the same
  `AppFrame`, so navigation *swaps content in place* (the §12 fixed-frame rule) rather than resizing.
- **The focus→maximize state machine (dashboard only).** The dashboard's Roots/Queue boxes are the
  one place with a two-press interaction; model it explicitly, not ad-hoc:

  | State | `[r]`/`[q]` | `↑/↓` · `←/→` | `Enter` | `Esc` |
  |---|---|---|---|---|
  | **unfocused** | → *focused* (heavy frame + `▸`) | (no-op) | (no-op) | (no-op) |
  | **focused** (box) | → *maximized* (push §2/§4) | move cursor / page **in place** | push detail/card | → *unfocused* |
  | **maximized** (a Screen) | (screen's own keys) | screen's list nav | screen's drill-in | pop screen |

  `[r]` and `[q]` are peers — focusing one unfocuses the other. `Ctrl-C` quits (since `[q]` is taken).
  This table *is* the spec the `Dashboard` screen implements; the `Panel`'s `heavy=` styling is just
  the visual reflection of the `focused` state, not the state itself.
- **Focus owns the arrows.** Exactly one list has keyboard focus at a time; `↑/↓` selects within it,
  `←/→` pages it (per the paginator), `Tab` cycles focus between the sibling panels on a screen
  (Roots → Queue → per-root jobs on the dashboard). A `PagedList` exposes `on_focus`/`selection` so
  the owning screen routes keys to the focused one — components never grab global keys themselves.
- **Actions are declared, not hard-wired.** A screen declares its footer actions as
  `(key, label, handler)` triples; the `HintBar` renders `label`s from that same list (so the hint
  bar can never drift from the real bindings), and each `handler` maps to a CLI verb (§1.6) — the
  single place the "no TUI-only action" rule is enforced. A deferred verb is declared `disabled=True`
  → shown greyed, not bound.

## Data & liveness

Widgets "take exactly that dict" (above) — but a TUI is *live*: the running bar moves, a finishing
job refreshes the Collection/Roots counts and flips a root's dot, and a running `ResultCard` swaps to
its terminal card on completion. **This section owns *when the dict changes and who pushes it*** — the
other half of what makes this a TUI and not a renderer. Missing it would leave every widget with no
answer to "and then it updates how?"

- **`DataSource` — one subscription seam per read-model query.** A thin object wrapping a
  `queries`/daemon-client call (`status_snapshot`, `roots_snapshot`, `root_detail`, `queued_jobs`,
  `recent_jobs`, `root_jobs`) that a screen **subscribes** to; on refresh it re-fetches and pushes the
  new dict into the bound widgets (via Textual **reactives** — set the reactive, the widget
  re-renders). Widgets stay pure `dict → frame`; the `DataSource` is the only thing that knows the dict
  can change. No widget calls the daemon directly.
- **Three refresh triggers (all already specified in the mockups):**
  1. **SSE stream** — the running job's `progress`/`log`/`state` events drive `ProgressBar` and the
     live `ResultCard` (§5.1) continuously. This is a *push*, not a poll.
  2. **Job-finished refetch** — on the SSE `done`/`error` event, immediately re-fetch the snapshot
     `DataSource`s so Collection tallies, `Last scan`, and the ◉/◐/○ dots update the instant a job
     completes (the behavior we pinned in the mockups), and the open running `ResultCard` swaps to its
     terminal `result_json` view.
  3. **Light poll timer** — a low-frequency `set_interval` refetch of the snapshot `DataSource`s as a
     backstop, so a job started in *another* terminal (no local SSE) still appears. Poll cadence lives
     in `tokens.py`.
- **Running → terminal card swap is a `DataSource` state transition, not two widgets.** One
  `ResultCard` bound to a job: while `status=='running'` it renders from the SSE `DataSource`; the
  `done`/`error` event flips it to the `job_detail`/`result_json` `DataSource` in place (the §5.1
  refresh). A dropped SSE stream just reconnects (job state is durable); if the job already finished
  while detached, the card opens straight to the terminal view.
- **Reconnect / daemon-down are first-class.** The `TitleBar`'s `daemon ● up` / `○ down` reflects the
  client's health; a dropped connection degrades to the poll timer and shows `○ down` rather than
  erroring. (Ties into the empty/loading/error states below.)

## Modals & overlays

The one-shot count-confirm (Open Q#4) is only the *first* modal; overlays are a recurring need
(confirmations, transient errors, small prompts, pick-lists that don't warrant a full screen). So
`Modal` is a **reusable base primitive**, not a single-purpose widget — designed for growth from the
start.

- **`Modal` is a Textual `ModalScreen` pushed onto the same `ScreenStack`** (§ Navigation) — so it
  layers over the current screen, `Esc` pops it (returns to exactly where you were), and the parent
  keeps its state. It is **modal**: keys go to the overlay until it closes; the backdrop is dimmed,
  not interactive.
- **It honors the fixed frame (§12).** A modal is a *centered inset* **within** the same 100×24
  `AppFrame` — it never resizes or escapes the frame; it draws a smaller bordered `Panel` over a
  dimmed backdrop. Same width/height budgeting (`row()`/`fit()`) applies to its body, so an
  over-long message wraps inside the modal rather than widening it. A modal too tall for the frame is
  itself a `fit()`-scrollable body, never a taller window.
- **One base, typed variants** (composed from existing components — no new rendering machinery):
  - **`ConfirmModal`** — a message + `[y]/[n]` (or a **typed-count** field via `PathInput` for the
    §6 delete-set confirmation, where the network-path permanent-delete warning also shows). Returns a
    bool/`None` to the caller.
  - **`MessageModal`** — a dismissable notice (a `RootError` from the add-root form, a transient
    "daemon unreachable", a completion toast). `[Enter]`/`Esc` closes.
  - **`ChoiceModal`** — a small `PagedList` of options for a quick pick that doesn't deserve a full
    screen (a lightweight sibling of `MergePicker`'s roots list).
- **Result flows back by callback, not shared state.** `push_modal(ConfirmModal(...), on_result=cb)`
  — Textual's screen-dismiss result mechanism. The opening handler resumes when the modal dismisses,
  so a modal that gates a CLI verb (typed-count confirm → `cleanup … --confirm`) stays a linear "ask,
  then act" flow, and the §1.6 rule holds (the modal only *gathers input*; the action is still a CLI
  verb).
- **Future-friendly by construction.** New modal kinds subclass `Modal` and supply a body built from
  the same components + `row()`/`fit()`; they inherit the centering, dimmed backdrop, `Esc`-to-close,
  fixed-frame containment, and callback-result plumbing for free. Nothing about a new modal touches
  the layout core.

---

## Module shape

```
src/packrat/tui/
  tokens.py      # W=100,H=24; column widths (COLLECTION_W=29, ROOTS_W); glyphs ◉◐○▸▶⚠;
                 #   COLOR ROLES + Theme table (role→color) + DEFAULT_THEME; poll cadence.
                 #   One source of truth (values only, no Textual import).
  layout.py      # row()/Cell (align+elide+style), wrap_cells(), fit()/Fitted — pure string helpers.
  data.py        # DataSource: subscribe/refetch over queries + SSE + poll timer (the liveness seam).
  nav.py         # ScreenStack + the focus→maximize state machine + action-triple declarations.
  components/    # AppFrame, Panel, HintBar, TitleBar, PagedList, Logo, CollectionBox, RootRow,
                 #   JobRow, ProgressBar, ResultCard, RootDetail, StatusDot,
                 #   RadioGroup, Checkbox, PathInput   (form controls reused by the forms)
  modals.py      # Modal base (ModalScreen) + ConfirmModal / MessageModal / ChoiceModal variants
  packrat.tcss   # the one stylesheet: fixed root container; borders + text colored by ROLE via
                 #   Textual CSS vars ($running/$warn/$accent/…) injected from the active Theme.
  screens/       # Dashboard, RootsMax, AddRootForm, RootDetail, MergePicker, QueueMax, JobCard
  fixtures.py    # the shared sample data (see Testing) — imported by BOTH the widget tests
                 #   and docs/_tui_mockup_gen.py, so frames and widgets render from one source.
```

`tokens.py` is the **single source of truth** shared with the generator: `_tui_mockup_gen.py` imports
`W,H` and the glyphs/widths from it so the doc and the runtime can never drift on the numbers (today
they're duplicated as `W,H=100,24` / `29` / `ROOTS_W`). It also owns the **color roles + `Theme`
table** (§ Theming) — the roles map to Textual CSS variables the `.tcss` consumes, so a color changes
in exactly one place. Pure-string modules (`tokens`, `layout`, `fixtures`) import **without** a Textual
runtime (they hold only *values* — role names, hex — not widgets), so the generator uses them headless
and renders its frames colorless.

## Testing — golden frames

- **Shared fixtures are the crux (stated principle, not an aside).** The mockup frames in
  `docs/M6-tui-mockups.md` are generated from **hardcoded sample data** in `_tui_mockup_gen.py`;
  widgets render **live query dicts**. For a golden-frame assertion to *mean* anything, both must come
  from **one source** — `tui/fixtures.py` (sample roots, jobs, a `root_detail`, `result_json`s). The
  generator builds its frames from it; the widget tests feed the same fixtures and assert byte-equal
  to the frame slice. Without this, the test asserts a widget against a hand-authored string that
  silently drifts — the exact failure this plan exists to prevent.
- **Snapshot per widget/screen:** render each to plain text from a fixture and assert equal to its
  frame slice in the mockups doc. (Pure widgets compare as strings; a full `Screen` uses Textual's
  `pytest-textual-snapshot` pilot.)
- **Invariant tests (cheap, high value):** `len(row(w, …)) == w` for random cells; `fit(lines,
  budget).rows` is always exactly `budget`; every screen renders to exactly 100×24. These catch the
  whole class of "window grew / border eaten / column misaligned" bugs.
- **Data-binding tests:** feed each widget the real query dict (from a seeded DB via `queries.py`) and
  assert the render — verifies "component ⇄ data contract."
- **State-transition tests:** the focus→maximize table (drive keys via a pilot, assert the screen
  pushed/popped) and the running→terminal `ResultCard` swap (feed a synthetic SSE `done` event, assert
  the card re-renders from `result_json`).
- **Color-role tests (separate from geometry):** assert the **role** a span carries (a running
  `JobRow` status → `running`, a `‹dry-run›` row → `dim`), never a concrete hex — so a theme retune
  never breaks a test. Geometry snapshots stay colorless (color is applied post-layout, § Theming).

## Empty / loading / error states

The mockups show several non-happy states, so they are a **component contract, not an afterthought**:
`idle — no jobs running or queued` (Queue), `no roots registered yet` (Roots), `daemon ○ down`
(TitleBar), and a job that errored. Rules:

- **`PagedList` takes an `empty=` message** rendered (centered, dimmed) when the list is empty — the
  Queue box's `idle` line and the Roots `none registered` line are this, not special-cased screens.
- **`daemon ○ down`** is a `TitleBar` state driven by the data layer's health (above); screens that
  need live data show a dimmed "waiting for daemon…" body rather than erroring.
- **A NULL `result_json`** is already handled by `ResultCard` keying off `status` first (§5) — the
  error/interrupted branch is a first-class render, not an exception path.

---

## Resolved decisions

1. **Share `tokens.py` with the generator — DECIDED (share).** `_tui_mockup_gen.py` imports the
   constants (`W,H`, column widths, glyphs) from `packrat.tui.tokens`, so the doc frames and the
   runtime can never drift on the numbers. The generator stays a thin script; it only pulls the
   token *values*, not any Textual widget (see #2 — the grid math it also needs is pure-function, so
   it's importable without a Textual runtime).

2. **`row()`/`fit()` — pure string helpers, with optional thin widget wrappers — DECIDED.** They
   stay **pure string functions** as the source of truth; components are Textual widgets that *call*
   them. Adding widget wrappers on top is fine **as long as the widget delegates to the pure
   function** and never reimplements the math. Direct answer to "any downside to also making them
   components?" — the downside only appears if the math lives *only* in a widget:
   - **Generator reuse breaks.** `_tui_mockup_gen.py` has no Textual runtime; if `row()`/`fit()`
     were widgets it couldn't call them, and the shared-token anti-drift win (#1) is lost.
   - **Snapshot tests get heavy.** A pure function is `assert row(w, cells) == "…"`; a widget needs
     a Textual `Pilot`/app harness to render. We want the cheap string assertions as the invariant
     net (`len==width`, `fit ≤ budget`).
   - **Lifecycle coupling for nothing.** Cell/row/height math is pure and stateless; wrapping it in
     the mount/refresh/reactive lifecycle adds surface with no benefit, and instantiating a widget
     per cell is heavier than a string op.
   So: **keep the pure functions; wrap in widgets only where a widget earns its keep** (focus,
   CSS, mouse) and always by delegation. No downside under that rule.

3. **`RootRow` dot — DECIDED (query extended, no degrade needed).** `roots_snapshot()` now returns
   `photos`, `videos`, and `last_dedup_at` (the last via the same `_last_completed_at(…, 'dedup')`
   success rule as `root_detail`/§11), so `RootRow` draws the real ◉/◐/○ from day one and the `[s]`
   sort cycle has its keys. Order stays `id` ascending (CLI-compatible); the TUI sorts client-side.
   The earlier "degrade to ◐/○" fallback is no longer needed.

4. **Elide policy per column — DECIDED (per-`Cell` default).** `middle` is the default for the
   `path` cell (keep drive + leaf, §12 — see `middle_elide` above); `end` for labels/messages where
   only the start matters. Encoded as the `Cell.elide` default so it's consistent everywhere without
   each call-site restating it.
