# TUI (`packrat` with no arguments)

Typing `packrat` alone opens a full-terminal UI (Textual). It is the **default face** of the tool
and, because jobs live in the daemon, a **live window onto work started from any terminal** — open it
anytime to watch progress or stop a running job. It never *owns* a job; it submits, observes, and
cancels, exactly like the CLI (design tenet, see [design tenets](goals-and-concepts.md)). `packrat --offline` runs the same UI on a bundled
sample dataset (no daemon) for demoing/development.

## Interfaces

- **Dashboard** (default): three stacked full-width sections — the packrat logo + live "N assets
  hoarded" line beside the **Collection** stats box (total/photo/video/trashed + **lifetime deduped**,
  the total files removed across all dedup runs); the **Roots** box (name, path, a **4-state
  freshness dot** [see below], asset count, **total size on disk**); and the **Queue** box (the
  running job's live bar + the queued backlog preview). `[r]`/`[q]` focus the Roots/Queue box (heavy
  accent-colored frame + `▸` cursor); pressing the same key again **maximizes** into the full Roots /
  Queue interface.
- **Roots interface** (maximized): the full root list with a `[s]` **sort cycle** (recent /
  most-assets / photos / videos, client-side over the id-ascending snapshot), `[a]` **add-root form**
  (the [register flow](goals-and-concepts.md) — Tab between fields, radios/checkboxes, paste-aware path input), `[Enter]` opens
  root detail. The **4-state dot legend** (`◉ deduped · ◉ need dedup · ◐ new files probed · ○ never`)
  + `page i/N` share the header line. **Trash-root exception:** a `kind='trash'`
  root has **no detail screen** (detail is scan/dedup/merge/cleanup — all library-only), so `[Enter]`
  on one instead opens a **confirm modal** (the packrat mascot clutching a trash can) asking to absorb +
  empty that folder; confirming issues `packrat trash refresh <root>` (see [trash refresh](trash-model.md)). Same guard from either
  entry point — the dashboard Roots box and this maximized list — so a trash root is never mistaken for
  a browsable library root (see [design tenets](goals-and-concepts.md): the modal action is exactly the CLI verb, nothing TUI-only).
- **Root detail** (see [architecture](architecture.md)): a **3-column stats header** (a folder ASCII icon · assets/photos/videos +
  total size on disk · last-scan / full-scan / last-dedup recency), then **two focus-able bordered boxes** —
  a **Review box** (`[e]`; ⚠ awaiting review, or a calm "No pending review") and a **Jobs panel**
  (`[J]`) laid out like the Queue interface: three independent **Running / Queued / History** sections,
  each with its own paginator (Queued is kept short; History gets the bulk). The two boxes split the
  space below the header by a **responsive ratio (review:jobs ≤ 1:1)**: the Review box shrinks to its
  content (a calm root → 1 row) and Jobs backfills the slack; when a rich review overflows the cap,
  `↑/↓` **scroll** it (title shows a `↑/↓ start–end of n` indicator, the scan-card idiom). A **stage-2
  dedup** review renders the keep-lead pick breakdown in **side-by-side photo/video columns** + a PDQ
  histogram + the internal/external group make-up — the same `review_stats` block the CLI staging log
  prints. A focused box gets the
  heavy accent-colored border and its inside key hints read normal; the unfocused box **dims** its
  hints (the Jobs sub-headers grey; the Review box's `[o]`/`[g]`/`[k]` grey). Within the focused Jobs
  panel `[r]`/`[q]`/`[h]` pick the sub-section (Queued/History paginate with `←/→`, `↑/↓` selects),
  `[Enter]` opens the selected job's result card; the **Running** row shows the live `███░░░` progress
  bar. `Esc` un-focuses (a second `Esc` backs out). Actions map to CLI verbs: `[s]` scan, `[d]` dedup
  (a **choice modal** first picks the keep-preference — prefer external [default] vs. internal, i.e.
  `--prefer-internal`), `[m]` **merge-from picker** (see [architecture](architecture.md) — radio between a paginated registered-root
  list and a typed external-folder path, `Ctrl+D` dry-run toggle), `[c]` cleanup (choice modal: exact /
  perceptual / undecodable), and `[o]`/`[g]`/`[k]` on a pending review (open Explorer / `--confirm` / `--cancel`).
  Actions that need **no confirmation** report via a non-blocking **toast** (a red error toast if the
  submit itself fails), never a modal popup; only confirm-gated deletes still open a modal.
- **Queue interface** (see [data model](data-model.md)): three independently-paged sections — **Running**, **Queued** (with blocked
  reasons), **History** — with per-section focus (`[r]`/`[q]`/`[h]`), `[c]` cancel, `[p]` prioritize,
  `[x]` cancel-all, `[Enter]` result card.
- **Job result/detail card** (see [fingerprints](fingerprints.md)): renders from `jobs.result_json`, keyed off `status` first then `op`;
  a running job shows the live SSE bar and swaps to its terminal card on completion; error/interrupted
  render from `status` + `error` (NULL `result_json` tolerated). A **scan** card also lists its
  undecodable/read-error **problem files** (paths + reasons, from `scan_problem_files`) in a fixed-height
  `↑/↓`-scrollable section below the count summary. A **probe** card shows its `new_count` (files awaiting
  a scan) or the offline notice.

**The 4-state freshness dot (a color signal, not just a shape).** Each library root shows one dot;
**color, not just shape, carries the meaning** — `◉` is drawn **both green and yellow**. Driven by
`roots.probe_new_count` (see [probe](workflow-scan.md)) + `roots.needs_dedup` + `last_scan_at`/`last_dedup_at`, resolved in this
precedence order (`tui/tokens.status_dot`, a pure `(glyph, role)` function):
1. **`probe_new_count > 0`** → **`◐` grey** — a probe found unscanned files waiting. **Outranks every
   other state, including `never`**: a freshly-registered root whose first probe finds files shows ◐,
   not ○.
2. **no `last_scan_at`** → **`○` grey** — never scanned.
3. **`needs_dedup` OR no `last_dedup_at`** → **`◉` yellow** — has scanned content awaiting a (re-)dedup,
   or was never deduped.
4. **else** → **`◉` green** — scanned AND deduped, nothing dirty since.

**Why an event flag, not a scan-vs-dedup recency test.** Rung 3 keys off the **`needs_dedup` signal**, NOT
`last_dedup_at > last_scan_at`. That comparison was wrong: `last_scan_at = MAX(file_instances.last_seen_at)`
bumps on *every* walked file, so a no-op re-scan (found nothing new) wrongly flipped a fully-deduped root
back to yellow. `needs_dedup` is instead **SET when a scan/merge indexes new dedup-able content** — in the
*same transaction* as the asset write (`scan._persist_new`/`_persist_backfill`, so an interrupted scan
can't strand it; `merge` after registering) — and **CLEARED when a dedup run reaches `completed`** (incl.
the already-clean path). A no-op scan sets nothing (green stays green); an undecodable-only scan and a
reappearing-trash hit aren't dedup-able, so they don't dirty. `last_dedup_at` now only gates "ever
deduped?" in rung 3's OR (which also makes the `needs_dedup=0` retrofit default self-correct — a
scanned-but-never-deduped legacy root still reads yellow).

