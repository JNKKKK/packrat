# packrat — TODO

Working notes for three planned changes. File/line refs are anchors at the time of
writing (2026-07-22) — re-grep before editing. Each item lists the behavior, the code
touch-points, and open questions.

---

## 1. Dashboard: give Roots more space, Queue less

**What:** On the main dashboard, the [R]oots box and [Q]ueue box split the vertical
space left below the fixed top section. Currently they split roughly 50/50 (Roots gets
the odd row). Rebalance so Roots gets more rows and Queue fewer.

**Where:** `src/packrat/tui/geometry.py:120-130` — the split lives entirely here:

```python
@property
def _dash_split(self) -> int:           # combined interior rows (ref 9)
    return max(2, self.content_rows - self.TOP_ROWS - 5)

@property
def dash_roots_rows(self) -> int:
    return (self._dash_split + 1) // 2   # ref 5 — Roots currently gets the odd row

@property
def dash_queue_rows(self) -> int:
    return self._dash_split - self.dash_roots_rows   # ref 4
```

**Decided ratio: Roots:Queue = 3:1** (Queue gets ~¼, Roots the remainder). Note the
combined interior `_dash_split` is what's left after the *fixed* parts — the top section
(TOP_ROWS=8 = logo + Collection box), the footer, and the two boxes' own chrome (−5:
Roots 2 borders + 1 DOTKEY line, Queue 2 borders). At reference `_dash_split == 9`, which
isn't divisible by 4, so 3:1 rounds to **Roots 7 / Queue 2** (remainder → Roots).

**Change:**

```python
@property
def dash_queue_rows(self) -> int:
    return max(2, (self._dash_split + 2) // 4)   # ≈ split/4, floor 2 (running bar + 1 row)

@property
def dash_roots_rows(self) -> int:
    return self._dash_split - self.dash_queue_rows   # Roots gets the 3/4 remainder
```

`dash_roots_rows + dash_queue_rows == _dash_split` still holds exactly (the pager/paging
math in `frames/dashboard.py` `action_page`/`action_move` reads both). The `max(2, …)`
floor keeps Queue showing the running bar + one row on the shortest allowed terminal.

**Tests / checks:**
- Reference split changes **5/4 → 7/2**, so the dashboard golden frames are pinned at
  100×24 (`geometry.py` docstring) and must be **regenerated**; confirm the new
  byte-frames read right.
- Verify at reference size and one larger size that the boxes still sum to the frame
  height (no overflow / no gap) and Queue stays ~¼ as the terminal grows.
- Queue at 2 interior rows leans harder on the unfocused `… N more` truncation — expected
  (full backlog is in the maximized §4 view).

---

## 2. `dedup --prefer-internal` (stage 1 survivor + stage 2 tie-break)

**What:** a new dedup option that flips who wins when an internal copy and an external
copy are the same/indistinguishable asset.

- **Today (no flag):** external is treated as the master.
  - *Stage 1 (exact):* when an asset has copies in the target root **and** an external
    root, the external copy is the survivor and the internal copies are deleted
    (`reason="exact-external"`).
  - *Stage 2 (keep-lead tie):* when the ranking key fully ties, the lead falls to the
    stable smallest-normcase-path tiebreak — with no regard to internal/external.
- **With `--prefer-internal`:** internal is the master.
  - *Stage 1:* delete the **external** copy by default; keep the internal one.
  - *Stage 2 tie:* see the new tiebreak rule below.

**New stage-2 tie rule (both photo and video), replacing the bare path tiebreak:**
1. If **all** tied members are in the target (internal) root → compare path (current
   behavior, unchanged).
2. If the tied members are **mixed** internal/external:
   - **without `--prefer-internal`** → suggest keeping the **external** copy.
   - **with `--prefer-internal`** → suggest keeping the **internal** copy.
   (If still ambiguous within the chosen side — e.g. two externals — fall back to the
   path tiebreak among that side.)

This only changes the *final* tiebreak; resolution / format / bitrate / codec keys are
untouched, so `--prefer-internal` never overrides a genuine quality signal, only the
coin-flip at the bottom.

### Persistence: `--prefer-internal` is a RUN-WIDE policy, persisted on the run

**Decided:** the flag is set once when the run opens (analyze) and **carries over to all
subsequent `--confirm`s automatically** — it is NOT re-read from each command's params.

