# packrat design notes

The authoritative design spec, one file per concern. This is the description of *intended*
behavior — consult it before changing how something works, and keep it in sync when behavior changes.

## Foundations

- [goals-and-concepts.md](goals-and-concepts.md) — goals, non-goals, design tenets; the core
  vocabulary (asset, file instance, root, the fingerprint layers).
- [architecture.md](architecture.md) — the daemon + two thin clients, the single-worker job queue,
  auto-spawn, startup reconciliation, the two concurrency guarantees, the resource-oriented HTTP API.
- [data-model.md](data-model.md) — the full SQLite schema (every table + its column comments).

## How duplicates are decided

- [fingerprints.md](fingerprints.md) — the three fingerprints (BLAKE3, PDQ, CLIP), the two kinds of
  duplicate, and the perceptual matching engine (photo + video rules, thresholds, parameters).
- [embeddings.md](embeddings.md) — the opt-in CLIP embedding infrastructure (tagging behavior is TBD).

## Operations

- [workflow-scan.md](workflow-scan.md) — adding a folder: `roots register`, the `scan` indexing
  pipeline, and `probe` (cheap discovery).
- [workflow-dedup.md](workflow-dedup.md) — `dedup`: the 3-stage review sequence, keep-lead ranking,
  and the review-run audit trail.
- [workflow-merge.md](workflow-merge.md) — `merge`: exact-hash "copy what's new", copy-only ingest,
  resumable plan.
- [trash-model.md](trash-model.md) — the trash model: refresh the trash collection, `cleanup`
  (exact / perceptual / undecodable), and `untrash`.

## Interfaces & operations

- [cli.md](cli.md) — the complete CLI command reference.
- [tui.md](tui.md) — the Textual TUI: interfaces, render architecture, the 4-state status dot, and
  NSFW masking.

## Environment & planning

- [tech-stack.md](tech-stack.md) — the tech stack, format-coverage matrix ("decode is the gate"),
  and the `config.toml` reference.
- [performance.md](performance.md) — performance & safety, and the SMB/NAS tuning rules.
- [roadmap.md](roadmap.md) — build milestones (M0–M8) and open questions / risks.
