"""SQLite schema (§4) — the durable catalog.

The full DDL lives here as one string applied inside a transaction by
:func:`packrat.db.connection.init_db`. Design points carried over verbatim from
the plan:

- **presence = row existence.** ``file_instances`` has no ``present`` flag; a gone
  file has its row deleted (§4, §2).
- **two asset states only** (``active``/``trashed``); a forgotten asset is deleted,
  cascading its dependent rows via ``ON DELETE CASCADE`` (§4).
- **partial-unique indexes** encode the per-root exclusivity invariants (§3):
  one ``pending`` review_run per root; one open ``merge_run`` per dest root.
- ``similarity_edges`` is stored with **canonical ordering** ``asset_a < asset_b``
  and a ``UNIQUE(asset_a, asset_b)`` so an undirected pair has exactly one row (§4).
- ``review_actions`` FKs to assets/instances are **NOT** cascade-linked — a scan of a
  referenced root may forget a now-gone asset mid-review, and the plan keys off the
  stored ``path`` (§4, §3 owned-vs-referenced).
"""

from __future__ import annotations

#: Bumped when the DDL changes. M0 ships v1; v2 adds scan_results +
#: scan_problem_files (new tables only — created via CREATE IF NOT EXISTS, so an
#: existing v1 DB gains them on next init_db with no migration runner needed).
SCHEMA_VERSION = 2

