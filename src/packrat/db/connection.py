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
    # v4: photo detail estimate for dedup stage-2 keep-lead (§8 B).
    ("assets", "detail_score", "INTEGER"),
    # v5: video codec for the video keep-lead's codec-efficiency weight (§8 B).
    ("assets", "codec", "TEXT"),
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


def init_db(db_file: Path | None = None) -> sqlite3.Connection:
    """Create the schema if missing and return an open connection (§4).

    Idempotent — every DDL statement is ``IF NOT EXISTS``, plus an ADD-COLUMN pass
    for columns added to existing tables after v1 (:func:`_migrate_columns`).
    Records/updates the ``schema_version`` in ``meta``.
    """
    conn = connect(db_file)
    with transaction(conn):
        conn.executescript(SCHEMA_SQL)
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
