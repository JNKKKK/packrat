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
   fingerprints-kept-forever semantics; see [dedup](operation-dedup.md). The trash-folder route above is the general,
   explicit path.)

**Multiple trash roots are allowed.** Any number of roots may be `kind='trash'` (e.g. one per
drive). They are all consulted together as one logical trashed set.

## Operations

Three operations act on this model: [refresh the trash collection](operation-trash-refresh.md) (the
shared step that absorbs trash folders into permanent memory), [cleanup](operation-cleanup.md) (cull
trashed / undecodable files from a library folder), and [untrash](operation-untrash.md) (forget
content from trash memory).
