# Tech stack

| Concern            | Choice |
|--------------------|--------|
| Language           | Python 3.11+ |
| Packaging / deps   | **uv** (project + venv + lockfile; `uv run` / `uv sync`) |
| Daemon API         | FastAPI + uvicorn (127.0.0.1 + token); single-worker job queue |
| CLI                | Typer (thin client: submit job, stream progress, Ctrl-C detaches) |
| TUI                | Textual (`packrat` no-args: logo + stats + live jobs + menu; later milestone) |
| DB                 | SQLite (WAL); SQLAlchemy Core or light SQL layer |
| Vector search      | numpy brute-force → hnswlib / sqlite-vec if needed |
| Content hash       | blake3 |
| Perceptual hash    | **pdqhash** only — 256-bit PDQ for both photos and video frames (see [fingerprints](fingerprints.md)). No `imagehash`/pHash anywhere. |
| Image decode       | Pillow + **pillow-heif** (HEIC/AVIF), OpenCV where handy; **rawpy** for the opt-in RAW group |
| Video              | ffmpeg / **PyAV** (frame sampling), ffprobe (metadata) |
| Metadata           | exiftool via pyexiftool |
| Embeddings (opt-in) | torch (CUDA) + open_clip — only on `scan --embed` (see [embeddings](embeddings.md)); OCR (PaddleOCR/Tesseract) is speculative/TBD |
| Scheduling         | **APScheduler** (`BackgroundScheduler`, in daemon) — a **core dep** (pure-Python, no wheel risk); realized by `jobs/scheduler.py`'s `PeriodicScheduler` + `PeriodicTask` registry (see [architecture](architecture.md)), first client `probe` (see [scan](workflow-scan.md)) |
| Job cancellation   | cooperative — jobs poll a cancel flag at their existing checkpoints |
| Locking            | in-daemon single-worker queue (mutating ops); `review_runs` row (per-root review) |
| Optional watch     | watchdog (real-time; not required for v1) |

**iPhone specifics called out**: photos are often **HEIC** and videos **HEVC/H.265** — HEIC
decode via `pillow-heif`, HEVC via ffmpeg. Handle Live Photos (paired .HEIC + .MOV) as two
assets. Handle long paths, Unicode, and Explorer "skip duplicates" semantics ourselves.

## Format coverage — "decode is the gate"

**Principle that makes this tractable:** only *decode* is format-sensitive. Everything else in
the pipeline operates on the decode output, not the file format:
- **Content hash (BLAKE3)** hashes raw bytes → format-agnostic; works on every format above,
  including files we can't decode or don't recognize.
- **Perceptual hash** — `pdqhash`/PDQ on an RGB numpy array, for **both** photos (the still) and
  video (each sampled frame). Format-agnostic *given a decoded image*; one algorithm for both.
- **CLIP embedding** takes a decoded RGB frame; it never sees the container/codec.
- **Metadata** (`exiftool`) is an independent reader with the widest format support in the stack.

So the only thing to verify is: **every photo format decodes to one RGB still, and every video
format decodes to sampled RGB frames.** Everything downstream then follows automatically.

| Format group | Decode path | Bytes hash | Perceptual | Embedding | Metadata |
|---|---|---|---|---|---|
| jpg jpeg jfif png gif bmp tif tiff webp | Pillow (native; libwebp bundled) | ✅ | ✅ | ✅ | ✅ |
| heic heif | `pillow-heif` (libheif) | ✅ | ✅ | ✅ | ✅ |
| avif | Pillow ≥11.3 native, else `pillow-heif` | ✅ | ✅ | ✅ | ✅ | ⚠ POC |
| RAW: dng cr2 cr3 nef arw raf orf rw2 pef srw | `rawpy` (LibRaw ≥0.20 for cr3) → embedded preview or postprocess | ✅ | ✅ | ✅ | ✅ | ⚠ POC |
| mp4 m4v mov avi mkv webm wmv flv mpg mpeg m2ts mts ts 3gp | PyAV/ffmpeg (H.264/HEVC/VP9/AV1/MPEG-2/VC-1…) → sampled frames | ✅ | ✅ | ✅ | ✅ (ffprobe) |

