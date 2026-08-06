# `roots register` — declare a folder as a root (metadata-only)

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