**Why this is correctness, not just UX:** a dedup run is a single `review_runs` row
spanning all 3 stages (schema.py:136-140). The two halves of the flag fire at *different*
commands:
- Stage 1 (exact survivor) is planned in `_analyze` — the opening `dedup <root>`
  (dedup.py:190).
- Stage 2 (keep-lead tie) is computed when stage 2 is *staged*, which in the normal flow
  is the **`--confirm` that advances stage 1 → stage 2** (dedup.py:289-293), NOT analyze.

So if the flag were read per-command (the way `--keep-suggested` is, dedup.py:241), then
`dedup <root> --prefer-internal` then a bare `dedup <root> --confirm` would apply the
preference to stage 1 but **silently drop it for stage 2**. It must be persisted.

**Mechanism:**
1. Add column `prefer_internal INTEGER NOT NULL DEFAULT 0` to `review_runs`
   (schema.py:142); set it in the analyze-time INSERT (dedup.py:208-214). (The
   "already clean" fast-path at dedup.py:192-205 opens no run — flag is moot there.)
2. Stage-1 planning **and** stage-2 staging read `prefer_internal` **from the run row**,
   not from `ctx.params`. A bare `--confirm` then applies the policy the run opened with.
3. This also blocks an *inconsistent* run: stage-1 exact deletes already happened (to the
   recycle bin), so the policy must be locked for the whole run — flipping it mid-run
   would leave stage 1 keeping externals while stage 2 suggests internals.

**Collision — `--prefer-internal` passed on a later `--confirm`/`--cancel`:**
- matches the run's stored policy → accept silently (forgiving of a user who retypes it
  each step);
- conflicts (flip attempt) → **error** with "preference is fixed when the run opens;
  `--cancel` and re-run to change it" (mirrors the `--keep-suggested` wrong-context error
  at dedup.py:242-247).

Note: `--keep-suggested` stays per-confirm (it only affects that confirm's apply step and
never needs to survive) — do NOT persist it. Only `--prefer-internal` is run-scoped.

### Touch-points

- **CLI** — `src/packrat/cli/main.py:495` `dedup()`: add
  `prefer_internal: bool = typer.Option(False, "--prefer-internal", help=...)`; thread
  into `client.submit_dedup(...)`; fold into the `label` string (main.py:525) so the
  job card reads e.g. `dedup --prefer-internal`.
- **Daemon client** — `src/packrat/daemon/client.py:159` `submit_dedup`: add
  `prefer_internal: bool = False` param → include in the posted body.
- **Daemon server** — `src/packrat/daemon/server.py:328` `submit_dedup`: read
  `"prefer_internal": bool(body.get("prefer_internal"))` into the job params (next to
  `keep_suggested` at server.py:344).
- **Schema** — `src/packrat/db/schema.py:142` `review_runs`: add
  `prefer_internal INTEGER NOT NULL DEFAULT 0` (see Persistence section above).
- **Job** — `src/packrat/jobs/dedup.py`:
  - `_analyze` (dedup.py:208-214): read `bool(ctx.params.get("prefer_internal"))` and
    store it in the run-opening INSERT. Everything downstream reads it **from the run
    row**, not `ctx.params`.
  - `_confirm`/`_cancel` (dedup.py:223, 299): validate any `--prefer-internal` on the
    command against the run's stored value (silent if equal, error if conflicting — see
    Collision above).
  - **Stage 1** `_plan_exact` (dedup.py:352-387): today `if external:` picks the
    external survivor and deletes internal. When `prefer_internal`, instead pick an
    **internal** survivor (reuse the all-internal keeper logic: sort internal by
    `(mtime, normcase path)`) and delete the external copies (reason e.g.
    `"exact-internal-preferred"`). Make sure `is_external`/`external_deleted` stats and
    the network-permanent-delete warning still tally correctly (deleting an external
    copy may be a network delete — see [[review-network-count]]).
  - **Stage 2** thread `prefer_internal` + the target `root_id` into the keep-lead call
    (`_group_lead_and_level` is invoked from the perceptual planner — trace from
    dedup.py:509-522 staging).