**Decode-stage notes:**
- **Perceptual + embedding both gate on decode.** There is no separate per-format work for
  hashing or CLIP — if a frame decodes, PDQ (photo still / video frame alike) and CLIP just run on
  the pixel array. This is why the matrix's last three columns mirror the decode column.
- **AVIF (⚠):** covered either by recent Pillow (native `AvifImagePlugin`, ~11.3+) or by
  `pillow-heif`'s AVIF opener. Both rely on the AV1 decoder being present in the bundled
  libheif/Pillow wheel — confirm on the Windows wheel with a real `.avif` in the smoke test.
- **RAW (⚠, opt-in):** LibRaw covers all listed extensions (cr3 since 0.20). **Decision:** for
  dedup we hash the RAW's **embedded JPEG preview** (fast, consistent, matches what a viewer
  shows) rather than a full demosaic (slow, and render params drift). Full postprocess is a
  fallback when no preview is embedded. Same preview feeds CLIP.
- **Animated GIF / multi-page TIFF:** decode the **first frame** for the perceptual hash and
  embedding (still treated as one asset).
- **Video codecs:** ffmpeg (via PyAV) decodes every codec these containers realistically carry
  (H.264, HEVC, VP8/9, AV1, MPEG-2/4, VC-1/WMV3). The only real risk is an exotic/ancient codec,
  which is negligible for a personal collection.
- **Transport streams (`ts`/`m2ts`/`mts`, MPEG-TS/AVCHD):** the *container*, not the codec, is the
  hazard — they routinely report **no duration** and **break mid-file seeking** (a seek to a
  non-zero target silently yields nothing; they also carry a non-zero `start_time`). The [fingerprints](fingerprints.md) sampler
  handles both: a demux-only last-packet pass recovers the timeline, and per-target seeking falls
  back to a single sequential decode pass; a genuinely undecodable `.ts` still flags `undecodable`.
  (`ts` collides with TypeScript, so a *code* directory registered as a library root would treat
  `.ts` sources as media candidates — a non-issue for a media collection.)
- **Graceful failure is mandatory:** a file whose bytes hash fine but *won't decode* is still
  recorded as an asset (identity is the hash) but flagged `undecodable` — no perceptual sig, no
  embedding, no near-dup matching for it. Scan never crashes on a bad file; it logs and moves on.
- **Windows install:** `Pillow`, `pillow-heif`, `PyAV` (bundles ffmpeg), `rawpy`, `blake3`, and
  `pyexiftool` all ship prebuilt Windows wheels — no compiler needed. `pdqhash` is a C++ binding
  and may need a wheel-availability check (⚠ POC); fall back to a pure-Python PDQ or the
  reference build if no wheel exists for the target Python version.

**Smoke test (do this before M1 in earnest):** assemble one real sample of *every* extension in
the allowlist (plus the RAW group) and run the decode→hash→perceptual→embed path over all of
them. This is the only check that truly "makes sure" — a doc/version claim can't guarantee a
given Windows wheel decodes *your* camera's CR3 or *that* AVIF encoder's output. The ⚠ cells
above are exactly what this test resolves.

## Configuration (config.toml)

All tunable knobs referenced throughout this plan live in **one** file:
**`%APPDATA%\packrat\config.toml`** — beside the daemon's existing `token` file (see [architecture](architecture.md)). TOML because
Python 3.11 parses it natively (`tomllib`, no dependency) and it matches the uv/`pyproject.toml`
world already in the stack (above).

**Lifecycle:**
- **Auto-created with commented defaults.** On first daemon start, if the file is absent, the
  daemon writes it out fully populated — every key below at its default, each with a one-line
  comment. So the shipped defaults are always visible and editable, never hidden in code.
- **Hand-edited in v1.** There is **no `packrat config` command in v1** — you edit the TOML in a
  text editor. A `packrat config get/set` (with validation) is a deferred nicety (see [roadmap](roadmap.md)) and the
  file format is forward-compatible with it.
- **Re-read at each job start.** The daemon reloads `config.toml` when a job begins, so an edit
  applies to the **next** scan/dedup/merge/cleanup with no daemon restart. A job already running
  keeps the snapshot it started with — which is exactly the config the audit trail records "in
  effect" for that run (see [dedup](workflow-dedup.md)). A malformed file → the job is rejected with a parse error naming the
  bad key, and the daemon keeps serving read-only queries with the last-good config.