A completed `scan` zeroes `probe_new_count`, so a scan-latest root skips rung 1; a **found-nothing probe
(count 0) is inherently a dot no-op** — it skips rung 1 and falls through to the scan/dedup rungs, which
a probe never writes. *(Rendering note: the runtime colorizer derives color from the glyph, so `◉`'s two
colors are applied by a small post-pass — `colorize.recolor_root_dots` recolors each root row's dot to its
true role, anchoring on the displayed (elided) name so a long root name still matches, and
`recolor_dot_legend` fixes the legend's two `◉`; the plain/golden frames stay colorless, the role is
asserted via the row `Cell`.)*

## Architecture (how it's built)

A **pure render core + thin Textual widgets** (`src/packrat/tui/`): `tokens` (sizes, glyphs, color
roles, `Theme`), `layout` (CJK-aware `row`/`fit`/`middle_elide` with a `cell_width(row)==width`
invariant), `geometry` (terminal size → layout budgets), `framing` (frame/box composition), `render` +
`screens/*` (pure `dict → line` builders), `data` (relative-time + TUI-side ETA helpers), `nav`,
`colorize`. The Textual layer is `modals`, the `frames/*` package (one Textual screen controller per
screen over a shared `frames.base`, each paired with its `screens/*` builder of the same name), and
`app` (the `PackratApp` + entrypoint). The pure layers import **without** Textual and are tested as
plain strings; the Textual screens each display one pre-composed frame and own only key routing / focus
/ liveness (a light poll timer + the running job's SSE stream — the app drives fetch/subscribe directly,
no separate subscription object). One pure builder lives **outside** `tui/`: `packrat/review_stats.py`
(dedup stage-1/2/3 review stats — compute + line-builders, plus the `stats_for_stage` /
`lines_for_stage` **dispatch** that is the single `stage → compute / stage → builder` map both faces
call, and `thresholds_from_row`, the seam through which both feed a run's analyze-time PDQ-threshold
snapshot) is shared by the TUI Review box AND the CLI `dedup` staging log, so it sits at the top level
where both the jobs layer and the TUI can import it without either depending on the other
([[review-stats-shared-renderer]]).

**Key invariants:**
- **Full-terminal responsive layout.** The frame fills the whole terminal and reflows via a **surplus
  model** — every width/height budget is `reference + surplus` over a 100×24 minimum, so flexible
  columns and lists grow on a larger terminal (assumes ≥100×24; no min-size handling). Long paths
  **middle-elide** in compact rows (drive + leaf kept) and grow when there's room; hint bars **wrap**
  to a second line rather than truncate. CJK/wide characters are measured as 2 cells so alignment holds.
- **Read-safe & CLI-complete (see [design tenets](goals-and-concepts.md)).** Every TUI action maps to an existing CLI verb / daemon submit;
  it issues no privileged operation of its own and there is **no TUI-only action** — both are thin
  clients over the same daemon API (see [architecture](architecture.md)). The CLI is the authoritative surface every capability lands on
  first.
