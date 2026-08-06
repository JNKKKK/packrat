# Core workflows

This section specifies three behaviors, step by step, so the logic can be reviewed
for correctness:

- **A. Add a folder to the collection** — catalog an existing on-disk folder (`roots register` +
  `scan`). Pure indexing; it never moves, renames, copies, or deletes any file. Only the
  database changes. Scan writes **all per-asset fingerprint data** to the DB.
- **B. Dedup a single registered folder** — from the DB fingerprints (plus a liveness check),
  find the target folder's duplicates against the whole collection, stage removable copies as
  Explorer **shortcuts** inside the folder, and — after the user reviews and confirms — delete
  them (to Recycle Bin). One pending run per folder; pending → completed.
- **C. Merge a folder into an existing folder** — discard trash and copy into a destination only
  the files new to the *whole* collection, decided by **exact hash only** (no near-dup matching,
  no review). Read-only on the source; copy-only on the destination.

**Division of labor (important):** `scan` (A) produces *per-asset* data only — hash, metadata,
perceptual signatures. The *pairwise near-dup* matching (which asset is visually a near-dup of
which) is done **only** by `dedup` (B), over DB assets. `merge` (C) does **not** do near-dup
matching at all — it classifies incoming files purely by exact `content_hash` (dup-in-source /
trashed / exact-known / new), collapsing byte-identical duplicates but leaving recompressed
near-dups for `dedup`. The one kind of duplicate scan *does* resolve is **exact byte-identical**
files — that is identity assignment (a second `file_instance` on the same asset), enforced by
the `content_hash` unique index, not near-dup dedup.

All three rely on the **asset / file-instance** split and the identity rules below.

## Identity rules (used by all three workflows)
- **Exact identity** = BLAKE3 content hash. Files with the *same bytes* are the **same asset**
  with multiple **file instances** (e.g. the same photo living in two folders). Adding such a
  file never creates a second asset — it just adds a file-instance row pointing at the
  existing asset.
- **Near-duplicate** = *different bytes, visually the same* (recompressed / resized /
  re-encoded). These are **distinct assets** linked by a recorded **similarity edge**, never
  silently collapsed. Near-dup relationships are found and acted on **only by `dedup`** (see [dedup](workflow-dedup.md));
  `merge` does not consider them.
- **Trashed-hash exclusion** applies during **merge** (discard incoming exact-hash trash matches)
  and **cleanup** (delete library exact-hash trash matches). Trashed assets keep their
  fingerprints forever (physical file may be gone); this is what excludes re-appearing junk. Merge
  matches trash by **exact hash only** — a recompressed copy of trashed content is caught later by
  `dedup`, not by merge.

---

## A. Add a folder to the collection — two separate operations

Adding a folder is deliberately split into two commands so a cheap bookkeeping action is never
coupled to a multi-hour fingerprinting job:

- **`roots register`** — record the folder as a root. Metadata-only, instantaneous, touches no files.
- **`scan`** — walk a registered root and fingerprint its contents. This is the resumable,
  long-running indexing job. **It does not compute CLIP embeddings unless `--embed` is passed**
  — dedup never needs them, so the default scan stays lean.

Both are non-destructive: files are read-only, the only writes are to the packrat database.
(`roots register` is grouped under the `roots` command — the noun for root lifecycle/metadata —
alongside `roots list`; `scan` stays a flat top-level verb because it is a *job run against* a
root, not root bookkeeping. See [cli](cli.md).)

---

### `roots register` — declare a folder as a root (metadata-only)

```
packrat roots register "D:\Backup\iPhone"           # default kind: library
packrat roots register "D:\Backup\iPhone" --scan    # register, then immediately kick off a scan
```

1. Resolve the path to an absolute, long-path-safe form; require it to exist, be a directory,
   and be readable.
2. **Overlap check:** reject if the path is already a root, or is nested inside / contains an
   existing root (prevents double-indexing the same bytes under two roots).
