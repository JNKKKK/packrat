# TODO

## Centralize the dedup review-stats stage dispatch (§8 B)

**Problem.** `review_stats.py` exists so the CLI staging log and the TUI Review box
render from *one* compute + *one* line-builder and "can't drift" (its module docstring).
That guarantee holds **per-builder** but not for the **dispatch that picks** the builder —
the `stage → which compute fn / which line-builder` ladder is hand-written in three places:

- `queries.py:_review_counts` (≈456-462) — the read-only TUI **poll**: `stage → compute`,
  stashing under `out["stage1"|"stage2"|"stage3"]`.
- `jobs/dedup.py:_report_review_stats` — the CLI **staging log**: `stage → compute + build`.
- `tui/screens/rootdetail.py:_review_lines` (≈350-369) — the TUI **render**: `stage → build`
  over the bundle the poll already stashed.

Drift just moved up a level: a new stage / renamed bundle key / changed `stage=` arg means
editing three ladders in lockstep. The stage-3 work (commits `2a55d54`, `1259f99`, `8d5555d`)
is the proof — the stage-3 arm had to be added to all three sites in the same change; a miss
in any one is a silent divergence, exactly what the module was built to prevent. The
threshold dual-source issue below is a symptom (two compute-dispatch sites → two threshold
decisions).

**Fix.** Push the dispatch into the shared module; each face calls one entry point:

```python
# review_stats.py
def stats_for_stage(rows, stage, *, thresholds, is_network) -> dict:
    """stage → the right bundle. The ONE place the stage→compute map lives."""

def lines_for_stage(bundle, stage, width, *, keep_suggested=True) -> list[str]:
    """stage → the right line list, from a bundle stats_for_stage produced."""
```

Then:
- `queries.py` calls `stats_for_stage` (passing default thresholds — poll is read-only).
- `rootdetail.py` calls `lines_for_stage`.
- `dedup.py` calls both (live `ctx.config.match` thresholds; `keep_suggested=False`, since the
  CLI prints its own `--confirm --keep-suggested` tip).

Three ladders collapse to two functions with one definition each.

**Deliberate limit — stays two entry points, not one.** The TUI computes at poll time and
builds at render time, on opposite sides of the poll→DB(JSON)→render boundary; the bundle is
serialized between them. So compute and build cannot fuse into a single `render_stage(rows,
stage)`. This is inherent (a pure CLI could fuse; the TUI can't) — the dispatch centralizes,
the two-phase split stays. Rated a *secondary* altitude fix for this reason.

**Scope / steps.**
1. Add `stats_for_stage` + `lines_for_stage` to `review_stats.py`; keep the per-stage
   `stageN_*` functions as their implementation (no behavior change).
2. Rewire the three call sites to the two entry points.
3. Consider renaming `stage2_stats` → `perceptual_stats` (it already produces the stage-3
   bundle via `stage=3`); cosmetic, only worth doing alongside this.
4. Update tests that call the per-stage fns directly if the rename lands
   (`tests/test_review_stats.py`, `tests/test_tui_databinding.py`).

No schema change here — dispatch only. A net line reduction; makes a future stage 4 a
one-place edit. See [[review-stats-shared-renderer]].

## Follow-up: persist analyze-time PDQ thresholds on `review_runs`

**Problem (latent drift, altitude finding).** The histogram bin boundaries come from two
sources: the CLI passes live `ctx.config.match`, the TUI poll passes the `_T_*` defaults in
`review_stats.py`. A user who overrides thresholds in `config.toml` sees different bins in the
log vs the box for the same run. Worse, even the CLI reads config *now*, not the thresholds the
run was **analyzed** under — a run analyzed before a config edit re-reports with the wrong bands.
The comment calling the bins "cosmetic" is wrong: `t_photo_recompress` also bounds which photos
land in a stage at all (`dedup._plan_perceptual`), so counts and labels genuinely diverge.

**Fix.** Snapshot the run's analyze-time thresholds on `review_runs` and have **both** faces
read that snapshot. One authoritative source; config edits can't retroactively rewrite an old
run's histogram; the poll never needs to re-read config. Best done *after* the dispatch
centralization: `stats_for_stage`'s `thresholds` param becomes the single seam both faces feed
the snapshot through.

**Schema change (mirror the `prefer_internal` precedent).** Three columns follow the exact
pattern of `review_runs.prefer_internal` (a run-scoped policy locked at analyze, read back by
every confirm):

```sql
-- db/schema.py, review_runs table (after prefer_internal):
t_photo_recompress INTEGER,  -- PDQ thresholds snapshotted at analyze (§8 B): the bands the
t_photo_edit       INTEGER,  -- run's stages + histogram bins are derived from, so the log and
t_match_video      INTEGER,  -- box read ONE source and can't drift from a later config edit.
```

Nullable (no `NOT NULL DEFAULT`), so a row that predates the columns reads NULL → callers fall
back to `review_stats._T_*` defaults (the current behavior), no migration runner needed
(pre-release; dev DBs rebuild anyway).

**Persistence steps (paths verified against current code).**
1. `db/schema.py` — add the three columns to `review_runs` with the comment above.
2. `jobs/dedup.py:_analyze` — the `INSERT INTO review_runs(...)` (~L221-223) already stores
   `prefer_internal`; add the three `ctx.config.match.t_*` values to the same insert.
3. `jobs/dedup.py:_report_review_stats` — build `thresholds` from the **run row** (like
   `prefer_internal = bool(run["prefer_internal"])` at ~L251), not live `ctx.config.match`, so a
   confirm after a config edit reports the analyze-time bands.
4. `queries.py:_review_counts` — read the three columns from the joined `review_runs` row and
   pass them to `stats_for_stage` instead of relying on the `_T_*` defaults; drop the "bins are
   cosmetic" comment (they aren't — `t_photo_recompress` also bands which photos enter a stage,
   `_plan_perceptual`).
5. Tests: a run analyzed under non-default thresholds shows the SAME histogram bins in the CLI
   log and the TUI poll bundle (regression against the dual-source drift).

Needs the schema column but no migration runner — still more than a one-liner, which is why it
stays separate from the dispatch refactor above.
