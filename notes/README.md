# packrat design notes

The authoritative design spec, organized so each operation has its own file. This is the
description of *intended* behavior — consult it before changing how something works, and keep it in
sync when behavior changes.

## Foundations

- [goals-and-concepts.md](goals-and-concepts.md) — goals, non-goals, design tenets; the core
  vocabulary (asset, file instance, root, the fingerprint layers).
- [architecture.md](architecture.md) — the daemon + two thin clients, the single-worker job queue,
  auto-spawn, startup reconciliation, the two concurrency guarantees, the resource-oriented HTTP API.
- [data-model.md](data-model.md) — the full SQLite schema (every table + its column comments).
- [operations-overview.md](operations-overview.md) — how the operations divide the work: the
  three core workflows, the division of labor, and the exact-vs-near-dup identity rules they share.

## How duplicates are decided

- [fingerprints.md](fingerprints.md) — the three fingerprints (BLAKE3, PDQ, CLIP), the two kinds of
  duplicate, and the perceptual matching engine (photo + video rules, thresholds, parameters).
- [embeddings.md](embeddings.md) — the opt-in CLIP embedding infrastructure (tagging behavior is TBD).
- [trash-model.md](trash-model.md) — the trash model concept: the two ways content leaves the
  collection, multiple trash roots, and the permanent trashed-hash memory.

## Operations (one file per command)

- [operation-register.md](operation-register.md) — `roots register`: declare a folder as a root
  (metadata-only).
- [operation-scan.md](operation-scan.md) — `scan`: the resumable indexing pipeline (walk,
  fingerprint, deletion-detect).
- [operation-probe.md](operation-probe.md) — `probe`: cheap discovery of new/changed files, no
  fingerprinting.
- [operation-dedup.md](operation-dedup.md) — `dedup`: the 3-stage review sequence, keep-lead
  ranking, and the review-run audit trail.
- [operation-merge.md](operation-merge.md) — `merge`: exact-hash "copy what's new", copy-only
  ingest, resumable plan.
- [operation-cleanup.md](operation-cleanup.md) — `cleanup`: cull trashed / undecodable files from a
  library folder (exact / perceptual / undecodable modes).
- [operation-trash-refresh.md](operation-trash-refresh.md) — `trash refresh`: absorb trash-folder
  files into the trashed set and empty the folders.
- [operation-untrash.md](operation-untrash.md) — `untrash`: forget content from trash memory.

## Interfaces

- [cli.md](cli.md) — the complete CLI command reference.
- [tui.md](tui.md) — the Textual TUI: interfaces, render architecture, the 4-state status dot, and
  NSFW masking.

## Environment & planning

- [tech-stack.md](tech-stack.md) — the tech stack, format-coverage matrix ("decode is the gate"),
  and the `config.toml` reference.
- [performance.md](performance.md) — performance & safety, and the SMB/NAS tuning rules.
- [roadmap.md](roadmap.md) — build milestones (M0–M8) and open questions / risks.