SCHEMA_SQL = """
-- ---------------------------------------------------------------------------
-- roots: registered folder trees (§4). name is globally unique (case-insensitive
-- via NOCASE collation). kind ∈ library|trash.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS roots (
    id                 INTEGER PRIMARY KEY,
    path               TEXT NOT NULL UNIQUE,
    name               TEXT NOT NULL COLLATE NOCASE UNIQUE,
    kind               TEXT NOT NULL CHECK (kind IN ('library', 'trash')),
    enabled            INTEGER NOT NULL DEFAULT 1,
    ignore_globs       TEXT,                 -- JSON array of per-root --ignore patterns
    last_full_scan_at  TEXT
);

-- ---------------------------------------------------------------------------
-- assets: a unique piece of content, identified by content_hash (§4).
-- status ∈ active|trashed (no 'missing'). undecodable is orthogonal to status.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    id            INTEGER PRIMARY KEY,
    content_hash  TEXT NOT NULL UNIQUE,      -- blake3 hex
    media_type    TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
    size          INTEGER,
    width         INTEGER,
    height        INTEGER,
    duration_s    REAL,
    captured_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'trashed')),
    undecodable   INTEGER NOT NULL DEFAULT 0, -- bytes hashed OK but decoder rejected pixels (§4)
    decode_error  TEXT,                       -- last decoder failure detail (debugging POC wheels)
    added_at      TEXT,
    trashed_at    TEXT,
    trash_reason  TEXT
);

-- ---------------------------------------------------------------------------
-- file_instances: a physical file at a path. presence = row existence (§4).
-- UNIQUE(root_id, path): one row per physical file; scan/merge upsert on this key.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS file_instances (
    id            INTEGER PRIMARY KEY,
    asset_id      INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    root_id       INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    path          TEXT NOT NULL,             -- canonical long-path-safe form (§8 A1)
    filename      TEXT,
    size          INTEGER,
    mtime         REAL,
    last_seen_at  TEXT,
    UNIQUE (root_id, path)
);
CREATE INDEX IF NOT EXISTS ix_file_instances_asset ON file_instances(asset_id);
CREATE INDEX IF NOT EXISTS ix_file_instances_root  ON file_instances(root_id);

-- ---------------------------------------------------------------------------
-- phash: one PDQ row per photo asset (§4/§5.3). algo always 'pdq'.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS phash (
    asset_id  INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    algo      TEXT NOT NULL DEFAULT 'pdq',
    bits      BLOB NOT NULL,                 -- 256-bit PDQ
    quality   INTEGER,                       -- 0-100 PDQ quality
    PRIMARY KEY (asset_id, algo)
);

-- ---------------------------------------------------------------------------
-- vphash: one row per sampled video frame (§4/§5.3). Same PDQ as photos.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vphash (
    asset_id     INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    frame_index  INTEGER NOT NULL,
    t_offset_s   REAL,
    pdq_bits     BLOB NOT NULL,              -- 256-bit PDQ of the sampled frame
    quality      INTEGER,                    -- 0-100; below video.min_frame_quality excluded from vote
    PRIMARY KEY (asset_id, frame_index)
);

-- ---------------------------------------------------------------------------
-- embeddings: one CLIP vector per asset, only if scan --embed (§4/§7).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS embeddings (
    asset_id  INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    model     TEXT NOT NULL,
    vector    BLOB NOT NULL,                 -- float32 blob (e.g. 512d)
    PRIMARY KEY (asset_id, model)
);

-- ---------------------------------------------------------------------------
-- similarity_edges: pairwise near-dups; written by dedup, NOT scan (§4/§5.3).
-- CANONICAL ORDERING: asset_a < asset_b, so UNIQUE(asset_a, asset_b) dedups pairs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS similarity_edges (
    asset_a     INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    asset_b     INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    media_type  TEXT NOT NULL,
    distance    INTEGER,                     -- PDQ Hamming (photo) or video match score
    algo        TEXT NOT NULL CHECK (algo IN ('pdq', 'video')),
    created_at  TEXT,
    PRIMARY KEY (asset_a, asset_b),
    CHECK (asset_a < asset_b)
);
CREATE INDEX IF NOT EXISTS ix_sim_b ON similarity_edges(asset_b);

-- ---------------------------------------------------------------------------
-- review_runs: one stateful review lifecycle per target root (§4).
-- partial UNIQUE(root_id) WHERE status='pending' → at most one open run per folder.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_runs (
    id            INTEGER PRIMARY KEY,
    root_id       INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    run_type      TEXT NOT NULL CHECK (run_type IN ('dedup', 'cleanup-perceptual')),
    status        TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'cancelled')),
    created_at    TEXT,
    confirmed_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_runs_pending_root
    ON review_runs(root_id) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- review_actions: the persisted, crash-safe plan for a review_run (§4).
-- path is the AUTHORITATIVE target; asset_id/instance_id/survivor_instance_id
-- are reference-only and MUST tolerate becoming dangling (NOT cascade-linked).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_actions (
    id                       INTEGER PRIMARY KEY,
    run_id                   INTEGER NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    folder                   TEXT NOT NULL,   -- will_be_deleted|grouped|perceptually_identified_trash
    kind                     TEXT,            -- exact|perceptual
    reason                   TEXT,            -- exact-internal|exact-external|perceptual|cleanup-perceptual
    default_action           TEXT,            -- delete|keep
    asset_id                 INTEGER,         -- reference only (may dangle)
    instance_id              INTEGER,         -- reference only (may dangle)
    path                     TEXT NOT NULL,   -- authoritative target
    survivor_instance_id     INTEGER,         -- reference only (may dangle)
    group_no                 INTEGER,
    member_no                INTEGER,
    is_external              INTEGER,
    matched_trashed_asset_id INTEGER,         -- cleanup-perceptual only
    distance                 INTEGER,
    shortcut_name            TEXT
);
CREATE INDEX IF NOT EXISTS ix_review_actions_run ON review_actions(run_id);

-- ---------------------------------------------------------------------------
-- merge_runs: one merge lifecycle; the frozen plan header + cross-op guard (§4).
-- partial UNIQUE(dest_root_id) WHERE status IN ('planning','copying').
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS merge_runs (
    id            INTEGER PRIMARY KEY,
    job_id        INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    source_path   TEXT NOT NULL,
    dest_path     TEXT NOT NULL,
    dest_root_id  INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    status        TEXT NOT NULL CHECK (status IN ('planning','copying','done','cancelled','error')),
    created_at    TEXT,
    finished_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_merge_runs_open_dest
    ON merge_runs(dest_root_id) WHERE status IN ('planning', 'copying');

-- ---------------------------------------------------------------------------
-- merge_plan_items: the persisted, FROZEN per-source-file plan (§4/§8 C).
-- No metadata columns — dims/duration/captured_at are probed JIT in Phase 3.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS merge_plan_items (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES merge_runs(id) ON DELETE CASCADE,
    source_rel_path TEXT NOT NULL,
    size            INTEGER,
    mtime           REAL,
    content_hash    TEXT,
    classification  TEXT,   -- dup-in-source|trashed|exact-known|new
    rep_of_hash     TEXT,   -- dup-in-source only
    dest_path       TEXT,   -- final dest incl. collision rename; NULL until copied
    progress        TEXT NOT NULL DEFAULT 'pending',
                            -- pending|copied|registered|copied-unindexed|skipped|error
    error           TEXT
);
CREATE INDEX IF NOT EXISTS ix_merge_plan_items_run ON merge_plan_items(run_id);

-- ---------------------------------------------------------------------------
-- jobs: the queue + progress-display counter (§4). total/done drive the bar only;
-- resume is from each op's own durable state. status includes 'interrupted'.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY,
    type         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('running','done','error','cancelled','interrupted')),
    total        INTEGER,
    done         INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT,
    finished_at  TEXT,
    error        TEXT,
    params_json  TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);

-- ---------------------------------------------------------------------------
-- scan_results: one row per (scan job, root) — the persisted scan report so a
-- later `status <root>` (and the M6 TUI) can re-render a past scan. One scan of
-- N roots (--all) writes N rows. Keyed to the jobs row; cascades when it's gone.
-- Counters mirror the scan-done banner; profile_json holds the --profile snapshot.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_results (
    job_id            INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    root_id           INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    root_name         TEXT,
    full              INTEGER NOT NULL DEFAULT 0,
    embed             INTEGER NOT NULL DEFAULT 0,
    profiled          INTEGER NOT NULL DEFAULT 0,
    candidates        INTEGER, new INTEGER, exact_dup INTEGER, backfilled INTEGER,
    matches_trashed   INTEGER, skipped_fastpath INTEGER, undecodable INTEGER,
    errors            INTEGER, deleted_instances INTEGER, forgotten_assets INTEGER,
    root_offline      INTEGER NOT NULL DEFAULT 0,
    profile_json      TEXT,     -- ScanProfiler.snapshot_json(), NULL unless --profile
    created_at        TEXT,
    PRIMARY KEY (job_id, root_id)
);
CREATE INDEX IF NOT EXISTS ix_scan_results_root ON scan_results(root_id);

-- ---------------------------------------------------------------------------
-- scan_problem_files: one row per undecodable / unreadable file in a scan, so
-- the exact paths + reasons are retrievable (not just counted). content_hash is
-- NULL for a read-error (bytes never read). Cascades with its jobs/roots rows.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_problem_files (
    id            INTEGER PRIMARY KEY,
    job_id        INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    root_id       INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    path          TEXT NOT NULL,
    media_type    TEXT,
    problem       TEXT NOT NULL,   -- 'undecodable' | 'read-error'
    content_hash  TEXT,            -- NULL for read-error
    detail        TEXT             -- decode_error text or OSError message
);
CREATE INDEX IF NOT EXISTS ix_scan_problem_files_job  ON scan_problem_files(job_id);
CREATE INDEX IF NOT EXISTS ix_scan_problem_files_root ON scan_problem_files(root_id);

-- ---------------------------------------------------------------------------
-- meta: schema version + small daemon-owned key/values.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""