- **Every job is show-able.** Each job writes a uniform `jobs.result_json` at terminal time whatever
  its outcome (see [data model](data-model.md)), so history and the result card always render; the CLI's `status` surfaces the
  actionable slice of the same rows (see [cli](cli.md)).
- **Live.** Queue/running views subscribe to the running job's **SSE stream**; reads poll on light
  timers and re-fetch immediately on the job-finished event. **ETA is computed TUI-side** from the
  observed rate. Keyboard-first: `↑/↓` select, `←/→` page, `Enter` drills in, `Esc` backs out (and
  quits at the dashboard), `Ctrl+Q` quits anywhere. `Ctrl+C` is left unbound so the terminal's
  copy shortcut works.
- **Two poll cadences, single-concern fetches (matches the [architecture](architecture.md) resource API).** The **fast** timer
  refreshes only the live job set (`/jobs/live`); a **slow** timer (+ every job-finished event)
  refreshes the O(collection) stats (`/stats`) + roots (`/roots`) — those only move when a scan/dedup
  completes, so a running scan never re-aggregates the whole collection every tick. The app composes
  these into one read-model dict the pure builders read; each fetch updates only its own keys, so a
  slow stats poll never clobbers a fresher live fetch.
- **Job history is lazy-loaded, never fetched wholesale.** The Queue and root-detail **History**
  sections hold only the *current page* (`limit`/`offset` against `/jobs` or `/roots/{id}/history`)
  plus the true total, so the paginator shows `page K/N` while one window is in memory — history is
  unbounded (no retention, see [open questions #10](roadmap.md)), so it must page. The page **size is the window height**, so a
  terminal resize re-anchors the page index on the absolute offset (keep the first-visible row) and
  refetches only when the height actually changed; a poll refetches history only when the live
  running/queued set changed (a job entered/left = history changed), since terminal history is
  otherwise immutable between finishes.

M6 depends on two M0-runtime pieces (see [architecture](architecture.md) / [data model](data-model.md)): the durable FIFO **queue** and per-job
**`root_id`/`result_json`** columns — so the TUI is a pure presentation layer on top of the runtime.

## NSFW masking (`packrat --nsfw`)

`packrat --nsfw` opens the TUI with adult-content root names/paths **redacted on screen** — a privacy
affordance for screen-sharing or screenshotting a media collection. Matched characters are replaced
with `░` (the `BAR_EMPTY` glyph, so the redacted run reads as a `dim` grey block); everything about it
is scoped to be safe and unsurprising:

- **Display-only.** Actions never change: the read-model `snapshot`/detail/job dicts keep the **true**
  values, so navigation and submits still route on the real name (`open_root` / `submit_scan` /
  `root_path` → Explorer); only what's *drawn* is redacted. It is **not** an API/daemon concern — the
  daemon is shared and client-agnostic (one per machine, see [architecture](architecture.md)), and the TUI needs the real name to act, so
  masking is a per-view presentation toggle applied on the way to the screen.
- **Value-based, not frame-scanning.** Keywords are matched **only** against the live roots' `name` and
  `path` (the two columns the sensitive text originates from, see [scan](workflow-scan.md)) — plus each individual path
  *component* (so an elided path's surviving segment is still caught). Those literal values are then
  replaced by their masked form **wherever they appear**: a root row, a job label (`scan <root>`), a
  review path, a toast, a modal. Because only real root-derived strings are ever rewritten, **app chrome
  can never be corrupted** (a keyword that happens to be a substring of "assets"/"analyze" can't touch
  them — they aren't root values); the worst a mis-chosen keyword can do is over-mask a genuine root, a
  cosmetic effect, never a leak.
- **Masked pre-layout (elision-safe).** The screens render through `app.view(...)`, a masked deep-copy of
  the read model, so a keyword is redacted **before** `middle_elide` can split it across a `…`. This is
  what closes the elision leak a post-layout scan can't (once `PornCollection` is elided to `Porn…tion`,
  the literal value no longer matches). A post-layout pass on the composed frame remains as a backstop
  for any inline text that bypasses a builder; on a fully pre-masked frame it changes nothing. Toasts
  (`notify`) and modal insets are masked the same value-based way.
- **Width-preserving.** Each masked character emits `░` repeated to its display width (a CJK char → two
  `░`), so a redacted 100×24 frame stays byte-aligned (the same `cell_width` invariant the Architecture section above relies on) —
  and pre-layout masking is width-neutral, so elision cuts a `░`-run identically to the real text.
- **Cheap.** The `(value, masked)` redaction pairs are derived from the roots once and **memoized**
  against a signature of their name/path values, so the keyword scan re-runs only when the roots change —
  not on every keypress, poll, or logo-animation tick.

Keyword list (English + Chinese) and the pure helpers (`mask_text` / `sensitive_tokens` /
`build_redactions` / `redact` / `mask_obj`) live in `src/packrat/tui/nsfw.py`.
