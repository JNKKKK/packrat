# Trash model

Two distinct ways content leaves the collection — treated very differently:

1. **Deleted directly in Explorer** (not via a trash folder): next scan deletes the gone
   `file_instances` row; if no instances remain, the (active) asset is **forgotten entirely** —
   the asset and all its fingerprints are deleted. It is **not** blocklisted — if it reappears in
   a future export it will be treated as new. This matches "a plain Explorer delete does not mean
   trash," and is exactly why we keep no `missing` state: a forgotten asset leaves no trace to
   compare against.

2. **Trashed by the user via a trash folder** — the primary way to trash content: the user
   manually moves or copies the file into a **registered trash folder** (a root with
   `kind='trash'`). A registered trash folder is a transient **inbox**: the user drops junk in,
   and *refreshing the trash collection* (below) absorbs it into the permanent trashed-hash memory
   and empties the folder. Trashed fingerprints are kept **indefinitely**, so future merges exclude
   anything matching them — this is what stops junk that still lives on the iPhone from being
   re-merged even after you emptied the trash folder. (Not *irreversibly*: an accidental trash can
   be undone with **`packrat untrash`** — untrash (below) — which forgets a fingerprint from trash memory.)

   (Content can also become `trashed` via **dedup** — when the user discards a perceptual
   near-duplicate during a dedup run, that asset is marked `trashed` with the same
   fingerprints-kept-forever semantics; see [dedup](workflow-dedup.md). The trash-folder route above is the general,
   explicit path.)

**Multiple trash roots are allowed.** Any number of roots may be `kind='trash'` (e.g. one per
drive). They are all consulted together as one logical trashed set.

## Refresh the trash collection (shared procedure)

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

**`scan` never touches trash roots** — indexing a trash folder is only ever done here (see [scan](workflow-scan.md)
validation). This keeps the "inbox that gets emptied" semantics from colliding with scan's
"index and keep" semantics.

## `packrat cleanup <folder>` — cull trashed / undecodable files from a library folder

From the user's perspective: **delete junk from `<folder>`.** Use case: a photo you trashed still
lives on the iPhone and got re-pasted into a library backup folder; `cleanup` removes those
re-appearances — and, separately, culls files that won't decode at all.

**`cleanup` requires exactly one mode** (there is no bare default — the command errors without one):
- **`--trash-exact`:** one-shot. Byte-identical matches to trashed content are deleted after a typed
  count confirmation — no per-file review (exact-hash matching is false-positive-free).
- **`--trash-perceptual`:** stateful (analyze → pause → `--confirm`). Adds *perceptual* trash matches
  (recompressed/resized copies of trashed content), staged as shortcuts for Explorer review since
  perceptual matching can misfire. Exact matches are **not** deleted inline in this mode — both
  exact and reviewed-perceptual deletions apply together at `--confirm`.
- **`--undecodable`:** one-shot. Deletes the folder's `undecodable=1` files (see [tech stack](tech-stack.md) — bytes hashed but
  the decoder rejected the pixels, so they carry no perceptual signature and can never dedup) after a
  typed count confirmation, **and marks each deleted asset `trashed`** (`trash_reason=
  'cleanup-undecodable'`) so a re-import of the same corrupt bytes is excluded from a future merge.
  Unlike the trash modes it does **not** refresh the trash collection — it targets the folder's own
  undecodables, independent of the trashed set. (`status <root>` lists exactly this set as the
  undecodable problem files — see [CLI](cli.md).)

**Shared validation & lock (all modes):** `<folder>` must be a registered **library** root —
reject a `kind='trash'` root (its files are consumed by refresh, not cleaned). Then the
**per-root exclusivity invariant** (see [architecture](architecture.md)) applies: if this root already has an active operation — a
`pending` dedup run, a `pending` perceptual-cleanup run, or an in-flight merge (an open
`merge_runs` row with `dest_root_id` = this root — see [data model](data-model.md)) — the cleanup job is **held in the queue
(dequeue gate — see [architecture](architecture.md)) until that holder clears**, rather than running against it, since a live plan may
stage `.lnk`s pointing at files cleanup would delete (broken shortcuts / a stale plan); conversely,
once a `--trash-perceptual` cleanup opens its own `pending` `review_runs` row it *owns* the root, so
dedup, merge, **and scan** (via [scan](workflow-scan.md) A2 step 1a) queue-and-wait on it until confirm/cancel. Recommend a
fresh `scan <folder>` first so newly-arrived files are indexed (submit it *before* cleanup — once the
pending run opens, a scan just waits behind it); cleanup operates on indexed instances.

### Exact mode — `packrat cleanup <folder> --trash-exact`
1. **Refresh the trash collection** (the refresh procedure above), so the trashed set is fully current.
2. In `<folder>`, find every `file_instances` row whose asset has `status='trashed'`, matched by
   **exact content hash only**.
3. **Print the count** and require typed confirmation — a sanity check, **no staging folder**. If
   `<folder>` is on a network/SMB root, warn that deletion is **permanent** (no Recycle Bin — see [performance & safety](performance.md)).