- **Missing keys fall back to the built-in default** (the file need not be exhaustive); **unknown
  keys are ignored with a logged warning** (forward-compat / typo signal).

**Scope — global only.** Every knob here is collection-wide. The one *per-root* setting is the
`--ignore` glob list, which is bound to each root at `roots register` time and stored on the `roots` row
(see [scan](workflow-scan.md)), **not** in this file. (The `roots.ignore_globs` column and the deferred per-root scan
interval (see [data model](data-model.md)) are the only per-root config; everything else is global.)

**The knobs (defaults are the shipped values):**

```toml
[allowlist]
# Media extensions that become assets (see workflow-scan.md). Photo + video are the fixed default set.
raw = false            # include the RAW group (dng cr2 cr3 nef arw raf orf rw2 pef srw); needs rawpy
# photo/video extension lists are editable here too, but default to the workflow-scan.md closed sets.

[fastpath]
mtime_tolerance_s = 2  # tolerant-mtime skip window (see workflow-scan.md, step 4); 0 = strict path+size+mtime

[match]
t_photo_recompress = 10   # photo PDQ cutoff for dedup stage 2 (recompression band, see fingerprints.md / workflow-dedup.md)
t_photo_edit       = 32    # photo PDQ match cutoff (see fingerprints.md); recompress < d ≤ edit → stage 3 (minor edit)
t_match_video      = 90    # per-frame PDQ cutoff for video (see fingerprints.md); looser, the frame vote reclaims precision
pdq_max_edge       = 512   # downscale each image/frame to this longest edge before PDQ (~7x faster; 0 = full-res)
video_bitrate_tie_pct = 10.0  # video keep-lead (see workflow-dedup.md): effective-bitrates within this % tie → codec then path
# codec-efficiency weights for the video keep-lead effective bitrate (see workflow-dedup.md); unlisted codec → 1.0
[match.codec_weights]
h264 = 1.0
hevc = 2.0    # == h265 (same codec); ~2x more efficient than h264
av1  = 2.5
vp9  = 1.5
mpeg4 = 0.5

[video]
sample_frames        = 12    # frames sampled per video, at segment midpoints (see fingerprints.md)
duration_tol_s       = 1.0   # duration pre-filter: absolute floor (see fingerprints.md)
duration_tol_pct     = 5.0   # duration pre-filter: relative part (percent)
frame_match_fraction = 0.60  # ≥ this fraction of comparable frame-pairs must match
min_frame_quality    = 50    # PDQ quality gate; frames below are excluded from the vote
min_comparable_frames = 5    # fewer comparable pairs than this → no match (insufficient evidence)

[review]
low_quality_hint = 50  # photo PDQ quality below this flags a near-dup pair low_confidence (see fingerprints.md, annotate-only)

[smb]
scan_workers = 6       # concurrent hashing/decoding streams over SMB (see performance.md); 4–8 typical

[audit]
retention_days = 0     # 0 = keep review audits forever (see workflow-dedup.md); >0 = prune older (deferred knob, see roadmap.md #5)

[schedule]
# Background periodic jobs (see architecture.md scheduler / workflow-scan.md probe). Interval edits apply on the NEXT
# daemon restart (a background cadence, so no live reload in v1).
probe_interval_hours = 24     # run a probe sweep (one probe per enabled library root) every N hours
probe_enabled        = true   # off-switch for the scheduled probe (probe stays a manual CLI verb)
```

> **Defaults marked tuning-dependent** (`t_photo_recompress`, `t_photo_edit`, `t_match_video`, the
> `video.*` knobs, and the keep-lead `codec_weights` / `video_bitrate_tie_pct`) are **starting points
> to be calibrated on real data before the first full scan** (see [fingerprints](fingerprints.md), [dedup](workflow-dedup.md), [roadmap](roadmap.md) #1) — not
> claimed-correct constants. `mtime_tolerance_s`, `allowlist.raw`, `smb.scan_workers`, and
> `review.low_quality_hint` are ordinary operational settings.
