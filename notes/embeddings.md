# Semantic embeddings (infrastructure; tagging TBD)

This section covers **only the embedding infrastructure** — computing and storing a semantic
vector per asset. The concrete tagging/classification behavior built on top (junk detection,
the double-check-trash flow, categories, thresholds) is **not yet designed** and is deferred;
the `tags` table is intentionally omitted from the schema (see [data-model](data-model.md)) until then.

> **CLIP lives here and only here.** The embedding is a *semantic* signal (for future search /
> tagging), never a dedup signal (see [fingerprints](fingerprints.md)). Dedup is decided entirely by content hash + perceptual
> signature.

- **Engine**: CLIP (open_clip, ViT-L/14 on the RTX) — the *intended* production model. Produces a
  fixed-length float32 vector per photo; for video, per sampled frame (aggregated). **Status: not yet
  built** — the embedding pass is deferred, so today `scan --embed` writes nothing (the only CLIP code
  in the tree is the decode smoke test, which loads a lighter ViT-B-32 just to prove the wheel imports).
- **When computed**: only on an explicit **`scan --embed`** (or a future tagging pass) — never by
  a plain scan. Fully decoupled from dedup/merge: skipping it or having it fail changes no
  dedup/merge result. Backfillable at any time (once the pass is implemented).
- **Storage**: one `embeddings(asset_id, model, vector)` row per asset (see [data-model](data-model.md)). Search over them
  starts as brute-force cosine on a memory-mapped numpy matrix (see [data-model](data-model.md) Notes).
- **What it unlocks later (design TBD)**: semantic search ("find beach photos"); zero-shot
  junk-flagging (screenshots, receipts, documents) with an Explorer-based human review; possible
  OCR corroboration. None of this is specified yet — only the vectors are.
- Embeddings are **not** used for near-dup confirmation — semantic similarity ≠ duplicate-ness.