4. On confirm, move each matched file to the **Recycle Bin** (permanent on NAS/SMB — see [performance & safety](performance.md)) and
   delete its `file_instances` row. The asset stays `trashed` (fingerprints retained). Report
   deleted count.

### Undecodable mode — `packrat cleanup <folder> --undecodable`
1. **No trash refresh** — this mode is independent of the trashed set.
2. In `<folder>`, find every `file_instances` row whose asset is `undecodable=1` **and** `active`
   (see [tech stack](tech-stack.md)). (An already-`trashed` undecodable is left for the exact mode.)
3. **Print the count** and require typed confirmation (network-path permanent-delete warning as above).
4. On confirm, move each file to the **Recycle Bin**, delete its `file_instances` row, and — if the
   asset now has zero instances — **mark the asset `trashed`** (`trash_reason='cleanup-undecodable'`,
   fingerprints = the hash retained), so a re-import of the same corrupt bytes is excluded from a
   future merge. Report deleted count.

### Perceptual mode — `packrat cleanup <folder> --trash-perceptual` (analyze → `--confirm`)
Analyze:
1. **Refresh the trash collection** (the refresh procedure above); open a persisted `pending` cleanup run for this root.
2. **Exact matches:** find library instances whose asset is `trashed` (exact hash), as in default
   mode — but **do not delete yet**; record them in the plan.
3. **Perceptual matches:** run the [fingerprints](fingerprints.md) matcher for `<folder>`'s active-asset instances against the
   **trashed** set (photo PDQ ≤ `t_photo_edit` / video per-frame ≤ `T_match_video` + frame vote;
   duration pre-filter). Cleanup uses the single wider photo cutoff — **no recompress/edit banding**
   (that stage split is dedup's review ergonomics — see [dedup](workflow-dedup.md); here every trash match is one folder). Each
   library file matching a trashed asset per [fingerprints](fingerprints.md) is a perceptual-trash candidate.
4. **Stage for review** at `<root>\_packrat_review\_perceptually_identified_trash\`: one `.lnk`
   per perceptual candidate (stat-before-create, so no broken `.lnk`; [dedup](workflow-dedup.md) Phase 4 rules), plus a
   `manifest.csv` (shortcut → target path → matched trashed asset → distance → `quality` →
   `low_confidence`, same photo-quality confidence hint as dedup — see [fingerprints](fingerprints.md)). Write a `proposed.json`
   audit record ([dedup](workflow-dedup.md) style).
5. **Report** the exact-match count (will delete on confirm) and perceptual-candidate count
   (staged for review), print the `--confirm` / `--cancel` commands, and **pause**.

Review convention (**delete-default**, like dedup's `_exact_dup_to_delete\` — *opposite* of dedup's
perceptual keep-default stages): a staged file is treated as trash and **will be deleted**; **remove
its shortcut to spare** the file (mark it "not trash" for this run). Renames count as removal (strict,
per [dedup](workflow-dedup.md)).

`packrat cleanup <folder> --confirm`:
6. Re-verify liveness per file (lazy stat, as [dedup](workflow-dedup.md) Phase 6). Require typed confirmation of the
   combined delete set; if `<folder>` is on a network/SMB root, warn that deletion is **permanent**
   (no Recycle Bin — see [performance & safety](performance.md)). Then, to the **Recycle Bin** (permanent on NAS/SMB — see [performance & safety](performance.md)):
   - **Exact matches** → delete the `file_instances` row; asset stays `trashed`.
   - **Perceptual matches still staged** (shortcut present) → delete the file **and mark its own
     asset `status='trashed'`**, `trash_reason='cleanup-perceptual'`, fingerprints retained — so
     this near-dup won't re-appear via merge (consistent with dedup's perceptual-deletion).
   - **Perceptual matches spared** (shortcut removed) → left untouched; not trashed.
7. Delete the `_perceptually_identified_trash\` staging folder, write `applied.json`, mark the run
   `completed`. `--cancel` discards staging and deletes nothing.

**`--dry-run`** reports the count/list of library files that *would* be deleted (and, with
`--trash-perceptual`, would be staged) without deleting or staging anything. For the two **trash
modes** it **still refreshes-and-empties the trash collection** (the refresh runs for real) — a
deliberate exception to "dry-run changes nothing": refresh (the refresh procedure above) is a shared, idempotent procedure
whose no-op variant isn't worth building, and it is non-destructive to your *library* (it only
absorbs hashes and empties the transient trash inbox — which is what trashing already means).
**`--undecodable --dry-run` changes nothing at all** — that mode never refreshes. Dry-run's
guarantee is scoped precisely: **it never deletes from the library folder being cleaned**; the trash
modes may still empty the trash inboxes.

## `packrat untrash <path>` — forget content from trash memory

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
   as scan** (see [scan](workflow-scan.md)) so non-media is skipped. Error if the path doesn't exist / isn't readable.
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
     ever reappears in a future merge/scan (exactly the plain-Explorer-delete "forget" model, case 1 above). This is the case that resolves the gap: the *hash* stops excluding re-imports.
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
