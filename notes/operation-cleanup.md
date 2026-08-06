# `packrat cleanup <folder>` — cull trashed / undecodable files from a library folder

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
dedup, merge, **and scan** (via [scan](operation-scan.md) A2 step 1a) queue-and-wait on it until confirm/cancel. Recommend a
fresh `scan <folder>` first so newly-arrived files are indexed (submit it *before* cleanup — once the
pending run opens, a scan just waits behind it); cleanup operates on indexed instances.

## Exact mode — `packrat cleanup <folder> --trash-exact`
1. **Refresh the trash collection** (see [refresh the trash collection](operation-trash-refresh.md)), so the trashed set is fully current.
2. In `<folder>`, find every `file_instances` row whose asset has `status='trashed'`, matched by
   **exact content hash only**.
3. **Print the count** and require typed confirmation — a sanity check, **no staging folder**. If
   `<folder>` is on a network/SMB root, warn that deletion is **permanent** (no Recycle Bin — see [performance & safety](performance.md)).
4. On confirm, move each matched file to the **Recycle Bin** (permanent on NAS/SMB — see [performance & safety](performance.md)) and
   delete its `file_instances` row. The asset stays `trashed` (fingerprints retained). Report
   deleted count.

## Undecodable mode — `packrat cleanup <folder> --undecodable`
1. **No trash refresh** — this mode is independent of the trashed set.
2. In `<folder>`, find every `file_instances` row whose asset is `undecodable=1` **and** `active`
   (see [tech stack](tech-stack.md)). (An already-`trashed` undecodable is left for the exact mode.)
3. **Print the count** and require typed confirmation (network-path permanent-delete warning as above).
4. On confirm, move each file to the **Recycle Bin**, delete its `file_instances` row, and — if the
   asset now has zero instances — **mark the asset `trashed`** (`trash_reason='cleanup-undecodable'`,
   fingerprints = the hash retained), so a re-import of the same corrupt bytes is excluded from a
   future merge. Report deleted count.

## Perceptual mode — `packrat cleanup <folder> --trash-perceptual` (analyze → `--confirm`)
Analyze:
1. **Refresh the trash collection** (see [refresh the trash collection](operation-trash-refresh.md)); open a persisted `pending` cleanup run for this root.
2. **Exact matches:** find library instances whose asset is `trashed` (exact hash), as in default
   mode — but **do not delete yet**; record them in the plan.
3. **Perceptual matches:** run the [fingerprints](fingerprints.md) matcher for `<folder>`'s active-asset instances against the
   **trashed** set (photo PDQ ≤ `t_photo_edit` / video per-frame ≤ `T_match_video` + frame vote;
   duration pre-filter). Cleanup uses the single wider photo cutoff — **no recompress/edit banding**
   (that stage split is dedup's review ergonomics — see [dedup](operation-dedup.md); here every trash match is one folder). Each
   library file matching a trashed asset per [fingerprints](fingerprints.md) is a perceptual-trash candidate.
4. **Stage for review** at `<root>\_packrat_review\_perceptually_identified_trash\`: one `.lnk`
   per perceptual candidate (stat-before-create, so no broken `.lnk`; [dedup](operation-dedup.md) Phase 4 rules), plus a
   `manifest.csv` (shortcut → target path → matched trashed asset → distance → `quality` →
   `low_confidence`, same photo-quality confidence hint as dedup — see [fingerprints](fingerprints.md)). Write a `proposed.json`
   audit record ([dedup](operation-dedup.md) style).
5. **Report** the exact-match count (will delete on confirm) and perceptual-candidate count
   (staged for review), print the `--confirm` / `--cancel` commands, and **pause**.

Review convention (**delete-default**, like dedup's `_exact_dup_to_delete\` — *opposite* of dedup's
perceptual keep-default stages): a staged file is treated as trash and **will be deleted**; **remove
its shortcut to spare** the file (mark it "not trash" for this run). Renames count as removal (strict,
per [dedup](operation-dedup.md)).

`packrat cleanup <folder> --confirm`:
6. Re-verify liveness per file (lazy stat, as [dedup](operation-dedup.md) Phase 6). Unlike the
   one-shot exact/undecodable modes, perceptual `--confirm` has **no typed count-confirmation** — the
   Explorer shortcut review *is* the confirmation (same as dedup `--confirm`). Then, to the **Recycle
   Bin** (permanent on NAS/SMB — see [performance & safety](performance.md); the completion log tallies
   how many deletions were on a network path):
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
deliberate exception to "dry-run changes nothing": refresh (see [refresh the trash collection](operation-trash-refresh.md)) is a shared, idempotent procedure
whose no-op variant isn't worth building, and it is non-destructive to your *library* (it only
absorbs hashes and empties the transient trash inbox — which is what trashing already means).
**`--undecodable --dry-run` changes nothing at all** — that mode never refreshes. Dry-run's
guarantee is scoped precisely: **it never deletes from the library folder being cleaned**; the trash
modes may still empty the trash inboxes.
