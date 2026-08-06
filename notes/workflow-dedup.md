# Dedup a single registered folder

`dedup` **targets one registered folder** (root) at a time and stages its removable duplicates
as **Windows shortcuts** inside that folder, for the user to review in Explorer and then confirm.
It works from the fingerprints scan already stored in the DB (hashes, `phash`/`vphash`). Comparison
spans all **active** assets across the whole collection: an asset in the target folder is judged
against active copies in *external* registered folders too. **Trashed assets are excluded** — dedup
only collapses copies of things you're keeping; trash exclusion is `merge`/`cleanup`'s job (see [trash-model](trash-model.md)).

**A dedup run is a fixed three-stage sequence, presented one stage at a time** so review stays
focused and each folder means one thing. Each stage stages its own kind of duplicate into its own
folder, you review it in Explorer, and `--confirm` applies *that stage* and then automatically
advances to the next non-empty stage. The stages, in order:

| # | Stage folder (`_packrat_review\…`) | What it stages | Default if you do nothing | To change a file's fate | Media |
|---|---|---|---|---|---|
| 1 | `_exact_dup_to_delete\` | byte-identical copies (an asset's redundant instances; a survivor is always kept) | **DELETE** | **remove** the shortcut to **spare** | photo + video |
| 2 | `_suspect_recompression\` | near-dups within the tight band — recompressed / resized / re-encoded copies (`d ≤ t_photo_recompress`; video: a matched frame-vote). packrat marks the least-compressed member `_suggested` as a keep-hint (photo *and* video, step 9) | **KEEP** | **remove** the shortcut to **delete** | photo + video |
| 3 | `_with_minor_edits\` | near-dups in the wider band — minor edits/crops/borders (`t_photo_recompress < d ≤ t_photo_edit`) | **KEEP** | **remove** the shortcut to **delete** | **photo only** |

The **naming carries the safety signal**: only the default-DELETE folder is named `…_to_delete`;
the two default-KEEP folders are content-named. Rationale: exact dups are objectively redundant
(default-delete, veto to keep); near-dups need human judgment (default-keep, remove to delete). Two
distance cutoffs (`t_photo_recompress < t_photo_edit`, see [fingerprints](fingerprints.md) / [config](tech-stack.md)) split photo near-dups into the
two review bands so you can blaze through the near-certain recompressions in stage 2 and scrutinize
the genuine edits in stage 3. **Video near-dups are a single frame-vote match** (a match score, not
a recompress-vs-edit split), so they all land in **stage 2** and there is no stage-3 video.

**Why sequential stages (not the old two-folder-at-once design).** Presenting exact + perceptual
folders simultaneously forced a hard rule — an asset with a planned exact deletion was **excluded
from perceptual grouping** and its near-dup **deferred to a later `dedup` run** (so no asset could
appear in two opposite-convention folders at once). Sequencing dissolves that: stage 1 resolves
exact dups first (deleting only *redundant instances*, never removing an *asset*), so by stages 2–3
every asset still exists and can be matched perceptually **in the same run** — the deferral and its
edge-case-6 exclusion are gone. It also means **survivors exist only in stage 1**: stages 2–3 stage
*distinct assets* with no survivor concept (deleting a near-dup member never threatens another
asset's last copy), which is what makes their apply path simple.

**Liveness is verified lazily, not eagerly.** Stale DB rows (a file moved/deleted in Explorer since
the last scan) are rare, and `stat()`-ing every candidate up front — especially external copies on a
cold/sleeping drive — is mostly wasted work. So there is **no eager stat**; liveness is checked only
where a stage acts, stat'ing only the files it is about to touch:
- **At shortcut creation (per stage):** stat each planned target right before writing its `.lnk`, so
  **no broken shortcut is ever created** — a vanished target is skipped and its DB row lazily cleaned.
- **At delete (per stage `--confirm`):** re-stat immediately before the irreversible move — the
  authoritative gate (a file may have changed again since staging).

Any divergence resolves toward **sparing**: a gone file is not staged / not deleted; if an exact
survivor turns out gone, its redundant copies are spared (and one is promoted). The pipeline only
ever acts on *fewer* files than the DB preview implied, never more. *(edge case 5)*

```
packrat dedup "D:\Backup\iPhone"            # analyze → stage 1 → pause (pending, stage 1)
packrat dedup "D:\Backup\iPhone" --confirm  # apply the current stage, auto-advance to the next
                                            #   non-empty stage (pending); after stage 3 → completed