- **Ranking** — `src/packrat/jobs/dedup_rank.py`:
  - `_group_lead_and_level` (dedup_rank.py:88-125): the tiebreak at lines 111-114 and
    the level-labeling loop need internal/external awareness. Options: pass
    `root_id` + `prefer_internal` in, and when the top-key ties, break by
    `(is_internal_desired, path)` where `is_internal_desired` depends on the flag.
    `is_external` per member = `inst["root_id"] != root_id` (same test dedup.py uses at
    509/518).
  - Keep the module **pure** (no DB/FS) — pass `root_id`/`prefer_internal` as args, do
    not import anything stateful. It's unit-tested in isolation
    (`tests/test_video_lead.py`).
  - Add a new decision-level label for "decided by internal/external preference" vs
    "path tiebreak" so the stage-2 lead-pick stats (dedup.py:959 tally) explain it.
    Both `_PHOTO_LEAD_LEVELS` and `_VIDEO_LEAD_LEVELS` + `_PATH_TIEBREAK` feed that
    ordered list.
- **Job label** — `src/packrat/jobs/labels.py:58`: consider surfacing `prefer-internal`
  in the params label alongside `keep-suggested`.

### TUI — prompt on `[d] dedup` in the root-detail screen

- **Where:** `src/packrat/tui/frames/rootdetail.py:250` `action_dedup` — today it
  submits immediately (`self.app.client.submit_dedup(root)`).
- **Change:** push a small popup first that lets the user choose the master preference,
  then submit with the chosen flag.
- **DECIDED — reuse `ChoiceModal`** (`src/packrat/tui/modals.py:233`) with its ↑/↓ + Enter
  + Esc list (NOT a Tab-navigable two-box variant). Small extension needed: `ChoiceModal`
  currently renders *only* the options list as its message (modals.py:250-255) — add an
  optional **prompt line** above the options so the question sentence has somewhere to
  live (the box title alone is too short for the chosen wording). Default cursor on the
  first option = the default (prefer-external) behavior, so a plain Enter keeps today's
  behavior.
