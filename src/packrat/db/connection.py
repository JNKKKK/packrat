"""SQLite connection management (§4, §10): WAL mode, FK enforcement, init.

The daemon owns a single writer connection (§3 concurrency). WAL lets read-only
queries (``status``, ``roots``, TUI stats) run concurrently with the writer.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .. import paths
from .schema import SCHEMA_SQL, SCHEMA_VERSION


def connect(
    db_file: Path | None = None,
    *,
    read_only: bool = False,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open a tuned SQLite connection.

    WAL mode + ``foreign_keys=ON`` (the schema relies on ``ON DELETE CASCADE``,
    which SQLite only enforces when this pragma is set). ``row_factory`` yields
    ``sqlite3.Row`` for name-based access.

    ``check_same_thread=False`` is used for the daemon's shared write connection,
    which is accessed from both the API thread (submit) and the worker thread —
    all writes are serialized by :class:`Database`'s lock, so it is safe.
    """
    p = db_file or paths.db_path()
    if read_only:
        # URI mode so we can open read-only even while the writer holds WAL.
        uri = f"file:{p.as_posix()}?mode=ro"
        conn = sqlite3.connect(
            uri, uri=True, timeout=30.0, check_same_thread=check_same_thread
        )
    else:
        conn = sqlite3.connect(p, timeout=30.0, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class Database:
    """The daemon's single write connection, guarded by a lock (§3 single writer).

    Both the API thread (creating a ``jobs`` row on submit) and the worker thread
    (progress + op writes) go through this. Read-only snapshot queries
    (``status``/``roots``) should instead open their own short-lived read-only
    connection via :func:`connect` so they never contend with the writer.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def raw(self) -> sqlite3.Connection:
        """The underlying connection. Hold :attr:`lock` while using it."""
        return self._conn

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    @contextmanager
    def transaction(self):
        """Atomic unit of work, serialized against all other writers."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def clear_catalog(self) -> dict[str, int]:
        """DELETE every catalog row, preserving the schema (dev-only — §clear-db).

        Empties all data tables (assets/file_instances/phash/…/jobs/roots) inside a
        single transaction under the write lock, so it is safe against the API +
        worker threads. **Preserves** ``meta`` (the ``schema_version`` row) and the
        table structure — this resets *content*, not the schema, so the daemon's
        open connection keeps working with no re-init. ``sqlite_sequence`` is reset
        so ids restart at 1. Returns ``{table: rows_deleted}`` (only tables that
        had rows).

        Counts are taken for **all** tables up front, *before* any DELETE — with
        FKs on (the connection default), ``DELETE FROM assets`` cascade-deletes
        ``file_instances``/``phash``/… so a count-then-delete-per-table loop would
        misattribute the cascaded rows to zero. We don't disable FKs: ``PRAGMA
        foreign_keys`` is a no-op inside a transaction anyway, and cascades only
        help here since every referencing table is being cleared too.

        Not called anywhere in normal operation; exposed only via the dev-gated
        ``/dev/clear-db`` endpoint (:func:`packrat.build.is_dev_build`).
        """
        with self._lock:
            tables = [
                r["name"]
                for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' AND name != 'meta'"
                ).fetchall()
            ]
            counts = {
                t: self._conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                for t in tables
            }
            self._conn.execute("BEGIN")
            try:
                for t in tables:
                    self._conn.execute(f"DELETE FROM {t}")
                # Reset AUTOINCREMENT rowids if the bookkeeping table exists.
                if self._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
                ).fetchone():
                    self._conn.execute("DELETE FROM sqlite_sequence")
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()
        return {t: n for t, n in counts.items() if n}

    def backup_to(self, dest: Path | str) -> None:
        """Online-copy the whole DB to ``dest`` via SQLite's backup API (§10).

        Taken before every destructive apply (dedup/cleanup ``--confirm``, merge
        copy) as the backstop. Uses the live backup API (WAL-safe — a plain file
        copy would miss the WAL), holding the write lock so nothing mutates
        mid-copy. Overwrites ``dest`` if present.
        """
        import sqlite3

        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self._conn.backup(target)
            finally:
                target.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


