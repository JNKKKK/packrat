# Refresh the trash collection (`packrat trash refresh`)

This is the step that turns files-sitting-in-a-trash-folder into permanent trashed fingerprints.
It is invoked automatically at the start of **`cleanup`** and **`merge`** (and exposed directly
as `packrat trash refresh [<root>]` — see [CLI](cli.md)). Steps:

1. For **every** registered `kind='trash'` root (or, when `packrat trash refresh <root>` scopes it,
   that **single** trash root — see [CLI](cli.md)), enumerate its files (same allowlist/ignore rules as scan).
   `cleanup`/`merge` always invoke the all-roots form; only the standalone verb (and the TUI's
   trash-root modal) may scope to one. For each file:
   - Compute BLAKE3 + perceptual signature (photo PDQ; video per-frame PDQ). **No embedding.**
   - Resolve against `assets.content_hash`:
     - **New content** → create an asset with `status='trashed'`, `trashed_at`,
       `trash_reason='trash-folder'`, and persist its `phash`/`vphash` (so perceptual trash
       exclusion works in merge).
     - **Matches an existing `active` asset** → flip it to `status='trashed'` (the user is telling
       us this content is junk); retain its fingerprints. Its library-folder instances remain on
       disk until a `cleanup` removes them.
     - **Matches an existing `trashed` asset** → already remembered; nothing to add.
2. **Physically remove all files from every trash root** (to Recycle Bin). Their fingerprints now
   live forever in the trashed set, so the actual files are no longer needed — the folder is
   emptied, ready for the next drop.
   - **Crash-safety ordering (required):** step 1 (record the hash → DB, committed) must complete
     **before** step 2 deletes that file. Never delete first — a crash between would lose the
     trashed fingerprint. Because recording is idempotent (re-hashing the same file yields the
     same asset), a crash mid-refresh just re-processes survivors on the next run; nothing is lost.
   - **Undeletable file** (locked / permission denied): its fingerprint is already recorded
     (harmless), so leave the file in place and report it — it will be re-processed (a no-op for
     the DB) and retried for deletion next refresh. Never block the whole refresh on one stuck file.
3. Trashed assets legitimately have **zero file instances** afterward (the trash files are gone);
   this is the one case where an asset persists with no instances (see [data model](data-model.md)).

> ⚠️ **Refresh always absorbs and empties — even under `--dry-run`.** This procedure is
> **never a no-op**: any file in a trash root is fingerprinted, its hash recorded to the trashed
> set forever, and the file moved to the Recycle Bin. There is **no dry-run variant of refresh** —
> callers that support `--dry-run` (`cleanup`, `merge`) skip only their *own* destructive step
> (deleting library files / copying), but refresh runs for real first. This is intentional: putting
> a file in a trash folder **is** the act of trashing it, so absorbing + emptying it is expected
> regardless of what the surrounding command does or previews. Do not use a trash folder as
> scratch space — anything left there when `refresh`/`cleanup`/`merge`/`trash refresh` runs is
> consumed. (Recoverable from the Recycle Bin **only if the trash root is on a local volume**; a
> trash root on a NAS/SMB share is emptied **permanently** — see [performance & safety](performance.md). Since refresh has no confirm
> gate, treat a network trash folder as one-way: whatever you drop in is gone once it runs.)

**`scan` never touches trash roots** — indexing a trash folder is only ever done here (see [scan](operation-scan.md)
validation). This keeps the "inbox that gets emptied" semantics from colliding with scan's
"index and keep" semantics.
