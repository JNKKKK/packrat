# Goals & non-goals

**Goals**
- Treat the entire collection as **one logical set**, spanning multiple folders ("roots").
- Track assets by **content fingerprint**, never by path. Files may be freely moved/renamed
  in Explorer without the system losing track of them.
- **Merge** workflow: fingerprint a temp folder and copy into a destination folder *only* the
  assets new to the whole collection, deciding by **exact hash** (byte-identical copies collapse;
  trashed content is excluded).
- **Dedup** as a separate, reviewed operation — find **perceptual near-duplicates**
  (re-compressed / resized / re-encoded), for **both photos and video**, and stage them in
  Explorer for the user to resolve. Not automatic and not part of merge.
- **Trash memory**: remember fingerprints of assets I've deliberately trashed, so they are
  excluded from future merges even when re-exported from the iPhone.
- **Semantic embeddings** (opt-in CLIP pass) stored per asset, to enable later capabilities
  such as semantic search and junk-flagging — with any human review done **in Explorer**. The
  embedding is infrastructure only; specific tagging/classification behavior is TBD (see [embeddings](embeddings.md)).
- Run work in a background **daemon** so jobs outlive the terminal that launched them; drive it
  through a **CLI** and an ASCII **TUI** (the `packrat` no-args entrypoint).

**Non-goals (v1)**
- No gallery/viewer UI. Explorer *is* the UI for reviewing files.
- No cloud, no multi-user, no mobile app.
- No editing of asset pixels/metadata (we only copy/move whole files).

**Design tenets**
1. **Fingerprint is identity.** Paths are just where a fingerprint currently lives.
2. **Explorer is the review surface.** The system stages files into folders; the human
   accepts/rejects by keeping/removing files; the CLI resumes.
3. **Never destroy silently.** Merges are copy-only. Deletions go to a trash folder /
   Recycle Bin, and originals are removed only after explicit confirmation. **Caveat (see [performance](performance.md)):**
   the Recycle Bin exists only for local volumes — on NAS/SMB roots (most of the collection,
   see [performance](performance.md)) deletion is **permanent**, so typed confirmation + `--dry-run` are the real safety net
   there, and the confirm prompt warns when the delete set includes network-path files.
4. **Idempotent & resumable.** Any index/merge/tag job can be interrupted and re-run.
5. **Lazy when safe, thorough on schedule.** Skip re-fingerprinting when `path` + exact `size` +
   near-`mtime` (tolerant) are unchanged; do full sweeps on a fixed interval as the backstop.
6. **TUI is the default face; the CLI is the complete surface.** The expected way to drive
   packrat is the **TUI** (`packrat` no-args — the primary, discoverable entrypoint, see [TUI](tui.md)). But the
   TUI is only a *presentation layer*: **every action it offers must also be a first-class CLI
   command** (see [CLI](cli.md)), and the TUI issues no privileged operation of its own — each of its actions maps
   onto an existing CLI verb, so the two stay behaviorally identical (see [TUI](tui.md) "Read-safe"). This is a
   hard rule, not a courtesy: it keeps packrat fully scriptable/automatable and headless-usable
   (SSH, cron, CI), guarantees the TUI can never diverge from or outrun the CLI, and means the CLI
   is the authoritative contract the TUI (and any future client) is built *on top of* — both are
   thin clients over the same daemon API (see [architecture](architecture.md)). Consequence for every new capability: **add the CLI
   verb first (or together), never TUI-only.**

---

# Core concepts

- **Asset** — a unique piece of content, identified by fingerprint. This is the thing we
  "know exists." Has status: `active` or `trashed` — there is no `missing`. When a non-trashed
  asset loses its last file instance, we simply **forget it** (delete the asset and its
  fingerprints), because a plain filesystem delete must not be remembered (see [trash model](trash-model.md)). Only `trashed`
  fingerprints are retained across zero instances.
- **File instance** — a physical file at a path on disk. Many file instances can map to one
  asset (same photo living in two folders). **Presence is row existence**: a `file_instances`
  row exists iff we believe a file lives at that path; discovering it gone deletes the row (no
  `present` flag). This split is what makes "track by fingerprint,
  files move around" work cleanly.
- **Root** — a registered folder tree, each with a globally-unique name (see [scan workflow](workflow-scan.md)). Types:
  - `library` root — folders whose contents belong to the collection (e.g. the iPhone backup
    folder). Indexed by `scan`.
  - `trash` root — a transient **inbox**: files the user drops in are absorbed into the permanent
    trashed-hash set and the folder emptied ("refresh the trash collection", see [trash refresh](trash-model.md)). **`scan` never
    touches trash roots.** Any number of trash roots may exist; they form one logical trashed set.
- **Fingerprint layers** (cheap → expensive):
  1. **Fast-path key**: `path` + exact `size` + tolerant `mtime` (see [scan workflow](workflow-scan.md)) — used only to *skip*
     re-fingerprinting unchanged files, never for identity.
  2. **Content hash**: BLAKE3 of file bytes — exact-duplicate identity.
  3. **Perceptual signature**: robust to recompression/resize.
     - Photo: PDQ (256-bit) — the single photo signal.
     - Video: duration + sequence of per-frame PDQ hashes sampled across the timeline.
  4. **Semantic embedding**: CLIP vector — computed **only** on an opt-in `scan --embed`, for
     future semantic search / junk-flagging (see [embeddings](embeddings.md), TBD). **Never used in any duplicate decision**
     (dedup, merge, or cleanup) — those rely solely on the content hash (exact) and perceptual
     signature (near-dup).