#: Columns added to EXISTING tables after v1 — CREATE IF NOT EXISTS can't alter a
#: table, and there is no migration runner, so init_db adds any missing one via an
#: idempotent ADD COLUMN pass (§4 / schema v3). New *tables* need no entry here
#: (CREATE IF NOT EXISTS handles them). Keep in sync with schema.py.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # (table, column, column-def) — v3: the M3 3-stage dedup cursor + per-action stage.
    ("review_runs", "stage", "INTEGER NOT NULL DEFAULT 1"),
    ("review_runs", "stage_phase", "TEXT"),
    ("review_actions", "stage", "INTEGER"),
    # v5: video codec for the video keep-lead's codec-efficiency weight (§8 B).
    ("assets", "codec", "TEXT"),
    # NOTE: v4's assets.detail_score is intentionally ABSENT — retired in v6 (§8 B).
    # A DB created at v4/v5 keeps the column as harmless dead data (no DROP migration);
    # a fresh v6 DB never creates it. Nothing reads or writes it. Do NOT re-add it here.
    # v7: durable job queue + result history (§3/§4/§12). These three are plain
    # additive columns; the sibling 'queued' status value needs the CHECK widened,
    # which _migrate_jobs_v7 handles by rebuilding the table (runs BEFORE this pass).
    ("jobs", "root_id", "INTEGER REFERENCES roots(id) ON DELETE SET NULL"),
    ("jobs", "enqueued_at", "TEXT"),
    ("jobs", "result_json", "TEXT"),
    # v8: `jobs prioritize <id>` — higher priority is dequeued first (§3/§11).
    ("jobs", "priority", "INTEGER NOT NULL DEFAULT 0"),
)


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """Add any post-v1 column missing from an existing table (idempotent).

    ``CREATE TABLE IF NOT EXISTS`` leaves an already-created table untouched, so a
    DB from an earlier schema version keeps the old table shape. We reconcile by
    checking ``PRAGMA table_info`` and ``ALTER TABLE … ADD COLUMN`` for each
    declared addition. A fresh DB already has them from ``SCHEMA_SQL`` → all no-ops.
    """
    for table, column, coldef in _ADDED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")


def _migrate_jobs_v7(conn: sqlite3.Connection) -> None:
    """One-time REBUILD of the ``jobs`` table for v7 (§4 durable queue).

    v7 adds a ``'queued'`` value to ``jobs.status``'s CHECK constraint. SQLite can't
    widen a CHECK with ``ALTER``, so we rebuild the table following SQLite's
    documented recipe (foreign_keys OFF, create-copy-drop-rename in one txn). The
    three additive v7 columns (``root_id``/``enqueued_at``/``result_json``) are
    created here too, so ``_migrate_columns`` then finds them present (no-op).

    **Must run BEFORE ``executescript``** — the v7 ``SCHEMA_SQL`` creates
    ``ix_jobs_root ON jobs(root_id)``, which errors on an old table lacking that
    column. Idempotent: skipped when ``jobs`` doesn't exist yet (fresh DB — the
    following ``executescript`` creates it v7-shaped) or already has ``'queued'``.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if row is None or "'queued'" in (row["sql"] or ""):
        return  # no jobs table yet (fresh DB), or already the v7 shape

    existing = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    # Guard the copy against a partially-migrated DB (columns added but never rebuilt).
    root_sel = "root_id" if "root_id" in existing else "NULL"
    enq_sel = "enqueued_at" if "enqueued_at" in existing else "started_at"
    res_sel = "result_json" if "result_json" in existing else "NULL"

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    try:
        conn.execute(
            "CREATE TABLE jobs_v7_new ("
            "  id INTEGER PRIMARY KEY,"
            "  type TEXT NOT NULL,"
            "  root_id INTEGER REFERENCES roots(id) ON DELETE SET NULL,"
            "  status TEXT NOT NULL CHECK (status IN "
            "    ('queued','running','done','error','cancelled','interrupted')),"
            "  total INTEGER,"
            "  done INTEGER NOT NULL DEFAULT 0,"
            "  enqueued_at TEXT,"
            "  started_at TEXT,"
            "  finished_at TEXT,"
            "  error TEXT,"
            "  result_json TEXT,"
            "  params_json TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO jobs_v7_new "
            "(id, type, root_id, status, total, done, enqueued_at, started_at, "
            " finished_at, error, result_json, params_json) "
            f"SELECT id, type, {root_sel}, status, total, done, {enq_sel}, started_at, "
            f"       finished_at, error, {res_sel}, params_json FROM jobs"
        )
        conn.execute("DROP TABLE jobs")
        conn.execute("ALTER TABLE jobs_v7_new RENAME TO jobs")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_jobs_root ON jobs(root_id)")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys=ON")
        raise
    conn.execute("PRAGMA foreign_keys=ON")


def init_db(db_file: Path | None = None) -> sqlite3.Connection:
    """Create the schema if missing and return an open connection (§4).

    Idempotent — every DDL statement is ``IF NOT EXISTS``, plus a one-time v7
    ``jobs`` rebuild (:func:`_migrate_jobs_v7`, run first — see its docstring) and an
    ADD-COLUMN pass for columns added to existing tables after v1
    (:func:`_migrate_columns`). Records/updates the ``schema_version`` in ``meta``.
    """
    conn = connect(db_file)
    # v7 jobs rebuild runs BEFORE executescript (the v7 DDL indexes jobs.root_id,
    # absent on an old table) and manages its own txn + FK toggle.
    _migrate_jobs_v7(conn)
    conn.executescript(SCHEMA_SQL)
    with transaction(conn):
        _migrate_columns(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Transaction context: commit on success, rollback on exception.

    Uses an explicit ``BEGIN`` so a whole unit of work is atomic (important for
    the "single transaction" writes the plan calls for — §8 A2 step 9, §8 C step 11).
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def schema_version(conn: sqlite3.Connection) -> int | None:
    """Return the recorded schema version, or None if unset."""
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else None