- **Wording — DECIDED (variant C):**
  Title: `dedup — which copy is the master?`
  Prompt line: `On exact dups + tie-break suggestions across roots:`
  - `Prefer external (keep the other root's copy)`   ← default cursor (today's behavior)
  - `Prefer internal (keep this root's copy)`
  Cursor index 0 → `prefer_internal=False`; index 1 → `prefer_internal=True`.
- Map the choice → `submit_dedup(root, prefer_internal=<bool>)`.
- **CLI/TUI parity** ([[cli-tui-parity]]): the modal only *gathers* the flag; the actual
  action is still the `packrat dedup … --prefer-internal` verb. Keep the "ask, then act"
  shape (modals.py docstring) so the parity tenet holds.

### Tests

- `tests/test_video_lead.py`: add mixed internal/external tie cases for photo **and**
  video — default → external wins, `--prefer-internal` → internal wins; all-internal →
  path tiebreak unchanged. Update the new decision-level label assertions.
- `tests/test_dedup.py`: stage-1 exact — assert external deleted (not internal) under
  the flag; assert `is_external`/`external_deleted` counts.
- CLI/daemon: `--prefer-internal` plumbs through to job params.

**Resolved open questions:**
1. Modal UX → **↑/↓ list** (reuse `ChoiceModal`, add a prompt line). See TUI section.
2. Exact wording → **DECIDED (variant C)** — see the TUI section above.
3. No external overlap → **silently accept** (no-op). The TUI popup always asks, and the
   user can't know in advance whether a given root has cross-root copies; erroring would
   be noise. The flag simply has nothing to flip when a group has no external member.
4. Reason string for the flipped stage-1 delete → **`"exact-internal-preferred"`**
   (survivor is internal, external copy deleted by preference). Distinct from the two
   existing reasons, which name where the *survivor* is: `"exact-external"` (default —
   survivor external, internal deleted) and `"exact-internal"` (all-internal group —
   one internal kept, other internals deleted). The report/manifest (dedup.py:968-974,
   external/network tallies) must treat `exact-internal-preferred` deletes as **external
   deletions** for the network-permanent-delete warning ([[review-network-count]]).
- Persistence → run-scoped, stored on `review_runs`; carries across confirms (see the
  Persistence section).


**Tests (persistence):** `dedup <root> --prefer-internal` then bare `dedup <root>
--confirm` → stage-2 leads follow the internal preference (proves it's read from the run,
not the confirm's params). Conflicting flag on `--confirm` → errors.

---

## 3. Review box: more dedup metrics + scrollable, RESPONSIVE height (review:jobs ≤ 1:1)

**What:** the root-detail Review box should show more metrics for dedup runs, and its
height should adapt **responsively** — NOT a fixed 10-row cap. Instead, cap it by a ratio
against the Jobs panel, the way the dashboard caps Roots vs Queue:
- **Max ratio review:jobs = 1:1.** The Review box never takes more than half the shared
  interior; Jobs always gets ≥ half. On larger terminals the cap grows automatically.
- **Review shrinks to its content.** If the review has fewer lines than the cap, the box
  shrinks to exactly that many rows and **Jobs absorbs the freed rows** (unlike the
  dashboard, where both boxes always fill — here Review is content-driven and Jobs
  backfills the slack).
- If content exceeds the cap → clamp to the cap and scroll with ↑/↓.

**The shared pool (mirror of the dashboard `_dash_split` idea).** The §3 body stacks
(rootdetail.py:73-91): `1 spacer + ICON_H(5) + 1 spacer + (review_interior + 2 borders)
+ (jobs_interior + 2 borders) = content_rows`. So the two interiors share:

```
S = content_rows − 11            # ref: 21 − 11 = 10  (the "_detail_split")
review_cap = S // 2              # 1:1 max; odd row → Jobs (favor history), ref 5
review_interior = min(review_content_lines, review_cap)
jobs_interior   = S − review_interior          # Jobs backfills the slack
```

Result: `review ≤ S/2 ≤ jobs` always; ratio reaches 1:1 only when review content ≥ cap
(then it scrolls). The fixed `10` is GONE — ref cap is 5, and it grows with height (e.g.
h=40 → content_rows≈37 → S≈26 → cap≈13). The calm (no-pending-review) case still
collapses to 1 row (`review_content_lines == 1`, well under the cap).

**Where:**
- Height: `src/packrat/tui/screens/rootdetail.py:47-56` — replace `REVIEW_ROWS = 4` /
  `_review_rows(d)` with the ratio math above. `_review_rows` now needs `geo` (to read
  `content_rows`) AND the true review content-line count — so compute the review lines
  first, then clamp. Expose the true content length so the frame can scroll.
- Content: `_review_box` (rootdetail.py:239) and `_review_lines` (rootdetail.py:269).
  The counts dict is **shaped by run_type** (dedup vs cleanup — see
  [[review-counts-shape]]); the dedup branch (rootdetail.py:289-297) is where new
  metrics go.
- **Lockstep (critical):** `_panel_interior` (rootdetail.py:83-91) computes the Jobs
  panel height from `_review_rows(d)`; its `header_rows = 1 + ICON_H + 1 +
  (_review_rows + 2)` and `jobs = content_rows − header_rows − 2`. With the new math this
  becomes exactly `jobs_interior = S − review_interior` — same number, but the two MUST be
  derived from ONE shared helper (e.g. `_detail_split(d, geo) → (review_interior,
  jobs_interior)`) so review's clamp and jobs's backfill can't drift. The docstring
  already warns about this coupling — keep it true.
- Scroll state + keys: `src/packrat/tui/frames/rootdetail.py` — the Review box is
  focusable (`action_focus_review`, rootdetail.py:177; focus == "review"). Add a scroll
  offset on the frame screen and route ↑/↓ to it **when Review is focused** (the existing
  up/down bindings currently serve the Jobs panel — check the navigation split around
  rootdetail.py:127-143). Only scroll when `review_content_lines > review_cap`.
- **DECIDED — scroll indicator: copy the scan job card's problem-file pattern**
  (`src/packrat/tui/screens/jobcard.py:136-157`, `_problem_section`). It shows a header
  with the total + the visible range, right-aligned: `↑/↓  {start+1}–{end} of {n}` when
  it overflows, else `{n} file(s)`. Mirror that: a Review-box header line with a
  right-aligned `↑/↓ 1–{cap} of {n}` when `n > cap`, plain otherwise (`budget = review_cap`,
  not a fixed 10). Reuse the same
  windowing shape (`start = clamp(scroll, 0, n-budget); window = lines[start:start+budget]`),
  which `_problem_section` already models — simpler than the dashboard `fit(mode="scroll")`
  pager and it's the established scroll idiom for a fixed box in this app.
- **DECIDED — the ratio cap applies to ALL run_types** (dedup + cleanup), not dedup-only.
  The `min(content, review_cap)` clamp is universal, and the calm (no-pending-review) case
  still collapses to 1 row.

**Metrics to add:**

- **Stage 1 (exact dup) — internal vs external delete split.** Show how many files are
  slated for deletion broken down by internal (inside the target root) vs external
  (another root). E.g.:
  > `to delete (exact): 12  ·  8 internal, 4 external`

  **Data:** `review_actions.is_external` already exists and is per-action (marks whether
  the *deleted* file is outside the target root — set for stage 2 at dedup.py:518).
  - `queries.py` `_review_counts` (queries.py:428-444): add `is_external` to the SELECT,
    and in the dedup branch split the exact tally into
    `to_delete_exact_internal` / `to_delete_exact_external` (keep `to_delete_exact` as the
    total for back-compat). Counts shape is run_type + stage dependent
    ([[review-counts-shape]]) — this only extends the dedup stage-1 shape.
  - Display: the dedup branch of `_review_lines` (rootdetail.py:289) — add the split to
    the stage-1 line.

  **Terminology:** `review_actions` are per-**file** (one asset with 3 internal copies =
  3 delete actions), so this counts *files to delete*, not unique assets. Label it "files"
  / "to delete", not "assets", to avoid implying a per-asset count.

  **Interlock with item 2:** today `_exact_action` hardcodes `is_external=False`
  (dedup.py:396) because stage 1 currently only ever deletes *internal* copies (external
  is always the survivor) — so **external will read 0 until item 2 lands**. That's still
  accurate ("0 external will be touched"). Item 2's stage-1 change MUST set
  `is_external=True` on the flipped external deletes for this split to be correct once
  `--prefer-internal` exists. The split is what makes the flag's effect visible.

- **Stage 2 (recompression) — keep-lead pick stats + PDQ distance + internal/external
  suggestion breakdown.** Three groups of metrics:

  **(a) Keep-lead pick reasons** — the same tally the CLI logs (dedup.py:949-964
  `_report_lead_stats`): how many group leads were decided by each ranking level
  (`resolution`, `resolution + format`, `resolution + format + size` for photo; the
  bitrate/codec/`fine bitrate` analogues for video; `path tiebreak`). One row per level
  that has ≥1 group, ordered best-decision-first (same `ordered` list as dedup.py:959).

  **SPLIT BY MEDIUM — photos + videos in two side-by-side columns** (DECIDED). Photos
  left, videos right, under one `keep-lead decided by:` header (see mock). Consequence:
  photo and video **share two label strings** — `"resolution"` (both) and `_PATH_TIEBREAK`
  (both) — so the current flat `lead_levels` dict `{label: count}` (dedup.py:486-496)
  COLLAPSES them. To split, the tally must key by **(media_type, label)**, not label
  alone. Groups are homogeneous (a photo never matches a video, dedup_rank.py:3), so every
  group is cleanly one medium — the split is well-defined.
  - Getting medium into `_review_counts`: `review_actions` has no `media_type` column, so
    either (a) JOIN `review_actions.asset_id → assets.media_type` at poll time (clean, no
    redundant column, authoritative — a pending stage-2 review hasn't deleted its members),
    or (b) persist a `media_type` column alongside `is_lead`/`lead_reason`. Prefer (a)
    unless the JOIN complicates the count query.
  - **Video labels abbreviate** on the narrow half-width column (the full
    `resolution + bitrate + codec + fine bitrate` = 43 chars won't fit ~38 cols): show
    `resolution`, `+ bitrate`, `+ bitrate + codec`, `+ fine bitrate`, `path tiebreak` (the
    shared `resolution` prefix implied). Photo side similarly: `resolution`, `+ format`,
    `+ format + size`, `path tiebreak`.
  - All-photo or all-video collection → the empty column is **omitted** (DECIDED); the
    remaining column (and the histogram) may widen into the freed space.

  **(b) PDQ distance histogram** — a distribution over the near-dup members' `distance`
  (the `review_actions.distance` column, already persisted, schema.py:185).
  - **Layout: to the RIGHT of the two keep-lead columns** (DECIDED), aligned row-for-row
    with them — the histogram title sits on the `keep-lead decided by:` line, the bins on
    the rows below (see mock). This reuses the blank space beside the columns, so it costs
    **no extra rows** vs. a standalone PDQ line.
  - **Responsive fallback:** if the box interior is too narrow to fit both columns + the
    histogram (`interior < two_col_w + GAP + MIN_HIST_W`, ~72 cols), drop the histogram to
    a single full-width line **below** the keep-lead section instead. NOTE: the terminal is
    guaranteed ≥100 wide (geometry.py), so the box interior is always ≥92 — side-by-side is
    effectively always used; the below-fallback is defensive (future narrow/split render).
  - **Bins are THRESHOLD-DERIVED per stage (SHIPPED, supersedes the fixed scheme below).**
    `review_stats._pdq_bins(stage, t_rec, t_edit, t_video)`: stage-2 photos = even thirds
    of `0..t_rec` (→ `0–2 · 3–6 · 7–10`) + video on its own scale `t_rec+1..t_video` + an
    open overflow (`11–50 · 51–90 · 91+`, since the video mean-Hamming can exceed
    `t_match_video`); stage-3 photos = even thirds of `t_rec+1..t_edit` (→ `11–17 · 18–24 ·
    25–32`). Also added: stage-1 + stage-3 group make-up, stage-3 histogram. CLI passes
    live `ctx.config.match` thresholds; the read-only TUI poll uses the config defaults.
    (The older fixed `0–2 · 3–5 · 6–10 · 11+` scheme in the mocks below is historical.)

  **(c) Internal/external suggestion breakdown** — the user's requested framing:
  - how many groups are **all-internal** (every member in the target root);
  - how many groups are **mixed** (contain both internal and external members);
  - and among the mixed groups, how many **suggest keeping external** vs **suggest
    keeping internal**.
  (All-internal groups always suggest an internal lead, so they need no external/internal
  split — only the mixed groups do. This is exactly the set `--prefer-internal` flips.)

  **DATA GAP — two new `review_actions` columns needed.** `is_lead` and `lead_reason` are
  computed at staging (dedup.py:492-496, `_group_lead_and_level`) but **only written to
  `manifest.csv` + `proposed.json`, NOT to the `review_actions` table** (schema.py:169-187
  has no such columns). The review box reads counts from `review_actions` via
  `_review_counts`, and re-running the keep-lead ranking inside a read-only TUI poll would
  violate the lazy/read-only snapshot design (§8 B; queries.py:416-420). So **persist
  them**:
  - Add `is_lead INTEGER` and `lead_reason TEXT` to `review_actions` (schema.py:169-187).
  - Write them in the staging INSERT (dedup.py:692-701) from the action dict (the plan
    already carries `is_lead`/`lead_reason` at dedup.py:522).
  - Then `_review_counts` (queries.py:428-444) can compute all of (a)/(b)/(c) from the
    stage-2 rows: (a) = tally of `lead_reason` where `is_lead=1`, **keyed by
    (media_type, lead_reason)** for the photo/video split (JOIN to `assets.media_type`);
    (c) = per `group_no`, check whether any member has `is_external=1` (mixed) vs none
    (all-internal), and for mixed groups read the lead member's `is_external` to bucket
    suggest-external vs suggest-internal. `is_external` + `group_no` + `distance` are all
    already in the table.

  **One source for CLI + TUI:** these stats feed BOTH the TUI Review box and the CLI
  staging log — computed once and rendered by one shared line-builder so they can't drift.
  See "Unify CLI stage-2 stats onto the TUI Review renderer" below.

#### MOCK — review box content (for review)

The box is inside the root-detail frame (content width ~96). `▸` marks the focused box.
Header carries the scroll position (scan-card pattern). These are illustrative numbers.

**Stage 1 (exact dup) — 4 content rows, no scroll:**

```
╭─ Review ─────────────────────────────────────────────────────────  4 rows ─╮
│  dedup · stage 1 of 3 — exact duplicates (default DELETE)                    │
│  to delete (exact): 12 files  ·  8 internal, 4 external                      │
│  ⚠ 4 on a network share — permanent (not recycled)                           │
│  [o] open in Explorer   [g] confirm   [k] cancel                             │
╰─────────────────────────────────────────────────────────────────────────────╯
```

**Stage 2 (recompression) — 12 content rows (box FOCUSED).** *This mock assumes a taller
terminal where `review_cap ≈ 10` (e.g. h≈34); at the 100×24 reference the cap is 5, so the
same content would clamp to 5 and scroll. The cap is the ratio, not a constant.*

```
╭─ Review ─────────────────────────────────────────────────  ↑/↓  1–10 of 12 ─╮
│  dedup · stage 2 of 3 — recompression (default KEEP)                         │
│  near-dup groups: 37   ·   members: 91                                       │
│  keep-lead decided by:                          PDQ distance (91):           │
│    photos (32)             videos (5)             0–2  ████████████ 41       │
│      18 · resolution         3 · resolution       3–5  ███████ 25            │
│       9 · + format           1 · + bitrate+codec  6–10 █████ 18              │
│       5 · + format + size    1 · + fine bitrate   11+  ██ 7                  │
│       2 · path tiebreak      0 · path tiebreak                               │
│  group make-up:  29 all-internal · 8 mixed (internal+external)               │
╰─────────────────────────────────────────────────────────────────────────────╯
   (scroll down for: suggestion split + keep-suggested tip + actions)
```

rows 11–12, revealed by ↓:

```
│    of the 8 mixed groups →  5 suggest external · 3 suggest internal          │
│  tip: [b] confirm --keep-suggested — deletes 54 non-leads (6 on network ⚠)   │
│  [o] open in Explorer   [g] confirm   [k] cancel                             │
```

**Narrow fallback (defensive — box interior < ~72 cols): histogram drops to its own
full-width multi-row block BELOW** the keep-lead columns (a complete vertical histogram,
same bins as the side version — NOT a compressed one-liner):

```
│  keep-lead decided by:                                     │
│    photos (32)             videos (5)                      │
│      18 · resolution         3 · resolution                │
│       9 · + format           1 · + bitrate + codec         │
│       5 · + format + size    1 · + fine bitrate            │
│       2 · path tiebreak      0 · path tiebreak             │
│  PDQ distance (91):                                        │
│    0–2  ████████████████ 41                                │
│    3–5  █████████ 25                                       │
│    6–10 ███████ 18                                         │
│    11+  ██ 7                                               │
```

(This block is taller than the side-by-side layout, so on a narrow box it pushes more
content past the `review_cap` → scroll. Acceptable: the ≥100-col guarantee means this
branch effectively never fires; it exists so a future narrow/split render degrades
cleanly instead of clipping a half-drawn chart.)

**LAZY-LIVENESS — the stage-2 network warning ([[review-network-count]]).** Stage 2 is
default-**KEEP**: nothing is in the default delete set unless the user removes shortcuts,
and the read-only poll **must not stat on-disk shortcuts** (SMB cost is exactly what
lazy-liveness avoids, queries.py:416-420). So:
- Do **NOT** print a "⚠ N members on a network share — permanent if deleted" line for
  stage 2 as if those N are being deleted. `counts.network` for a default-KEEP stage is
  **0 when unedited**; the authoritative, shortcut-accurate permanent-delete warning is
  the **confirm job** (§8 B Phase 6), which re-reads shortcut presence — not the poll.
- The ONE stage-2 delete set that is deterministic without a stat is **`--keep-suggested`**
  (`[b]`): it deletes exactly the non-lead members, fully determined by the persisted
  `is_lead` column. `is_network_path(path)` (fsutil.py:84) over the non-lead rows is safe
  here: it classifies the **drive/share**, not the file — a UNC path is a string prefix
  test, and a mapped drive (`Z:\`) is a local `GetDriveTypeW(drive_root)` syscall
  (DRIVE_REMOTE). Neither touches the file or makes a network round-trip, so it is NOT the
  per-file SMB stat lazy-liveness prohibits (this is exactly how the existing
  `_review_counts` already computes `network`, queries.py:437-438). Memoize by drive root
  if the set is large. Surface network exposure THERE (folded into the `[b]` tip, as in
  the mock), scoped to "if you keep-suggested", not as a standalone default warning.
- Stage 1 (default-DELETE) keeps its `⚠ … on a network share — permanent` line — there the
  exact dups genuinely ARE the default delete set (unchanged, correct).

**Calm (no pending review) — collapses to 1 row (unchanged behavior):**

```
╭─ Review ──────────────────────────────────────────────────────────────────╮
│  no pending review — run [d] dedup or [c] clean up                          │
╰─────────────────────────────────────────────────────────────────────────────╯
```

*(Resolved: header scroll label → right-aligned in the title row like the dashboard pager,
per the DECIDED note below. Sub-tally indentation → follow the mock as drawn — the shared
line-builder owns it and the golden frames pin it, so it's not a loose choice.)*

**Tests:** golden frames for the root-detail screen at (a) content < cap (shrunk box —
Jobs backfills), (b) content == cap (1:1), (c) content > cap (clamped + scroll indicator),
AND at ≥2 terminal heights (ref 24 → cap 5; a taller size → larger cap) to prove the cap
is the ratio, not a constant. Verify `review_interior + jobs_interior == S` in every case
(the lockstep), and that the calm case is 1 row with Jobs taking `S − 1`.

**Resolved open questions:**
1. Metrics → stage-1 internal/external delete split + stage-2 lead-reason tally / PDQ
   distance / internal-external group make-up + suggestion split — all DECIDED above.
2. Scroll indicator → **copy the scan job card's `_problem_section` pattern**
   (`↑/↓ start–end of n` header, right-aligned).
3. Height cap → **responsive ratio, review:jobs ≤ 1:1** (NOT a fixed 10 rows); review
   shrinks to content and Jobs backfills the slack. Applies to ALL run_types. See the
   shared-pool math at the top of item 3.

**Formerly-open, now all DECIDED:**
- PDQ histogram — right of the keep-lead columns; below-fallback when narrow;
  bins `0–2 · 3–5 · 6–10 · 11+`.
- **DECIDED — empty-medium column is OMITTED** (not `videos (0)`). An all-photo collection
  shows only the photos column (and the histogram can widen into the freed space); an
  all-video collection shows only videos. If BOTH exist, both columns show.
- **DECIDED — header scroll label placement:** the `↑/↓ 1–{cap} of {n}` label goes in the
  box **title row, right-aligned**, exactly where the dashboard boxes put their `page i/N`
  pager (`box(..., right=<label>)`, see `screens/dashboard.py:63,74`). Consistent with the
  rest of the app.
- (Resolved: stage-2 network warning — no standalone default-KEEP warning; only the
  deterministic `--keep-suggested` non-lead set gets a network count. See lazy-liveness
  note above.)

**DECIDED — make the CLI stats output MATCH the TUI Review section, from ONE source.**
(Simplification of the former "share a helper" open item.) Instead of a shared *ordering*
helper feeding two hand-written renderers, collapse to **one stats computation + one
line-builder** that both faces use. See below.

#### Unify CLI stage-2 stats onto the TUI Review renderer

**Why this is now trivial (staging order).** `_stage_and_pause` (dedup.py:591) calls
`_materialize` (persists the `review_actions` rows, line 601) **before** `_report_staged`
prints (line 620). So by the time the CLI logs its stats, the SAME rows the TUI polls are
already in the DB. There is no "live dict vs persisted rows" divergence to reconcile — the
CLI can read the identical persisted data.

**The plan — one compute, one render, two callers:**
1. **One stats function.** Extend `_review_counts` (queries.py) — or a sibling it calls —
   to produce the full stage-2 stats bundle: `{groups, members, lead_levels_by_medium,
   pdq_histogram, group_makeup, suggestion_split}`. This is the single source of truth,
   computed from `review_actions` (+ the `assets.media_type` JOIN).
2. **One line-builder.** The pure `_review_lines` (rootdetail.py:269) already turns a
   counts bundle into display lines. Factor the stage-2 body (the two keep-lead columns +
   PDQ histogram + make-up + suggestion split, exactly as in the MOCK above) into a pure
   helper that returns `list[str]`.
3. **TUI** renders those lines in the Review box (with the responsive height + scroll).
4. **CLI** `_report_staged` / `_report_lead_stats` (dedup.py:929-964) prints the SAME
   lines (minus the box border) to the staging log. `_report_lead_stats`'s old inline
   `ordered` list (dedup.py:959) and flat `lead_levels` dict are **deleted** — the CLI now
   shows the photo/video split, PDQ histogram, and internal/external make-up too, matching
   the mock.

**Net:** the keep-lead breakdown is written and ordered in exactly ONE place; the CLI log
and the TUI box are the same text by construction, so they can't drift. Adding/reordering
a ranking level or rewording a label is a single-site change. Dovetails with the
[[m6-tui-architecture]] pure-render-layer + golden-frame anti-drift approach (the shared
line-builder is golden-testable, and a CLI snapshot test can assert the log matches).

*Consequence to accept:* the CLI staging log gets **richer** (currently just the flat
lead-level tally) — it now prints the columns/histogram/make-up. That's the point (parity,
[[cli-tui-parity]]), but it's a visible change to the CLI output; update any test that
pins the old `keep-lead picks (N group(s)) — decided by:` format.

---

### Cross-cutting notes
- CLI/TUI parity is a design tenet ([[cli-tui-parity]]): every TUI action stays a
  first-class CLI verb. Item 2's modal only gathers the flag.
- The keep-lead ranking module (`dedup_rank.py`) is deliberately pure and unit-tested in
  isolation — keep DB/FS out of it; pass `root_id`/`prefer_internal` as plain args.