packrat dedup "D:\Backup\iPhone" --cancel   # discard the whole run's staging, delete nothing
packrat dedup "D:\Backup\iPhone" --dry-run  # compute all 3 stages read-only; stage/write nothing
# (per-root dedup/review state — including the current stage — is shown by `packrat status`, see [cli](cli.md))
```

Terminology: **target folder** = the root passed to `dedup`. **External folder** = any *other*
registered root. **Survivor** = the one file instance of an asset that stage 1 keeps.

## Dedup state machine (one run per folder, a stage cursor within it)
- **A single `review_runs` row spans the whole 3-stage sequence.** It carries `status`
  (`pending` → `completed`/`cancelled`), a **`stage`** cursor (1→3) and a **`stage_phase`**
  (`staged` = shortcuts written, awaiting the user; `applied` = this stage's deletions done, next
  stage not yet staged) — the two new columns (see [data-model](data-model.md)). It stays `pending` across all three stages; only
  applying the **last non-empty** stage flips it to `completed`. The **partial unique index still
  enforces at most one `pending` run per `root_id`**, so a second `dedup <folder>` while a run is
  open errors and tells you to `--confirm` through it or `--cancel` it.
- Each stage's plan is persisted to `review_actions` (tagged with its `stage`) **when that stage is
  staged**, so every `--confirm` is deterministic and crash-safe: it re-reads which shortcuts you
  kept/removed and never re-decides. Because the asset set is stable after stage 1, later stages are
  computed **lazily at the moment they're staged** (right after the prior stage applies), not all up
  front — except `--dry-run`, which computes all three read-only for the preview.

---

## dedup <folder> — analyze & stage the first stage (produces `pending`)

**Phase 0 — Validate & lock**
1. Resolve `<folder>` to a registered root; it must be a **library** root (error otherwise). → **read** `roots`.
2. **Per-root exclusivity (see [architecture](architecture.md), guarantee 2).** If this root already has an active operation — a
   `pending` `review_runs` row (another dedup or a cleanup), or an in-flight merge (an open
   `merge_runs` row (`status IN ('planning','copying')`) with `dest_root_id` = this root, see [data-model](data-model.md)) — this
   analyze does not run against it: it is **held in the queue (dequeue gate, see [architecture](architecture.md)) until the holder
   clears**, acquiring ownership only when it actually runs (opening its `review_runs` row below).
   (A concurrent *scan* can't coexist either: scan's step 1a holds it behind this run's `review_runs`
   row, and the global single-worker slot never runs a scan and this analyze at the same instant.)
   Then compute stage 1 (Phase 2). If the whole run would be empty (no stage has
   any candidate) it **auto-completes "already clean"** without leaving a `pending` row dangling.
   Otherwise **write** a `review_runs` row (`root_id`, `status='pending'`, `stage=1`,
   `stage_phase='staged'`, `created_at`, plus the analyze-time policy snapshot — `prefer_internal`
   and the PDQ thresholds `t_photo_recompress`/`t_photo_edit`/`t_match_video`, see [data-model](data-model.md) — locked here and
   read back by every `--confirm` and both review-stats faces) — which now *owns* the root until
   confirmed/cancelled — and open a `jobs` row (`type='dedup'`).

**Phase 1 — Build from the DB (no eager stat)** *(edge case 5)*
Analyze builds the plan directly from existing `file_instances`/`phash`/`vphash` rows; it does
**not** stat files. It recommends a fresh `scan <folder>` first if `last_full_scan_at` is old (scan
already stats the folder, making internal liveness current for free) but does not force it. External
copies are trusted as live; if one turns out gone, the per-stage shortcut-creation and confirm
checks catch it and spare the internal copies (worst case: the preview offers slightly more than
confirm deletes). → No writes/stats here; lazy DB cleanup happens as broken targets are encountered.

**Phase 2 — Stage 1: exact-duplicate resolution** *(byte-identical = same asset)*
For each **active** asset with ≥1 live instance **in the target folder**:
3. **Exact dup with an external folder** → the external copy is byte-identical, so **all** of the
   target folder's instances are redundant. Plan every target-folder instance for deletion
   (`kind='exact'`, `reason='exact-external'`, survivor = the external instance). Keep nothing locally.
   **With `--prefer-internal` (keep-preference, this section)** the roles flip: keep an **internal** copy
   (oldest `mtime`, tiebreak path) and plan *every other* copy for deletion — including the external
   ones (`reason='exact-internal-preferred'`; the external deletes carry `is_external=1` so the
   network-permanent-delete warning still fires, see [performance & safety](performance.md) / [[review-network-count]]).
4. **Else, exact dups within the target folder** (≥2 live instances, all in this root) → keep the
   **oldest `mtime`** (tiebreak: stable by path), plan the rest for deletion (`kind='exact'`,
   `reason='exact-internal'`, survivor = the kept instance).
5. **Else** (single live instance, no external copy) → a survivor; nothing to delete.
   → Stage-1 deletions are **written** to `review_actions` (`stage=1`, `folder='exact_dup_to_delete'`,
   `default_action='delete'`, target instance/asset/path, survivor reference).

**Phase 3 — The perceptual stages (2 and 3) — computed when each is staged**
Perceptual stages are the **matching engine** (see [fingerprints](fingerprints.md)): run the PDQ / video-frame matcher for the target
folder's assets against all **active** assets collection-wide (**trashed excluded** — see [fingerprints](fingerprints.md)), **upsert**
the results into `similarity_edges` (dedup is that table's writer — see [data-model](data-model.md) / workflow division of labor), and
build clusters from the edges. Pure DB + fingerprint math, no file I/O. Video matching **pre-filters
by duration** (`|d₁−d₂| ≤ max(duration_tol_s, duration_tol_pct%·min)`, see [fingerprints](fingerprints.md)) to avoid the all-pairs
blowup.
6. **Which edges belong to which stage** (photo, by PDQ distance `d`):
   - **Stage 2 (`_suspect_recompression`)** — `d ≤ t_photo_recompress` (the tight band), **plus every
     video near-dup match** (video is a single frame-vote match, so all video pairs go here).
   - **Stage 3 (`_with_minor_edits`)** — `t_photo_recompress < d ≤ t_photo_edit` (photo only). A pair
     already in stage 2's band is **not** re-shown in stage 3.
7. **No cross-stage exclusion, no deferral.** Because stage 1 deleted only *redundant instances* and
   never removed an *asset*, an asset can legitimately appear in stage 1 (a copy deleted) **and** a
   later stage (it's a near-dup of something else) — both in the same run. There is **no**
   edge-case-6 asset-level exclusion and **no** "run it again to see the group" deferral anymore.
   (Between staging stage 2 and stage 3, exclude any pair already offered in stage 2 — see step 6 —
   so a spared recompression isn't nagged again as an "edit".)
8. **Edges are always (re)computed for this run, never reused as complete input.** `similarity_edges`
   stores only *matches*, not "compared, no match," so it cannot distinguish "no near-dups" from
   "never compared" (e.g. an asset scanned/backfilled after the cache was built) — trusting it would
   **silently miss** those, a recall loss the user can't see (against the recall-first tenet, cf.
   [fingerprints](fingerprints.md)). The matcher is pure DB/CPU and runs in seconds–low-minutes (see [fingerprints](fingerprints.md)), so recomputing is cheap
   and always correct. The upsert persists the run's edges as a queryable record (forensics / the
   audit trail below); it is deliberately **not** surfaced as a headline "duplicates (est)" TUI stat, since
   as a per-run cache it is 0 before any dedup and stale after later scans (see [tui](tui.md), Collection stats).
9. For each cluster of size ≥2 in a stage, assign a 4-digit `group_no` and each member a 4-digit
   `member_no`; plan a shortcut `group{NNNN}_{MMMM}.lnk`, with an `_external` suffix when the
   member's live file is in an external folder. Each member is represented by its single surviving
   instance (target-folder if present, else external). → **write** `review_actions`
   (`stage=2|3`, `folder='suspect_recompression'|'with_minor_edits'`, `kind='perceptual'`,
   `default_action='keep'`, `group_no`, `member_no`, target instance/asset/path, `is_external`,
   `distance`). **Perceptual actions carry no survivor** (`survivor_instance_id` NULL) — near-dup
   members are distinct assets.
   - **Stage-2 keep-lead (annotate-only).** In **stage 2** the members of a group are essentially the
     same content at differing compression (the tight `t_photo_recompress` band / the video frame-vote
     → almost no visible difference), so packrat **suggests which copy to keep** — the least-compressed
     one — by marking the winner's shortcut **`_suggested`** (`group{NNNN}_{MMMM}_suggested.lnk`,
     combined with `_external` if applicable). A group is homogeneous (a photo never matches a video),
     so the group's medium picks the ranking key; both lead with **resolution** (`width·height`) — a
     downscaled re-export loses outright:
     - **Photo:** resolution → **format rank** → file `size` → stable path. **Format rank** is a
       3-level ordinal (best first): lossless/original (`png`/`tif`/`tiff`/`bmp`/RAW) >
       **efficient-lossy** (`heic`/`heif`/`avif`) > other-lossy (`jpg`/`webp`/`gif`/…). It is the
       primary quality signal after resolution: at equal resolution a lossless copy is the master,
       and among lossy copies a modern codec packs more real detail per byte than JPEG, so an iPhone
       HEIC original outranks its JPEG export. Then, **within one format**, the larger file `size`
       wins — at fixed resolution+format the encoder's output size *is* the quality dial, so size is a
       clean monotonic quality proxy there. `size` is used only *within* a format because it **lies
       across** formats (an efficient HEIC master is smaller than a bloated JPEG export) — which is
       exactly what the format rank above it handles. *Accepted cost:* a genuinely low-quality HEIC
       outranks a high-quality JPEG of the same scene, and a JPEG re-wrapped as HEIC beats its source
       — rare (HEIC is the original on iPhone), advisory-only (never deletes; overridable in review).
       *(An earlier residual-entropy `detail_score` signal was tried and dropped: it cost ~40% of
       scan CPU, and once banded to tame its high-quality-JPEG noise it only ever agreed with `size`
       within a format — so `size` alone is simpler and equivalent. See [roadmap](roadmap.md).)*
     - **Video:** resolution → **effective-bitrate band** → **codec-efficiency weight** → **raw
       effective bitrate** → stable path. Effective bitrate = `size / duration_s × codec_weight`
       (`match.codec_weights`, see [config](tech-stack.md)): a more-efficient codec's bits are worth more, so an HEVC master
       beats an H.264 re-export at equal resolution+quality. Dividing by `duration_s` removes the
       length bias within the duration tolerance (a slightly-longer clip at equal quality has a bigger
       file, not more detail). Two effective bitrates within `match.video_bitrate_tie_pct` (default
       10%) share a **log-scale band** (a "tie"): the codec weight then decides a **cross-codec**
       near-tie (prefer the more-efficient/modern encoding at indistinguishable effective quality).
       The band + weight both tie only when the codecs are equally efficient (typically the *same*
       codec) — there the trailing **raw effective bitrate** breaks the tie, since higher bitrate at
       equal resolution+codec is a clean quality dial (the video analogue of file `size` within one
       photo format). Because it sits *after* the weight it can never reverse the cross-codec pick; it
       only replaces a path coin-flip on a same-codec near-tie. *Accepted caveat:* bitrate lies
       **across codecs** (HEVC is ~2× H.264-efficient), which the weight *reduces* but doesn't cure —
       surfaced in the manifest (codec + bitrate shown) for hand-override, not solved. No
       `duration_s`/`codec` → falls back to raw size / weight 1.0.
     - **Full-key tie → internal/external keep-preference (both media).** When the whole ranking
       key ties and the group is **mixed** (an internal copy and an external copy tied on
       everything), the keep-lead goes to the **external** copy by default, or the **internal** copy
       under **`--prefer-internal`** (keep-preference, this section; the run's stored `prefer_internal`). A
       coin-flip on the smallest normcase path decides only among copies on the *same* side. This
       tiebreak sits **below** the whole ranking key, so it never overrides a real quality signal —
       it only replaces the arbitrary path pick when quality is genuinely equal. (`--prefer-internal`
       is a run-wide policy fixed at analyze; it also flips the **stage-1** exact survivor, Phase 2.)
     **This is a hint by default:** the stage stays default-**KEEP** (you still delete a member by
     removing its shortcut); the marker itself never deletes anything and never changes a default.
     **Stage 3 (minor edits) is deliberately NOT ranked** — the *edited* copy may be the one you want
     to keep. → `is_lead` + `lead_reason` (the decision level, below) recorded in the plan; surfaced in
     `manifest.csv` (`suggested_lead`, `suggested_reason`, `media_type`, `width`, `height`, `size`,
     `duration_s`, `codec`, `bitrate` columns) + `proposed.json`.
     - **Keep-lead pick stats (reported at staging AND in the TUI Review box).** When stage 2 is
       staged, the report logs *how* each group's lead was decided — a tally over the ranking key's
       decision levels, **split into side-by-side photo and video columns** (photo: `resolution` /
       `+ format` / `+ format + size`; video: `+ bitrate` / `+ bitrate + codec` / `+ fine bitrate`;
       plus `internal/external preference` and `path tiebreak` when the whole key tied). This exposes
       how much of the collection the lead rests on resolution alone vs. the finer calls, so the
       suggestion's confidence is visible before you act on it. The CLI staging log and the TUI
       Review box render this from **one shared builder** (`packrat/review_stats.py`) over the same
       persisted `review_actions` rows, so they can't drift — the box also adds **separate photo and
       video PDQ-distance histograms** (bins derived from the stage's thresholds; video is a mean-
       Hamming on its own scale, see [fingerprints](fingerprints.md)), the internal/external group make-up, and the mixed-group
       suggestion split (all-internal vs. mixed → suggest-external vs. suggest-internal). **Stage 3
       (minor edits)** renders the same near-dup shape (groups/members + photo histogram + make-up)
       but no keep-lead columns, since it is unranked.
     - **`--confirm --keep-suggested` (stage 2 only): act on the suggestion in bulk.** Instead of
       reviewing shortcut-by-shortcut, this **keeps only each group's `_suggested` lead and deletes
       every other member, ignoring your shortcut edits for the stage**. It is the "I trust packrat's
       pick" shortcut. **Safety:** a group with **no** suggested lead (an all-external group, or a lead
       whose `.lnk` failed to stage) is **fully spared** — it never deletes every copy of an asset
       because packrat couldn't name a keeper. Rejected on stage 1 / stage 3 (no leads there). Deleted
       non-leads follow the normal perceptual-deletion path (asset → `trashed`/`dedup-perceptual` at
       zero instances, Phase 6 below). Only stage 2 is affected; the run then advances normally.

**Phase 4 — Materialize the current stage's staging folder** *(edge case 5)*
Create the current stage's folder under `<root>\_packrat_review\` (already in the ignore set, so
scan never indexes it or the `.lnk`s). Analyze materializes **stage 1**; `--confirm` materializes the
next stage after applying the current one (Phase 6). Per staged action:
10. **Stat-before-create — never emit a broken `.lnk`.** `stat()` the target at the instant of
    creating its shortcut:
    - **Target present** → create the `.lnk` (this also finalizes `is_external` / the `_external`
      suffix from the live path).
    - **Target gone** → **skip** the shortcut, lazily clean the DB (delete the gone `file_instances`
      row; if an `active` asset hits zero instances → delete the asset, cascading fingerprints), and
      **do not persist** a `review_actions` row for it — count "skipped-at-staging". *(Never persist a
      row whose shortcut isn't on disk: in a default-KEEP stage `--confirm` reads an absent shortcut as
      "delete", so a phantom row would silently delete an unreviewed file.)*
    - **Survivor-gone special case (stage 1 only):** if an exact target is present but its planned
      **survivor** has vanished, do **not** stage the target — **promote it to survivor** (redirect the
      asset's other exact deletions at it) and skip its shortcut. Same promotion as the Phase-6 gate
      (step 17b), applied early. *(Stages 2–3 have no survivors, so this case can't arise there.)*
    Net: **every `.lnk` that lands resolves to a real file** and previews correctly.
11. Write a **`manifest.csv`** in the stage folder — a flat export of that stage's `review_actions`
    so the opaque `.lnk`s are legible (a documentation sidecar; `--confirm` reads shortcut presence,
    **not** the manifest). Columns:
    - `_exact_dup_to_delete\manifest.csv`: `shortcut, target_path, asset_id, reason, survivor_path`
    - `_suspect_recompression\` / `_with_minor_edits\manifest.csv`:
      `shortcut, target_path, asset_id, group_no, member_no, suggested_lead, suggested_reason,
      media_type, width, height, size, duration_s, codec, bitrate, is_external, distance, quality,
      low_confidence` — `suggested_lead`=`1` on the keep-hint member (stage 2 only, step 9), and
      `suggested_reason` names *why that member won* (the ranking-key decision level — e.g.
      `resolution + format` — filled only on the lead row, blank otherwise); the
      `media_type`/`width`/`height`/`size`/`duration_s`/`codec`/`bitrate` columns are
      the ranking inputs (so a surprising lead is explainable at a glance — e.g. a HEIC-vs-JPEG or
      HEVC-vs-H.264 call); `quality` is the member's PDQ quality (0–100; video: min across comparable frames);
      `low_confidence`=`1` when this member or its partner is below `review.low_quality_hint` (a
      flat/near-black spurious-collision hint to skip fast, see [fingerprints](fingerprints.md)).
12. **Audit trail (capture point 1 — the proposed plan).** Write/append an immutable `proposed.json`
    in this run's audit dir (audit trail, below): the full plan **for every stage as calculated**, each action with
    its stage, target path, reason, survivor, group/member, distance, per-member `quality` and
    `low_confidence`, plus skipped/spared counts and the thresholds in effect (`t_photo_recompress`,
    `t_photo_edit`, `t_match_video`, the `video.*` knobs, `review.low_quality_hint`). During
    `--dry-run` this is the whole preview; during a live run it records the plan as each stage is
    computed. Immutable, outside the folder, unlike the in-folder `manifest.csv` (deleted at finalize).
13. Open the stage folder in Explorer (or its `_packrat_review\` parent), print the `--confirm` /
    `--cancel` commands **naming the current stage**, and **pause** (`review_runs.status='pending'`,
    `stage_phase='staged'`). If the current stage staged nothing (all targets gone), auto-advance to
    the next non-empty stage instead of pausing; if none remain, auto-complete "already clean".

**The conventions differ by stage — read carefully:**
| Stage folder | Default if you do nothing | To change a file's fate |
|---|---|---|
| `_exact_dup_to_delete\` | the real file **is deleted** | **remove** its shortcut to **spare** the file |
| `_suspect_recompression\` | the real file **is kept** | **remove** its shortcut to **delete** the file |
| `_with_minor_edits\` | the real file **is kept** | **remove** its shortcut to **delete** the file |

**Reviewing = deleting shortcuts, not renaming them.** Matching is strict on the planned filename, so
a *renamed* shortcut counts as removed (Phase 5). In the default-KEEP stages that means an accidental
rename would delete the target — the typed `--confirm` summary lists every such file (per root) so it
can't happen silently.

---

## dedup <folder> --confirm — apply the current stage, advance (→ `completed` after stage 3)

**Phase 5 — Read the user's edits**
14. Load the `pending` run and its current stage's `review_actions`. **No pending run** → error
    ("nothing to confirm; run `dedup <folder>` first"); same for `--cancel`. A `completed`/`cancelled`
    run is terminal — re-`--confirm` is a no-op error.
15. **Safety guard:** if the current stage's staging folder is *missing* (user deleted the whole
    folder), **abort** — never read "folder gone" as "delete all" (mass data loss in a default-KEEP
    stage). Require the folder to exist to be read.
16. For each of the stage's planned actions, check whether a file with **its exact planned shortcut
    name** still exists in the folder (strict, filename-only — the manifest is not consulted):
    - `_exact_dup_to_delete`: shortcut **present** → intend delete; **absent/renamed** → spare (veto).
    - `_suspect_recompression` / `_with_minor_edits`: shortcut **absent/renamed** → intend delete the
      target; **present** → keep.
    A renamed shortcut counts as **removed**; extra files dropped in are ignored (only planned names
    consulted). This yields the *intended* delete set; liveness is applied per-file in Phase 6.
    - **`--keep-suggested` override (stage 2 only):** skip the shortcut-presence read entirely and
      derive the intended set from the plan — delete every member **except** each group's
      `_suggested` lead, regardless of what shortcuts the user added/removed. A group with no
      `_suggested` lead is spared whole (never delete every copy because no keeper was named).
      Rejected outside stage 2 (stages 1/3 have no leads). Phase 6 liveness still applies.

**Phase 6 — Authoritative liveness + apply this stage's deletions** (backup DB first) *(edge case 5)*
The authoritative gate — done lazily, one target at a time, right before the irreversible move.
17. Print a summary for **this stage** grouped by target root — **including any external-folder files**
    a default-KEEP-stage shortcut removal would delete — and require typed confirmation. **Flag
    non-recyclable paths:** count and call out files on network/SMB roots, deleted **permanently** (no
    Recycle Bin — see [performance & safety](performance.md)), e.g. "K of N are on network shares → permanent."
18. For each file in the intended delete set, at the moment of deletion:
    a. **`stat()` the target.** Gone already → nothing to delete; lazily clean the DB (delete the gone
       `file_instances` row; an `active` asset at zero instances → delete the asset, cascading
       fingerprints) — count "already-gone". Present → proceed.
    b. **Stage 1 only — verify the survivor is still live** before deleting (guarantees an asset never
       loses its last copy): `stat()` the `survivor_instance_id` path. **Live** → delete the target.
       **Gone** → the target is no longer redundant: **spare it** and **promote it to survivor**
       (redirect the asset's remaining exact deletions at it), lazily delete the vanished survivor's
       row, log "spared: survivor vanished (promoted)". *(Stages 2–3 have no survivor step.)*
    c. Move the (still-present, still-redundant) file to the **Recycle Bin** (recoverable locally;
       **permanent on NAS/SMB** — see [performance & safety](performance.md)), then update the DB:
       - **Stage 1 (exact)** → delete that redundant `file_instances` row. The asset keeps its survivor,
         so it **stays `active`** — never trashed. No re-appearance concern.
       - **Stages 2–3 (perceptual)** → the user deliberately discarded a near-dup. Delete its
         `file_instances` row; if the asset now has zero instances → **write** `assets.status='trashed'`,
         `trashed_at`, `trash_reason='dedup-perceptual'`, **retain its fingerprints** (the one path where
         an asset survives at zero instances) so a future merge/dedup excludes this near-dup (trash memory, see [trash-model](trash-model.md)).

**Phase 7 — Apply-then-advance / finalize** *(the one new crash window — resumable via `stage_phase`)*
`--confirm` applies the current stage and then stages the next, as **two committed steps** (like
merge's copied→registered gap):
19. **Commit "applied".** After Phase 6's deletions commit, set `review_runs.stage_phase='applied'`
    (still `pending`, same `stage`). *(A crash here leaves `applied` with nothing staged — reconcile
    must **not** roll this back, the deletions were correct; re-running `--confirm` sees `applied` and
    jumps straight to step 20.)*
20. **Stage the next non-empty stage** (Phases 3–4 above): compute stage `stage+1` (then `+2` if it's
    empty), materialize its folder + `review_actions` + append to `proposed.json`, and commit
    `stage=<next>`, `stage_phase='staged'`. Then **pause** with the next stage's `--confirm`/`--cancel`
    prompt. If **no non-empty stage remains**, instead finalize:
21. **Audit + finalize.** Write the immutable `applied.json` (audit trail, below): the final disposition of every
    action across all applied stages — `deleted` / `spared` / `kept` / `already-gone` /
    `survivor-vanished`, with path, root, asset_id, Recycle-Bin destination, stage — plus totals and
    `confirmed_at`. Delete all of this run's stage folders (shortcuts + manifests), leaving the shared
    `_packrat_review\` parent. → **write** `review_runs.status='completed'`, `confirmed_at`; close the
    `jobs` row. Report per-stage: exact deleted, perceptual deleted (stage 2/3), spared/kept, external
    deleted, plus lazily-cleaned stale rows. **`--cancel`** (any stage) deletes **all** the run's stage
    folders, marks the run `cancelled`, deletes nothing, and still writes `applied.json` (every action
    `cancelled`). *(A run cancelled mid-sequence keeps whatever earlier stages already deleted — those
    were confirmed — and its `similarity_edges` rows, which are a cache never trusted as complete input.)*

**Cross-folder note:** a perceptual member can live in an external folder (`_external` shortcut) — a
near-dup of a target-folder asset that physically resides only in another root. Removing that shortcut
deletes a file in *another* root — the Phase 6 typed-confirm summary (step 17) calls this out per-root
so it is never accidental.

**Why dedup is DB-first with lazy liveness:** the *decision* work is pure DB comparison — no eager
whole-pool stat. It stats a file only when a stage acts on it: once creating that file's shortcut
(no broken `.lnk`) and once immediately before deleting it (the authoritative gate). **Merge** (see [merge](workflow-merge.md)) is
unrelated to this machinery: it hashes transient source files and classifies them by exact hash — no
perceptual signatures, no `similarity_edges`, no shortcuts.

## Review-run audit trail (dedup & perceptual-cleanup)

Every stateful review run — `dedup` **and** `cleanup --trash-perceptual` — leaves a permanent,
append-only record outside the collection, so you can always answer "what did it propose, and
what did it actually delete" long after the staging folders (and their `manifest.csv`s) are gone.
Deleting a whole registered folder never erases this history.

**Location:** one directory per run under
`%APPDATA%\packrat\audit\{run_type}\{root_name}\{run_id}\` (`run_type` ∈ `dedup`,
`cleanup-perceptual`), containing:
- **`proposed.json`** — written at Phase 4 (capture point 1): the complete calculated plan
  before any user review — every action (target path, root, asset_id, kind/reason, survivor,
  group/member, distance, `is_external`), the counts of skipped-at-staging/spared items, and the
  active threshold/config. Immutable once written.
- **`applied.json`** — written at Phase 7 (capture point 2): the final disposition of each
  action (`deleted` / `spared` / `kept` / `already-gone` / `survivor-vanished` / `cancelled`),
  with Recycle-Bin destinations for deleted files, totals, and `confirmed_at`. Written even on
  `--cancel`.

**Properties:**
- **Immutable & additive:** files are written once, never edited; a re-run of dedup on the same
  root gets a *new* `run_id` directory. This mirrors `review_runs`/`review_actions` in the DB, but
  survives DB loss/rebuild and is trivially greppable.
- **JSON (not CSV):** richer/nested and stable for tooling; the in-folder `manifest.csv` stays
  CSV for Explorer/Excel legibility. Different audiences, different formats.
- **Retention:** governed by `audit.retention_days` (see [config](tech-stack.md)); default **0 = keep forever** (small
  text files). Setting it >0 prunes audits older than N days — the pruning *pass* itself is a
  deferred nicety ([roadmap](roadmap.md) #5), but the knob and its default live in `config.toml` now.
- These files are **records, not inputs** — `--confirm` never reads them to make decisions (it
  reads shortcut presence + the DB plan); they exist purely for audit/forensics.
