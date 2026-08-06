# `scan` — walk a registered root and fingerprint it (the indexing job)

```
packrat scan "D:\Backup\iPhone"     # incremental; fingerprint new/changed files. No embeddings.
packrat scan --all                  # scan every enabled root
packrat scan "D:\Backup\iPhone" --embed   # also compute CLIP embeddings for tagging (see embeddings.md)
```

Scan is purely per-asset: it fills in every fingerprint column for each file but computes **no
near-dup relationships** — those are the `dedup` operation's job (see [dedup](operation-dedup.md)). The one exception is
exact byte-identity, which scan must resolve because it decides asset identity. Each step below
notes exactly what it writes.

**Phase 1 — Enumerate**
1. Resolve the target to a registered root (error if it isn't one — `roots register` it first).
   **Reject `kind='trash'` roots** — trash folders are transient inboxes indexed only by "refresh
   the trash collection" (see [trash refresh](operation-trash-refresh.md)), never by `scan` (whose "index and keep" semantics would fight the
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
     `undecodable=0` and **no** `phash`/`vphash` yet (see [merge](operation-merge.md) step 11), so it is **not** fully
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
     - **In-place content edit (same path, changed bytes).** If a row already exists at this path but
       the bytes changed, the hash misses this path's old asset and either hits a *different* asset or
       misses entirely; the upsert repoints the row to the new/other asset. In the **same
       transaction**, if that repoint left the *previous* asset `active` with zero remaining
       instances, it is **forgotten** (asset + fingerprints deleted) — the deletion-detection in
       step 11 can't catch this (the path is still present), so the upsert forgets the orphan directly.
     - **Backfill exception (a hit that should still (re)compute perceptual data).** After
       attaching the instance, **continue to steps 7–8** and in step 9 **update the existing asset
       in place** (write/replace `phash`/`vphash`, refresh metadata, set/clear
       `undecodable`/`decode_error`) — *not* insert a new asset — when the hit asset is either:
       - **(a) not-yet-fingerprinted:** `undecodable=0` with **no** perceptual rows — characteristically
         a merge-created asset (see [merge](operation-merge.md) step 11). Fires on **any** scan (incremental or `--full`): such
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
7. **Metadata** — dimensions, duration, capture time, codec, read **inline in the decode pass**:
   PIL EXIF for photos, PyAV for videos. No per-file `exiftool`/`ffprobe` subprocess (which would cost
   an extra SMB round-trip per file). → values held for step 9 (→
   `assets.width/height/duration_s/captured_at`, `size`).
   (`media_type` is decided by **extension** via the allowlist — see [roots register](operation-register.md) — not by decoding, so it is
   known even for files that won't decode.)
8. **Perceptual signature** — photo: PDQ + quality; video: duration + PDQ (with quality) of each of
   the `video.sample_frames` frames sampled at fixed timeline fractions (see [fingerprints](fingerprints.md)). → values held for
   step 9 (→ `phash` / `vphash` rows). *No near-dup comparison here.*
   - **Video `codec` (see [dedup](operation-dedup.md) stage-2 keep-lead), same decode pass.** For a **video** that decodes,
     capture the video stream's `codec` name (`h264`/`hevc`/`av1`/…) from the already-open decoder —
     free, no extra work. → value held for step 9 (→ `assets.codec`). Feeds the video keep-lead's
     codec-efficiency weight (see [dedup](operation-dedup.md)). **Photos → NULL; a truly-unopenable
     file → NULL** (a video that opens but yields zero decodable frames is flagged `undecodable=1` yet
     keeps the codec its decoder already reported).
   - **Decode failure (graceful, see [format coverage](tech-stack.md)):** if the pixels/frames won't decode (corrupt file,
     unsupported codec, missing wheel), **do not crash and do not abort the asset** — the BLAKE3
     hash (step 5) already gives it identity. Record it in step 9 with **`undecodable=1`**, the
     `decode_error` detail, and **no `phash`/`vphash` rows**. Metadata (step 7) is best-effort:
     keep whatever the inline PIL/PyAV read returned before it failed; leave the rest NULL. Log and
     move on.
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

*(Near-dup linking is intentionally absent — it is the `dedup` operation (see [dedup](operation-dedup.md)), which writes the
`similarity_edges` table from this data. Scan never writes similarity edges.)*

**Phase 3 — Embeddings (only if `--embed`)**
10. **Deferred — not yet implemented.** A plain scan computes no embeddings and writes no
    `embeddings` rows; passing `--embed` today only logs "note: --embed pass is deferred; scan wrote
    no embeddings" and moves on. **As designed** (once built), `--embed` would queue assets with no
    current `embeddings` row for the active model **and `undecodable=0`** for a batched CLIP pass →
    **write** `embeddings`: (`asset_id`, `model`, `vector`); undecodable assets skipped (CLIP needs a
    decoded frame). It is fully decoupled — computing it or not leaves every dedup/merge result
    identical, and it is backfillable. See [embeddings](embeddings.md).
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
    (see [cleanup](operation-cleanup.md)), which deletes library files whose content is trashed.

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
