# Fingerprints & how duplicates are decided

This section defines the fingerprints packrat stores per asset and the two separate notions of
"duplicate" built on them. It is reference material; the operations that *act* on it are
scan/dedup/merge (see [scan](workflow-scan.md), [dedup](workflow-dedup.md),
[merge](workflow-merge.md)) and cleanup (see [trash-model](trash-model.md)).

## The fingerprints

Three fingerprints, all produced by **`scan`** (see [scan](workflow-scan.md)) and stored in the DB. Computing them is
scan's job; every other operation reads them.

- **Content hash — BLAKE3 of the file bytes.** The identity key: same bytes ⇒ same asset. Cheap,
  exact, format-agnostic (works even on files that won't decode).
- **Perceptual signature — robust to recompression/resize/re-encode.**
  - Photo: **PDQ (256-bit)** + its quality score — the single photo signal. (pHash is deliberately
    *not* stored for photos; see the perceptual matching engine below for why one signal is both sufficient and higher-recall.)
    Photo quality is **stored and surfaced as a confidence hint, but never gates** a photo out of
    matching — see the perceptual matching engine below (asymmetry with video).
  - Video: **duration** + a sequence of **per-frame PDQ** hashes (+ quality) sampled at fixed
    fractions of the timeline (see the perceptual matching engine below). Frames use the *same* PDQ as photos — after the [embeddings](embeddings.md) gap review dropped photo
    pHash, nothing in packrat uses pHash anymore.
- **Semantic embedding — CLIP vector.** Computed **only** on an explicit `scan --embed`, stored
  for future semantic search / trash-tagging (see [embeddings](embeddings.md)). **It never participates in any duplicate
  decision** — semantic similarity is not duplicate-ness (two different receipts, or two beach
  photos, score high on CLIP yet are distinct assets you want to keep). A plain scan computes
  none, and its absence or failure changes no dedup/merge/cleanup result.

## Two kinds of duplicate

Everything downstream rests on this distinction:

- **Exact duplicate — identical bytes** (same content hash). This is *identity*, not a judgment
  call: two files with the same hash are simply two `file_instances` of one asset. Resolved
  automatically wherever files are seen — by `scan` (attach a new instance), `merge` (skip/collapse
  on ingest), and `cleanup` (delete library copies of trashed content). Zero false positives.
- **Perceptual near-duplicate — different bytes, visually the same** (recompressed, resized,
  re-encoded, cropped…). These are **distinct assets** joined by a recorded similarity edge, never
  silently collapsed — because both files genuinely exist. Deciding what to do about them needs
  human review, so this is **only** ever surfaced by `dedup` (see [dedup](workflow-dedup.md)) and `cleanup --trash-perceptual`
  (see [trash-model](trash-model.md)), which stage candidates in Explorer for the user.

Exact resolution is cheap and safe enough to run inline anywhere; perceptual matching is a
deliberate, reviewed, opt-in operation.

## The perceptual matching engine

A single scope-agnostic matcher, run only by `dedup` and `cleanup --trash-perceptual`. It uses the
perceptual signature alone (never CLIP), over fingerprints already in the DB — pure hash math, no
file I/O.

- **Photo:** the **only** signal is **PDQ Hamming distance**. PDQ at a sane threshold is precise
  on the recompress/resize/format-conversion case — essentially the entire iPhone-re-export
  reality — so one robust signal is both sufficient and higher-recall than gating two signals
  together (which is why pHash is not computed or stored for photos at all — decided in the embeddings gap
  review; a single signal, no dead data). The matcher itself reports the raw PDQ distance for a
  matched pair; **`dedup` bands that distance into two review stages** with two cutoffs
  `t_photo_recompress < t_photo_edit` (see [dedup](workflow-dedup.md) / [tech-stack](tech-stack.md)): `d ≤ t_photo_recompress` = a recompression
  (stage 2, near-certain), `t_photo_recompress < d ≤ t_photo_edit` = a minor edit/crop (stage 3,
  scrutinize). The engine's own match cutoff is the wider `t_photo_edit`; the tighter cutoff only
  splits already-matched pairs into stages, so it is a review-ergonomics band, not a second recall
  gate. (`cleanup --trash-perceptual` uses the single wider cutoff, no banding — see [trash-model](trash-model.md).)
  - **Photo quality — annotate, never gate (asymmetric with video).** PDQ's 0–100 quality is
    *stored* per photo but **does not exclude** a photo from matching. This is deliberate and
    differs from video (`video.min_frame_quality`): a video has ~12 frames, so dropping a bad one
    still leaves plenty to vote; a photo has **exactly one** PDQ, so gating it out would make the
    asset **silently invisible to dedup** — a recall loss the user can't see, against the plan's
    recall-first tenet. Instead quality is used two safe ways:
    1. **Confidence hint in review.** PDQ on flat/near-black/letterboxed/low-detail images yields
       hashes that spuriously collide, flooding review with junk pairs. So every staged photo
       near-dup carries its (and its partner's) quality in the `manifest.csv` / `proposed.json`
       (see [dedup](workflow-dedup.md)), and a pair where *either* photo is below `review.low_quality_hint` (default **50**,
       same scale as video) is **flagged low-confidence** — a visual cue to skip it fast, not a
       removal. Nothing is hidden; noisy matches are just easy to dismiss.
    2. **Future gate, no re-scan.** Because quality is already stored, a `min_photo_quality` *gate*
       (if calibration on real data shows the collision flood is worse than the recall cost) can be
       switched on later **without re-decoding the collection**. Off by default in v1.
- **Video:** durations within a tolerance **and** at least a configured fraction
  (`video.frame_match_fraction`, default 0.60 — see table) of sampled frame descriptors match
  within threshold. **Frame descriptor is PDQ** — the same 256-bit hash used for photos, run on
  each sampled RGB frame (a decoded frame is just an image; per-frame PDQ is exactly what Meta's
  TMK+PDQF does — see [roadmap](roadmap.md) #3). This **unifies photo and video on one algorithm and drops the
  `imagehash` dependency entirely** (after the [embeddings](embeddings.md) gap review removed photo pHash, video frames were its only
  remaining use). Matching **pre-filters by duration** (compare only clips within ±tolerance) to
  avoid the naïve all-pairs blowup, then compares the two clips **frame-index-aligned** (frame *k*
  of A vs frame *k* of B), which is valid because both are sampled at the *same relative timeline
  positions* and the duration pre-filter keeps their lengths close enough to stay aligned.

  **Video match parameters (concrete defaults; canonical values live in `config.toml` (see [tech-stack](tech-stack.md)), logged
  with each run).** These were previously unspecified — pinned here so [dedup](workflow-dedup.md) / [trash-model](trash-model.md) are
  implementable:

  | Param | Default | Meaning |
  |---|---|---|
  | `video.sample_frames` | **12** | Frames sampled per video, at the **midpoints of N equal segments**: `t_k = duration·(k+0.5)/N`, `k=0..N-1` (offset by the stream's `start_time`, which transport streams commonly make non-zero). Proportional positions ⇒ same-content clips align frame-to-frame. Short clips (e.g. a 3 s Live-Photo `.MOV`) still get all 12. **Transport streams (`.ts`/`.m2ts`/`.mts`)** need two robustness fallbacks the mp4/mov path doesn't: (a) they often report **no duration** → recovered by a demux-only last-packet-timestamp pass; (b) they often **break mid-file seeking** (a seek to a non-zero target silently returns nothing) → sampling falls back from per-target seeks to one sequential decode pass picking the nearest frame to each target. Both engage only when the seek path under-delivers, so well-behaved containers keep the cheap seek. |
  | `video.duration_tol_s` | **1.0 s** | Absolute floor of the duration pre-filter. |
  | `video.duration_tol_pct` | **5.0 %** | Relative part. Two videos pass the pre-filter iff `|d₁−d₂| ≤ max(duration_tol_s, duration_tol_pct%·min(d₁,d₂))` — so a 3 s clip tolerates ~1 s drift, a 2 h movie ~6 min. |
  | `T_match_video` (per-frame distance) | **e.g. 90** | A frame-pair *matches* iff its PDQ Hamming distance ≤ `T_match_video`. **Separate from the photo cutoffs `t_photo_recompress`/`t_photo_edit`** and typically **more permissive**: video frames carry inter-frame-compression / motion-blur / keyframe-drift noise a still doesn't, and the frame-fraction vote below reclaims the precision a looser per-frame cutoff spends. (Same 0–255 PDQ Hamming scale as photo, different tuned value.) A video near-dup is a single frame-vote match — it is **not** banded into recompress/edit stages; all video matches go to dedup stage 2 (see [dedup](workflow-dedup.md)). |
  | `video.frame_match_fraction` | **0.60** | The two videos are a near-dup iff **≥ 60 %** of *comparable* frame-pairs (see quality gate) match within `T_match_video`. This vote is video's *second* precision control — the one photos lack — which is exactly why the two cutoffs need not (and should not) be equal. |
  | `video.min_frame_quality` | **50** | PDQ emits a 0–100 quality per frame; dark/blurry/transition frames score low and hash unreliably. A frame below this is **excluded** from comparison (stored, but flagged). A frame-pair is *comparable* only if **both** frames clear the gate. |
  | `video.min_comparable_frames` | **5** | If fewer than this many comparable frame-pairs remain after the quality gate, the pair is **not** matched — insufficient evidence beats a coin-flip. |

**Match-distance thresholds — `t_photo_recompress` / `t_photo_edit` (photo) and `T_match_video`
(video)** (all configurable and logged). Same 0–255 PDQ Hamming scale, tuned independently. For a
**photo** the single comparison *is* the decision: `t_photo_edit` is the engine's match cutoff (a
pair with `d ≤ t_photo_edit` is a near-dup), and `t_photo_recompress` (the tighter value) *bands*
matched pairs into dedup's two review stages (see [dedup](workflow-dedup.md)) — it is not a separate recall gate. For a
**video** the per-frame cutoff only feeds a **majority vote** (`video.frame_match_fraction`), a
second precision control, and frames are noisier — so `T_match_video` is typically the most
permissive. A pair is a near-dup iff:
- **photo:** PDQ Hamming distance ≤ `t_photo_edit` (then banded: `≤ t_photo_recompress` → recompress,
  else → minor-edit);
- **video:** the two clips pass the duration pre-filter **and** ≥ `video.frame_match_fraction` of
  their *comparable* (quality-gated) frame-pairs are each within `T_match_video` **and** at least
  `video.min_comparable_frames` comparable pairs exist (table above).

No *second, per-medium* "auto vs. borderline" cutoff is needed on top of these, because **every**
perceptual match is surfaced for human review — nothing is auto-acted-on. (The other `video.*`
knobs are *structure* parameters — how many frames, how close in length, how many must agree — not
distance cutoffs.) Set each threshold high enough to catch what PDQ *structurally* can
(recompression, resize, format conversion) plus the harder cases you want a look at (crops,
rotations, borders/watermarks, heavy re-encodes); every hit lands in the review folder either way,
so a permissive threshold just means more candidates to eyeball, never a silent deletion. The
operation (see [dedup](workflow-dedup.md) / [trash-model](trash-model.md)) decides how matches are staged. **All three cutoffs
(`t_photo_recompress`, `t_photo_edit`, `T_match_video`) and every `video.*` knob need calibration
on real data — see [roadmap](roadmap.md) #1.**

**Comparison set depends on the caller:**
- **`dedup`** compares a folder's assets against **active assets only** — trashed assets are
  excluded (its model is "collapse redundant copies, keep one survivor," which a trashed asset —
  usually zero instances, nothing to keep, opposite intended action — cannot fit).
- **`cleanup --trash-perceptual`** compares a folder's active assets against the **trashed** set (find
  recompressed copies of things you trashed).
- **`merge`** does **not** use this engine at all — it decides purely by exact hash, including its
  trash check.

## Cost & caching

The first full `scan` of 100K assets is a one-time multi-hour cost (video decode dominates over
SMB); it is checkpointed per file and resumes after interruption. Later scans are cheap via the
fast-path (see [scan](workflow-scan.md)), which skips re-fingerprinting unchanged files. The perceptual matcher runs on
the stored signatures (a few MB in the DB) — seconds of CPU, no I/O (see [performance](performance.md)).
Embeddings, if ever wanted, are a separate opt-in `scan --embed` pass and not part of this
baseline cost.
