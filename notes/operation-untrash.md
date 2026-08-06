# `packrat untrash <path>` — forget content from trash memory

The reversal for an accidental trash (a file dropped in the wrong folder, a `dedup`/`cleanup`
perceptual discard you regret). Its job is narrow and precise: **remove a fingerprint from the
permanent trashed-hash set** so the content is no longer excluded from future merges.

**What untrash is NOT — it does not restore bytes.** A trashed asset stores only *fingerprints*
(hash, PDQ), never pixels — so packrat cannot reconstruct, preview, or recover the file itself.
Getting the *file* back is the Recycle Bin's job (where one exists — see [performance & safety](performance.md)), entirely separate. So
untrash never previews and never writes to disk; it only edits DB rows. This is why identification
is **by presenting the file**, not by browsing a gallery of ghosts:

```
packrat untrash "R:\recovered\IMG_4471.jpg"     # one file
packrat untrash "R:\recovered\2019\"            # every media file under a folder (recursive)
packrat untrash "…" --dry-run                   # report what would be forgotten; change nothing
```

**The path is just bytes to hash — it need NOT be a registered root, and untrash does not
catalog it.** This is the key difference from `scan`/`cleanup` (which operate on the catalog):
untrash reads arbitrary files off disk *purely to compute their BLAKE3* for a trash-memory lookup.
The file you're holding (pulled from the Recycle Bin, still on the iPhone, recovered anywhere) *is*
the identifier — the real thing stands in for a preview packrat can't produce. It's fine — expected,
even — for `<path>` to point outside every root.

**Procedure:**
1. Resolve `<path>`: a file, or a folder walked recursively with the **same allowlist/ignore rules
   as scan** (see [scan](operation-scan.md)) so non-media is skipped. Error if the path doesn't exist / isn't readable.
   (No root resolution, no overlap check — the location is irrelevant.)
2. For each file, compute **BLAKE3** (no metadata, no perceptual — exact-hash match only, chosen in
   the [performance & safety](performance.md) gap review: false-positive-free, like `cleanup`'s default mode) and look it up in
   `assets.content_hash`:
   - **Matches a `trashed` asset** → untrash it (per-asset rule below). Count as `untrashed`.
   - **Matches an `active` asset** → already not trash; no-op, count as `already-active`.
   - **No match** → packrat never knew this content (or already forgot it); no-op, count as
     `unknown`. (Untrash **never creates** an asset — presenting a novel file just does nothing.)
3. **Per-asset untrash rule** (mirrors [data model](data-model.md)'s forget/keep logic, inverted):
   - **Trashed asset still has ≥1 live `file_instances` row** (e.g. refresh flipped a library
     folder to `trashed` but no `cleanup` has deleted the files yet) → flip **`status` back to
     `active`**, clear `trashed_at`/`trash_reason`, **retain fingerprints** (they're valid). It
     simply rejoins the collection in place — nothing was lost.
   - **Trashed asset with zero instances** (the physical copies were emptied/deleted) → **forget it
     entirely**: delete the asset and its dependent rows (`phash`/`vphash`/`embeddings`/
     `similarity_edges`, via `ON DELETE CASCADE`). There is nothing to reactivate — the bytes are
     gone — so we drop the blocklist entry and let the content be treated as **brand-new** if it
     ever reappears in a future merge/scan (exactly the plain-Explorer-delete "forget" model — see [trash model](trash-model.md)). This is the case that resolves the gap: the *hash* stops excluding re-imports.
4. **Report:** `untrashed` (reactivated in place), `forgotten` (zero-instance, blocklist entry
   dropped), `already-active`, `unknown`. **Nothing on disk changed.**

**Safety & interactions:**
- **Non-destructive to files by construction** — untrash only reads (to hash) and writes DB rows;
  it moves/deletes nothing. No typed confirmation needed for the file/dry-run path. *(A future
  batch mode — `--since`/`--reason` — that forgets many entries without presenting files would want
  a count-confirm; deferred — see [roadmap](roadmap.md).)*
- **Per-root exclusivity (see [architecture](architecture.md)):** untrash is a mutating job (takes a global worker slot), but it
  targets *no* root, so it acquires **no** per-root ownership and is never blocked by / never blocks
  a pending review or merge. It touches only `assets`/fingerprint rows by hash.
- **`--dry-run`** reports the same counts without modifying the DB. (Unlike `cleanup`/`merge`,
  untrash does **not** call refresh, so its dry-run truly changes nothing — the refresh procedure's
  always-absorb rule doesn't apply here.)
