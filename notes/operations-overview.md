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
  silently collapsed. Near-dup relationships are found and acted on **only by `dedup`** (see [dedup](operation-dedup.md));
  `merge` does not consider them.
- **Trashed-hash exclusion** applies during **merge** (discard incoming exact-hash trash matches)
  and **cleanup** (delete library exact-hash trash matches). Trashed assets keep their
  fingerprints forever (physical file may be gone); this is what excludes re-appearing junk. Merge
  matches trash by **exact hash only** — a recompressed copy of trashed content is caught later by
  `dedup`, not by merge.

---

## Adding a folder to the collection

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
