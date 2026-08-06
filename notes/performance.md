# Performance & safety

**Performance (100K+)**
- First full scan: hours (video decode bound); checkpointed & resumable. Embeddings excluded
  unless `--embed`.
- Incremental scans: seconds–minutes via the tolerant `path`+`size`+near-`mtime` fast-path.
- CLIP batched on the RTX handles thousands of images/sec; video is the cost center — sample
  few frames, cache aggressively.
- All fingerprints cached; nothing is recomputed unless the file changed (per the fast-path key).

**Safety**
- Merge never writes to or deletes from the temp source; copies are hash-verified after write;
  destination name collisions get a numeric suffix.
- Destructive ops support `--dry-run` and require confirmation; deletes prefer Recycle Bin.
- DB is the crown jewel: WAL mode, periodic `VACUUM`/integrity check, and an automatic
  backup of the DB before every merge/trash-commit.

**Deletion target — Recycle Bin where it exists, permanent on NAS/SMB (accepted).** Every
"move to Recycle Bin" in this plan (dedup, cleanup, trash refresh — [trash model](operation-cleanup.md), [dedup workflow](operation-dedup.md)/[trash model](operation-trash-refresh.md)) means: attempt
the Windows Recycle Bin (via `send2trash`/`SHFileOperation`). **Windows provides no Recycle Bin
for network locations** (UNC / mapped-drive paths), and per the SMB / NAS performance section below most roots live on the Synology
NAS — so for those files the delete is **permanent** (the shell either errors or hard-deletes).
This is **accepted for v1**, not worked around (no packrat-managed quarantine/`.recycle` folder):
- **Local roots** (NTFS on a fixed/USB disk) → real Recycle Bin, recoverable as the tenets imply.
- **NAS/SMB roots** → **permanent deletion.** The typed-confirmation gate (dedup/cleanup) and
  `--dry-run` are therefore the *real* safety net for network roots, and the tools say so at the
  confirm prompt: **the summary must warn when any file in the delete set is on a non-recyclable
  (network) path** — e.g. "N of M files are on a network share and will be deleted PERMANENTLY
  (no Recycle Bin)." Implementation: detect per-path whether a Recycle Bin is available (network
  vs. fixed volume) and count/flag network-path deletions in the confirm summary.
- Merge is unaffected — it is copy-only and never deletes from a root.
- The DB backup before every destructive op (above) is what makes the *catalog* recoverable
  regardless; the *files* on a NAS are not.

---

## SMB / NAS performance (most roots on a Synology NAS)

Most registered folders live on SMB shares served by a Synology NAS, so packrat must be tuned
for SMB's cost model, which differs sharply from local NTFS:

- **Metadata is latency-bound.** A bare per-file `stat()` round-trip is ~0.3–2 ms on a LAN
  (vs. microseconds locally). Individually trivial, but ×100K done serially = minutes of pure
  waiting.
- **File *data* is bandwidth-bound.** Reading bytes runs at link speed — gigabit ≈ 110 MB/s,
  2.5GbE ≈ 280 MB/s.

Mapping this onto packrat's operations:

| Operation | Dominant SMB cost | Verdict |
|---|---|---|
| `roots register` | none | trivial |
| **First full `scan`** | transferring **every byte** to BLAKE3 + decode | the real cost; hours, bandwidth-bound |
| Incremental `scan` | directory enumeration (size+mtime) | seconds–minutes *if enumerated, not per-file stat'd* |
| dedup Phase 4/6 stats | a few hundred/thousand round-trips, deferred + concurrent | sub-second to seconds |

**Rules the implementation must follow:**

1. **Enumerate directories; never per-file `stat` for the fast-path.** Use `os.scandir()` /
   `FindFirstFile`/`FindNextFile`, whose SMB2 *query-directory* response returns name + size +
   mtime **in one batched round-trip per directory** (Python's `DirEntry` caches these on
   Windows). An incremental scan that changes nothing then costs ~one enumeration per directory,
   not 100K stats. This is the single most important SMB detail — and it is exactly why the
   fast-path key is `path`+`size`+near-`mtime` (all available from enumeration, no extra I/O).
2. **Parallelize the byte-bound work.** SMB services concurrent requests happily, so multiple
   hashing/decoding streams hide latency and saturate the link. Cap concurrency
   (`smb.scan_workers`, default e.g. 4–8) so the NAS/array isn't thrashed.
3. **Keep the connection warm.** The daemon holds the share mounted; never remount per file.
   Expect the *first* access after HDD spin-down to pay a one-time array wake (seconds).
4. **Lean on incrementals.** Only the first full scan pays the byte-transfer cost; afterward only
   new/changed files are hashed. This is why the tolerant-mtime fast-path matters — it prevents
   spurious re-reads (each wrongly-invalidated file is a full byte transfer over the wire).

**SMB-specific correctness hardening (matters more than raw speed):**

- **Enumeration errors must never be read as deletions.** A NAS blip, timeout, or partial
  listing mid-scan could make files *look* absent → deletion-detection would wrongly forget
  fingerprints (see [data model](data-model.md)). Rule: **an enumeration error/timeout suppresses deletion-detection only for
  the affected directory subtree, not the whole root** (fail-safe — never delete-and-forget on
  incomplete data). Because a `file_instances` row belongs to a specific directory (its path's
  parent), a gone instance is deleted **only if its containing directory was cleanly enumerated
  this pass**; instances under any directory that errored/timed out are left untouched. So one
  flaky folder on a large NAS root no longer disables deletion-detection for the entire root — only
  that subtree is skipped (and reported), while the cleanly-listed rest reconciles normally. A
  *fully* offline/unreadable root degenerates to the [data model](data-model.md) whole-root guard (every directory failed →
  nothing reconciled), which this generalizes from "root offline" to "per-directory incomplete
  listing." **Implementation note:** track the set of cleanly-enumerated directories during Phase 1
  and scope the Phase-3 step-11 `DELETE … WHERE last_seen_at < start` to instances whose parent dir
  is in that set.
- **mtime stability.** The fast-path already tolerates small mtime jitter (see [scan workflow](operation-scan.md), step 4). A
  NAS-side reindex or an rsync that rewrites timestamps by more than the tolerance will force
  re-fingerprinting of those files — correct but costly; note it if you run such tools.