3. **Unique-name check:** the folder's **leaf name** (the last path component, e.g. `iPhone`)
   must be globally unique across all roots, compared case-insensitively. So with
   `D:\Backup\iPhone` already registered, `D:\test\iPhone` is **rejected** even though it is a
   different path — the leaf `iPhone` collides. Rationale: the leaf name is used as the human-
   facing handle for a root, so it must be unambiguous. The error suggests either picking a
   differently-named folder or passing an explicit `--name <label>` to override the handle (the
   label, not the path, is what must be unique).
4. Insert a `roots` row: `path`, `name` (leaf name or `--name`), `kind=library`, `enabled=1`,
   `last_full_scan_at=NULL`. Bind the **ignore set** to the root (see below).
5. Report the root id/name and that it is registered but **not yet scanned** — nothing is
   walked or fingerprinted here. The root contributes nothing to dedup/merge until a `scan`
   completes. With `--scan`, immediately enqueue a `scan` job for this root (equivalent to
   running `packrat scan <path>` next) and stream its progress; `--scan --embed` also runs the
   embedding pass.

**What the ignore set is (and what "bind" means):** the ignore set is the filter that decides
which files a later `scan` will even *look at* — matched files are skipped entirely (never
hashed, fingerprinted, or turned into assets). It has two parts:
- **Junk/system exclusions** — `Thumbs.db`, `desktop.ini`, `.DS_Store`, hidden/system-attribute
  files, zero-byte files, and packrat's own staging area `_packrat_review\` (which contains dedup's
  per-stage folders `_exact_dup_to_delete\` / `_suspect_recompression\` / `_with_minor_edits\` and
  cleanup's `_perceptually_identified_trash\`) plus `.lnk` shortcuts.
- **Media extension allowlist** — only these become assets. The **default** is a fixed, closed
  set (case-insensitive), defined once here and reused everywhere:
  - **Photo:** `jpg jpeg jfif png gif bmp tif tiff webp avif heic heif`
  - **Video:** `mp4 m4v mov avi mkv webm wmv flv mpg mpeg m2ts mts ts 3gp`

  Anything else (`.txt`, `.zip`, `.pdf`, sidecars like `.aae`, etc.) is ignored. The set lives
  in config and can be edited, but the shipped default is exactly the two lists above — no
  open-ended "…".

  **Optional RAW group (off by default):** `dng cr2 cr3 nef arw raf orf rw2 pef srw`. Enable via
  config (`allowlist.raw = true`) when you want camera RAW files catalogued. It is opt-in
  because RAW needs a separate decode path (`rawpy`) for metadata/perceptual hashing, and many
  workflows keep RAW+JPEG pairs where you may not want both indexed.

There is a **global default** ignore set from config; "bind" simply records, on the root, which
set applies (the default, optionally extended with per-root patterns via `--ignore <glob>`). It
is stored at register time so every scan of that root reuses the same rules deterministically.

Note the two mechanisms differ in form: the **allowlist** is a set of file *extensions* (what
qualifies as media at all), while **`--ignore` patterns are gitignore-style path globs** (e.g.
`**/cache/**`, `*.tmp`, `Screenshots/`), not a comma-separated extension list. A file is scanned
only if its extension is in the allowlist AND it matches none of the ignore patterns.

Registering alone leaves the collection unchanged in content terms; it just tells packrat this
folder exists and how to treat it. Follow with `scan` (or use `roots register --scan`).

---

### `scan` — walk a registered root and fingerprint it (the indexing job)

```
packrat scan "D:\Backup\iPhone"     # incremental; fingerprint new/changed files. No embeddings.
packrat scan --all                  # scan every enabled root
packrat scan "D:\Backup\iPhone" --embed   # also compute CLIP embeddings for tagging (see embeddings.md)
```

Scan is purely per-asset: it fills in every fingerprint column for each file but computes **no
near-dup relationships** — those are the `dedup` operation's job (see [dedup](workflow-dedup.md)). The one exception is
exact byte-identity, which scan must resolve because it decides asset identity. Each step below
notes exactly what it writes.

**Phase 1 — Enumerate**
1. Resolve the target to a registered root (error if it isn't one — `roots register` it first).
   **Reject `kind='trash'` roots** — trash folders are transient inboxes indexed only by "refresh
   the trash collection" (see [trash refresh](trash-model.md)), never by `scan` (whose "index and keep" semantics would fight the
   "index then empty" model). → reads `roots` (match `path`); no write.
1a. **Per-root exclusivity check (see [architecture](architecture.md) guarantee 2).** If this root has an active operation — a
   `pending` `review_runs` row (dedup or cleanup) or an open `merge_runs` row
   (`status IN ('planning','copying')`) with this root as `dest_root_id` — **do not scan it**, so
   scan's deletion-detection (Phase 3 step 11) can never churn the `file_instances`/assets that an
   open review plan references. This is the **dequeue gate**, not a submit-time reject (see [architecture](architecture.md)
   guarantee 1): a **manual `scan <root>`** submitted against a held root is *enqueued* and then
   **held in the backlog** — skipped on each pump, shown as `blocked: root R has a pending <run> —
   confirm/cancel to unblock`, and run automatically once the holder clears (the confirm/cancel/merge
   job's completion pumps the queue). It does **not** error at submit. A **`--all` or scheduled**
   scan is different — it owns no single root (it iterates), so it can't sit blocked without stalling
   the whole sweep: it **skips this root at run time and logs the skip**, listing the skipped root in
   the report. → reads `review_runs`/`merge_runs`; no write.
2. Recursively walk the root, applying the ignore set, to build the candidate worklist.
   → no DB write (in-memory worklist).
3. Open a job row. → **write** `jobs`: `type='scan'`, `status='running'`, `total`=file count,
   `done`=0, `started_at`, `params_json`={root_id, full, embed}.

**Phase 2 — Per-file pipeline** (worker pool; checkpointed after each file)
For every candidate file:
4. **Fast-path skip (tolerant-mtime key).** If a `file_instances` row exists at this exact `path`,
   its `size` matches exactly, its `mtime` matches within a small **tolerance**
   (`fastpath.mtime_tolerance_s`, default 2 s), and its asset is **fully fingerprinted** (defined
   below) → **write** `file_instances.last_seen_at` (now) only; skip the rest. `--full` ignores the
   fast-path and re-fingerprints unconditionally.
   - **"Fully fingerprinted" — the predicate (authoritative; used here, in step 6's backfill
     exception, and by the undecodable-retry rule).** An asset is fully fingerprinted iff **either**:
     - **`undecodable=1`** — it is as fingerprinted as it will ever be: hash-only identity, no
       perceptual data *by design* (see [format coverage](tech-stack.md)). Treated as complete so a plain scan doesn't re-decode a
       known-bad file every pass; only **`scan --full`** retries it (step 8 retry note, below). **Or**
     - **`undecodable=0` AND its perceptual rows for its `media_type` are present:**
       - **photo** → the asset's single `phash` (PDQ) row exists. (Written in the *same*
         transaction as the asset in step 9, so it's all-or-nothing — a partial perceptual write
         is impossible.)
       - **video** → a `vphash` row exists for the asset. (Likewise written atomically in step 9,
         so any frame row present ⇒ the full sampled set is present.)
     **Embeddings are deliberately NOT part of this predicate** — they are opt-in and decoupled
     (see [fingerprints](fingerprints.md) / [embeddings](embeddings.md)). Requiring them would force every non-`--embed` scan to re-process every asset. The
     `--embed` pass has its own "no `embeddings` row yet" gate (Phase 3 step 10), independent of
     the fast-path.
   - **Consequence for merge-created assets.** A file copied by `merge` gets an `assets` row with
     `undecodable=0` and **no** `phash`/`vphash` yet (see [merge](workflow-merge.md) step 11), so it is **not** fully
     fingerprinted → the fast-path won't skip it → the next `scan <dest>` hashes it, hits the
     existing asset (step 6), and takes the **backfill exception** to fill perceptual data
     in place. This is exactly why the predicate must distinguish "no perceptual rows because
     not-yet-attempted" (fill it) from "no perceptual rows because undecodable" (leave it).
   - **Why exact `size` but tolerant `mtime`:** size is high-entropy for media (two different
     photos/videos almost never share a byte count), so it is the strong change signal; mtime is
     a *weaker corroborator* whose exact value is unreliable across SMB/exFAT (2 s FAT rounding,
     SMB precision differences, NAS-side tools rewriting timestamps). A real in-place edit moves
     mtime by far more than the tolerance, so it still trips re-fingerprinting; the tolerance only
     absorbs jitter, avoiding needless re-reads (expensive over the network — see [SMB/NAS performance](performance.md)).
   - **Residual blind spot (accepted):** a same-`path`, same-`size`, byte-different file whose
     mtime also lands within tolerance is skipped and its stored fingerprint goes stale. This is
     rare for media and is the reason the periodic **`--full` scan** (which re-hashes everything)
     exists as the backstop. Setting `mtime_tolerance_s=0` restores strict `path+size+mtime`.
4a. **Moved-file relink (metadata-only, no re-hash).** A file that was *moved/renamed* within the
   root fails step 4 — its **new** path has no `file_instances` row — so the naïve path re-hashes
   it over the network only to rediscover a known asset (step 6 hit → new instance; the old path's
   row is then forgotten by deletion-detection, step 11). This is already *correct*, just wasteful.
   When a candidate can be **proven** a relocation of a now-gone instance we skip the byte work and
   simply **update that row's `path`** (+ `filename`, `last_seen_at`), leaving `size`/`mtime`/the
   asset untouched. Because this asserts content identity from **metadata alone**, it fires only
   when the `(filename, size)` pair is unambiguous on *both* sides and mtime corroborates — a
   candidate `C` whose path is absent from the DB is a move of instance `O` iff **all** hold:
   - **(i) `C`'s `(normcase filename, exact size)` bucket holds exactly ONE DB instance** in this
     root (0 → genuinely new; ≥2 → the pair doesn't identify content here → hash it);
   - **(ii) that sole instance `O` is *gone*, not merely unreadable** — its path was **not**
     enumerated this pass **and** is not under a **suppressed** (errored/ignored) subtree (see [SMB/NAS performance](performance.md)).
     A still-present sole match means `C` is a **copy**, not a move (→ hash it); a suppressed origin
     may still exist on disk, so relinking it would relocate a live row's path;
   - **(iii) mtime corroborates** — `|O.mtime − C.mtime| ≤ mtime_tolerance_s` (same tolerant window
     as step 4; a rename preserves mtime, a cross-fs move may round it);
   - **(iv) `O`'s asset is *fully fingerprinted*** (step-4 predicate) — else fall through so the
     step-6 miss/backfill path decodes its perceptual data (a merge-created / undecodable asset);
   - **(v) exactly ONE path-absent candidate shares the bucket** (`C` itself) — a file copied into
     two new spots gives two candidates; only one can be the move, so hash them all;
   - **(vi) the origin asset is `active`, not `trashed`** — a moved file of a trashed asset is a
     trash re-appearance the banner must count as `matches-trashed` (Phase 4); a metadata-only
     relink would silently drop that signal, so a trashed origin falls through to the hash path
     (which hits the trashed asset and counts it). Trashed origins are rare/short-lived, so the
     re-hash costs almost nothing.
   Every failed condition **falls through to the ordinary hash path (steps 5–9), which is always
   correct** — so 4a only ever *removes* byte work in the provable case and never changes an
   outcome. The `(filename,size)` bucketing lets a tolerant-mtime match use a hashable key; the
   live-side ambiguity check (i–ii) is bucket-level (no mtime filter) because tolerance isn't
   transitive, so a live collision must veto regardless of its exact mtime. **`--full` disables 4a**
   (re-hashing is forced). Same **residual blind spot** as step 4 — two distinct files sharing
   filename+size+mtime-in-tolerance — with the same `--full` backstop; the move key is *stronger*
   evidence than the same-path skip (it adds filename equality + a gone origin), but it is a genuinely
   new cross-path identity assertion, hence the strict two-sided uniqueness guard. A relinked move is
   **dedup-neutral** (same asset, no new content) so it does **not** set `roots.needs_dedup` (see [tui](tui.md)).
   → **write** `file_instances.path`/`filename`/`last_seen_at` for `O`; counted `moved` in the report.
5. **Content hash** — BLAKE3, streamed. → no write yet (value held for step 6).
6. **Exact-dup resolution.** Look up `assets.content_hash`.
   - **Hit** → this is another copy of a known asset: **upsert** a `file_instances` row
     **keyed by (`root_id`, `path`)** — insert if no row exists at this path, else update the
     existing row's `asset_id`/`size`/`mtime`/`last_seen_at` in place — then normally **stop** (no
     metadata/perceptual work). Upsert-by-path (not blind insert) makes re-encountering a
     known file idempotent: a `--full` re-hash, an mtime-drift re-hash, or a merge-created file's
     first backfill scan (case (a) below) all already have a row at this path and must reuse it,
     never create a second instance of the same physical file. If the hit asset was `trashed`,
     this is a re-appeared trashed fingerprint — see Phase 4. This is how identical bytes in two
     *different* paths become one asset with two instances (enforced by the `content_hash` unique
     index on assets; `file_instances` is unique on (`root_id`,`path`)).
     - **Backfill exception (a hit that should still (re)compute perceptual data).** After
       attaching the instance, **continue to steps 7–8** and in step 9 **update the existing asset
       in place** (write/replace `phash`/`vphash`, refresh metadata, set/clear
       `undecodable`/`decode_error`) — *not* insert a new asset — when the hit asset is either:
       - **(a) not-yet-fingerprinted:** `undecodable=0` with **no** perceptual rows — characteristically
         a merge-created asset (see [merge](workflow-merge.md) step 11). Fires on **any** scan (incremental or `--full`): such
         an asset fails the step-4 predicate, so an unchanged merge-created file reaches step 6 even
         on a plain incremental scan, and gets filled in here.
       - **(b) undecodable retry:** `undecodable=1` **and this is `--full`** — re-attempt decode
         after a decoder/library upgrade; on success clear `undecodable` and write phash, on failure
         leave `undecodable=1` with a refreshed `decode_error`. (A plain incremental scan does **not**
         retry undecodables — the step-4 predicate treats them as complete.)
       Otherwise the hit **stops early** (the normal case): a decodable, perceptually-complete asset
       has byte-identical content, so there is nothing to redo — true even under `--full`, whose job
       is to catch byte *changes*, which surface as a hash **miss** (or a hit on a *different* asset),
       never as a hit on this same asset.
   - **Miss** → continue; create the asset in step 9.
7. **Metadata** — decode/probe for dimensions, duration, capture time, codec (exiftool /
   ffprobe). → values held for step 9 (→ `assets.width/height/duration_s/captured_at`, `size`).
   (`media_type` is decided by **extension** via the allowlist — see roots register above — not by decoding, so it is
   known even for files that won't decode.)
8. **Perceptual signature** — photo: PDQ + quality; video: duration + PDQ (with quality) of each of
   the `video.sample_frames` frames sampled at fixed timeline fractions (see [fingerprints](fingerprints.md)). → values held for
   step 9 (→ `phash` / `vphash` rows). *No near-dup comparison here.*
   - **Video `codec` (see [dedup](workflow-dedup.md) stage-2 keep-lead), same decode pass.** For a **video** that decodes,
     capture the video stream's `codec` name (`h264`/`hevc`/`av1`/…) from the already-open decoder —
     free, no extra work. → value held for step 9 (→ `assets.codec`). Feeds the video keep-lead's
     codec-efficiency weight (see [dedup](workflow-dedup.md)). **Photo and undecodable → NULL.**
   - **Decode failure (graceful, see [format coverage](tech-stack.md)):** if the pixels/frames won't decode (corrupt file,
     unsupported codec, missing wheel), **do not crash and do not abort the asset** — the BLAKE3
     hash (step 5) already gives it identity. Record it in step 9 with **`undecodable=1`**, the
     `decode_error` detail, and **no `phash`/`vphash` rows**. Metadata (step 7) is best-effort:
     keep whatever `exiftool`/`ffprobe` returned (they often read headers of files Pillow/PyAV
     can't fully decode); leave the rest NULL. Log and move on.
9. **Persist the new asset (single transaction).** → **write**:
   - `assets`: `content_hash`, `media_type`, `size`, `width`, `height`, `duration_s`,
     `captured_at`, `status='active'`, `added_at`, `undecodable` (0 normally, 1 on step-8 decode
     failure), `decode_error` (NULL unless undecodable), `codec` (video only, from step 8).
   - `file_instances`: `asset_id`, `root_id`, `path`, `filename`, `size`, `mtime`,
     `last_seen_at`.
   - `phash` (photo only): the single PDQ row — (`asset_id`, `algo='pdq'`, `bits`, `quality`).
     **Omitted entirely if `undecodable=1`.**
   - `vphash` (video only): one row per sampled frame — (`asset_id`, `frame_index`,
     `t_offset_s`, `pdq_bits`, `quality`). **Omitted entirely if `undecodable=1`.** A video that
     decodes but yields **zero** usable frames (all failed to decode) is treated as undecodable.
   Then **write** `jobs.done += 1` (progress-bar counter — see [data model](data-model.md); the *durable* record that this
   file is done is the committed `file_instances`/asset rows above, which the fast-path reads on
   re-run, not `done`).

   **Retrying undecodables:** an `undecodable=1` asset has no perceptual rows *permanently*, so the
   fast-path (step 4) treats it as "fully fingerprinted" and won't re-decode it every scan (step 4 /
   gap-#3 predicate, above). To force a retry after a decoder/library upgrade (e.g. a new
   `pillow-heif` that now handles a format), run **`scan --full`**, which bypasses the fast-path,
   re-attempts decode, and **clears `undecodable`/`decode_error` and writes phash rows** if it now
   succeeds. A plain incremental scan never retries them.

*(Near-dup linking is intentionally absent — it is the `dedup` operation (see [dedup](workflow-dedup.md)), which writes the
`similarity_edges` table from this data. Scan never writes similarity edges.)*

**Phase 3 — Embeddings (only if `--embed`)**
10. **By default skipped entirely — no embeddings computed, no `embeddings` rows written.** With
    `--embed`, assets with no current `embeddings` row for the active model **and `undecodable=0`**
    are queued for a batched CLIP pass → **write** `embeddings`: (`asset_id`, `model`, `vector`).
    (Undecodable assets are skipped — CLIP needs a decoded frame, which is exactly what failed.)
    Fully decoupled: skipping or failing this leaves every dedup/merge result identical;
    backfillable later.
11. **Deletion detection (every completed scan of a reachable root — not just `--full`).**
    Reconcile files removed from disk since last scan. This needs **no re-hashing**: enumeration
    (Phase 1 step 2) walks the whole tree on *every* scan, and every present file has its
    `file_instances.last_seen_at` bumped this pass (step 4 fast-path or step 9). So gone files are
    simply the rows this scan never touched:
    - `DELETE FROM file_instances WHERE root_id=? AND last_seen_at < <this scan's start time>`
      **AND the instance's parent directory was cleanly enumerated this pass** (see guard) — i.e.
      any instance under a fully-listed directory not seen this pass → **delete the row**.
    - Then for each affected asset: if it is `active` and now has **zero** instances anywhere →
      **delete the asset** (cascading `phash`/`vphash`/`embeddings`/`similarity_edges`) — it is
      forgotten, not remembered as missing (see [trash model](trash-model.md): a plain filesystem delete is not trash). A
      `trashed` asset at zero instances is left intact (trash memory).
    On a `--full` scan, additionally **write** `roots.last_full_scan_at`.
    (`--full` governs re-*hashing* via the fast-path bypass; it does **not** govern deletion
    detection, which keys off enumeration + `last_seen_at` and therefore runs on incremental scans
    too.)
    **Guard (per-directory, see [SMB/NAS performance](performance.md)):** reconcile an instance only if its **containing directory was
    fully and cleanly enumerated this pass**. Skip (leave untouched, report) instances under any
    directory whose listing errored/timed out, and under a fully offline/unreadable root skip
    everything — so incomplete data is never mistaken for "files deleted," and one flaky folder on a
    large NAS root no longer disables reconciliation for the whole root (only that subtree). Track
    the cleanly-enumerated directory set in Phase 1. (Separately, a root under an open review/merge
    never reaches this step at all — step 1a refuses to scan it — so deletion-detection cannot churn
    an active plan's referenced rows; that is a distinct reason to skip.)
12. Close the job → **write** `jobs.status='done'`, `finished_at` (or `status='error'`, `error`).

**Phase 4 — Trashed-fingerprint handling**
13. If a file's `content_hash` matches an asset already `status='trashed'`, step 6 attached the
    new `file_instances` row to that **trashed** asset — it does **not** flip to `active` (the
    user trashed this content; re-appearing on disk doesn't un-trash it). The file physically
    exists but the collection still treats the content as trash. → counted as `matches-trashed`
    in the report; no status change. Remove these re-appearances with **`packrat cleanup <folder>`**
    (see [trash model](trash-model.md)), which deletes library files whose content is trashed.

**Phase 5 — Report**
14. Summarize: new assets, files that were exact-dups of a known asset (new instance only),
    non-media skipped, undecodable/corrupt errors, `matches-trashed` count, embeddings computed
    (`--embed`) or deferred. **No near-dup clustering here** — that is reported by `dedup`.
    **Nothing on disk changed.** (The user-facing banner phrases these as `N new`, `N exact-dup
    instances`, `N filled in missing fingerprints`, `N identified trash`, `N undecodable`, etc.)
15. **Persist the report (see [data model](data-model.md) `scan_results` / `scan_problem_files`).** After the per-root loop (so
    a *completed* scan only — dry-run/cancel/interrupt/error persist nothing), write one
    `scan_results` row per root scanned: the banner counts + flags + (if `--profile`) the profiler
    snapshot, plus a `scan_problem_files` row per problematic file (path + reason). This lets
    `status <root>` and the M6 TUI re-render the scan later. **The undecodable set is re-derived
    from the catalog here, not taken from this pass's activity** (see [data model](data-model.md) scan_results note): a resume /
    incremental re-run fast-path-skips undecodables (step 4), so a per-pass count would wrongly read
    as zero on re-run — reading committed `assets.undecodable=1` (with a live instance in the root)
    instead makes the report describe the root's *current* state, stable across resumes. `read-error`
    files (unreadable bytes, no asset) stay per-pass. Persist tolerates a closed DB on shutdown (like
    the worker progress writes) so a stop at the finish line can't flip a `done` scan to `error`.

**Idempotency & resume:** re-running `scan` on the same root is a no-op except for genuinely
new/changed files — the **fast-path** (step 4) skips the rest, which is what makes an interrupted
scan effectively resume: already-persisted files are cheap no-ops on the next pass, so the work
picks up where it stopped without any explicit cursor. (`jobs.done` is only the progress number,
see [data model](data-model.md) — not the resume key.) If the daemon died mid-scan, startup reconciliation flips the stale
`running` row to `interrupted` (see [architecture](architecture.md)); the next `scan` (manual or scheduled) then continues via the
fast-path. Re-running `roots register` on an existing root is rejected by the overlap check.

---

### `probe` — cheap discovery: is there anything new here worth a scan?

```
packrat probe "D:\Backup\iPhone"     # count new/changed files; write NO fingerprints
packrat probe --all                  # probe every enabled library root (one job each)
```

`probe` (`jobs/probe.py`) splits the two halves `scan` conflates — *discovery* (walk + notice
new paths; seconds, no per-file I/O) and *fingerprinting* (hash + decode + PDQ; the multi-hour
cost). Probe is **discovery-only**: it answers one question about a root — *are there files here
we haven't scanned yet?* — and records a per-root "new files waiting" signal (the TUI surfaces it
as a status-dot state, see [tui](tui.md)), so the user learns a root needs a `scan` without remembering they
dropped files in. Runs every 24 h per root via the scheduler (see [architecture](architecture.md)); never blocks the user (press
`[s]`/`packrat scan` to scan now). CLI-exposed; **no** TUI keyshortcut (background-only).

**What it does.** Reuse scan's `enumerate_root(root_path, ignore)` (the walk + allowlist + ignore
filter — Phase 1) to build the candidate list, then per candidate apply scan's **existing
fast-path skip predicate** (path + exact size + tolerant mtime + "fully fingerprinted", step 4)
to decide *known* vs *new/changed*. Count the news. **No BLAKE3, no decode, no PDQ, no
`assets`/`phash`/`vphash` writes** — that is exactly the line between probe and scan. The
predicate is factored into a shared helper (`scan.load_existing_instances` +
`scan.is_fastpath_hit`) that both scan and probe call, so **"probe says N" ⇒ "scan would
fingerprint ≥ N"** holds by construction — no second copy of the rule.

- **"New"** = a candidate with no matching live `file_instances` row, OR a matching row whose
  size/mtime drifted past tolerance (a changed file also needs re-scanning).
- Probe does **no deletion-detection** (that mutates the catalog — scan's job, needs the full
  pass). Probe is **read-only on the catalog**; its only write is the per-root signal.
- **Trash roots:** never probed (scan never touches `kind='trash'` — see [trash refresh](trash-model.md)); `probe --all`
  iterates `enabled=1 AND kind='library'`.
- **Offline / unreadable root** (SMB blip, see [SMB/NAS performance](performance.md)): report `root_offline`, write **no** signal —
  absence of a readable listing ≠ "no new files"; never let an unreachable root read as "clean".

**Per-root exclusivity — probe OWNS its root.** `owned_root=root_id`, so a probe **waits in the
backlog until its root is idle** (no running scan/dedup/cleanup/merge on it) via the existing
dequeue gate (see [architecture](architecture.md) guarantee 2), exactly like scan. Rationale for N per-root probe jobs over one
`probe --all` sweep: a single sweep owns no root and *iterates*, so at a busy root it must either
skip it (a silent miss) or stall the whole sweep; N per-root jobs sidestep it — each is an
independent queue entry the scheduler holds until *its* root frees. The cost ("100 roots → 100
jobs / 24 h") is bounded by the submit-dedup below. Probe is sub-second-to-seconds, so the brief
block it imposes is negligible; non-destructive → reconcile drains a queued probe normally, an
interrupted running probe just re-runs (idempotent — recomputes from scratch, writes nothing else).

**The signal.** On **clean completion** (not offline): set `last_probe_at=now` and
`probe_new_count=<n found>` — which may be **0** (found nothing). Writing 0 is correct and
important: it means "a probe ran and there's nothing unscanned," so the dot stays whatever the
scan/dedup state says (see [tui](tui.md)). **A completed `scan` clears `probe_new_count=0`** (Phase 5, alongside
`last_full_scan_at`; skipped for a dry-run or an offline root) — the news are now fingerprinted.
Because the count is self-clearing, `count > 0` *is* the "latest meaningful op is a
probe-with-news" state — no `last_activity` column needed, and it stays honest if the user deletes
the new files and re-probes (re-enumeration finds 0 → dot reverts).

**Submit-time dedup — "one pending probe per root".** The queue never dedups in general (every
submission is enqueued — see [architecture](architecture.md) guarantee 1). A **narrow, probe-only** exception in `JobQueue.submit`
(`_dedup_probe`): if an un-started `probe` job for the same `root_id` is already `status='queued'`,
skip the insert and return that job's id. Match `queued` only — **not** a *running* probe (a fresh
queued one after it is legitimate; files may have arrived after it started). This bounds the "100
roots" backlog: a root whose probe from yesterday is still `queued` (worker backed up) gets a no-op
today. Scan/dedup/merge still enqueue freely.

**Result / CLI / API.** `result_json`: `{op:"probe", new_count, root_offline, candidates}` for the
[tui](tui.md) job card. `packrat probe <root>`/`--all`/`--detach`/`--json`, `client.submit_probe` (returns a
list of job ids), `POST /probe` (does the `--all` fan-out to per-root submissions). [goals and concepts](goals-and-concepts.md) parity
holds: probe is a first-class CLI verb; the TUI only *reflects* its result in the dot, and the
user's manual equivalent is `[s]` scan.

---
