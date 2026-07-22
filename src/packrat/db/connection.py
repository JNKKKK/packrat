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
    # `journal_mode`/`synchronous` are WRITE pragmas — setting them on a read-only
    # handle raises `OperationalError` if the DB isn't already in WAL mode (a restored/
    # copied DB left in delete-journal mode, an externally-created one). The writer
    # connection WAL-izes the file once (persistent), so a read-only handle just needs
    # the connection-scoped pragmas below; skip the file-level write pragmas.
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
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
        self._txn_depth = 0   # >0 while inside a transaction() (suppresses auto-commit)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def raw(self) -> sqlite3.Connection:
        """The underlying connection. Hold :attr:`lock` while using it."""
        return self._conn

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute one statement. Auto-commits — EXCEPT when called inside a
        ``transaction()`` block, where the commit is deferred to the block (so a
        nested ``db.execute`` can't prematurely commit and break the unit's atomicity).
        """
        with self._lock:
            cur = self._conn.execute(sql, params)
            if self._txn_depth == 0:
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
        """Atomic unit of work, serialized against all other writers.

        Re-entrancy-safe: a nested ``transaction()`` (or a ``db.execute`` called within
        one) joins the outer transaction rather than starting/committing its own — the
        whole outermost block commits once on success, rolls back once on any exception.
        SQLite has no nested BEGIN, so only the OUTERMOST enter issues BEGIN/COMMIT."""
        with self._lock:
            outermost = self._txn_depth == 0
            if outermost:
                self._conn.execute("BEGIN")
            self._txn_depth += 1
            try:
                yield self._conn
            except Exception:
                if outermost:
                    self._conn.rollback()
                raise
            else:
                if outermost:
                    self._conn.commit()
            finally:
                self._txn_depth -= 1

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

    def backup_labeled(self, label: str) -> str:
        """Back the DB up to ``backups/<label>-<timestamp>.db`` (§10). Returns the path.

        The one place the pre-destructive-apply backup filename is built, so dedup /
        cleanup / merge don't each re-derive the timestamp-suffixing.
        """
        from .. import paths
        from ..util import now_iso

        ts = now_iso().replace(":", "").replace("-", "")
        dest = paths.backups_dir() / f"{label}-{ts}.db"
        self.backup_to(dest)
        return str(dest)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def init_db(db_file: Path | None = None) -> sqlite3.Connection:
    """Create the schema if missing and return an open connection (§4).

    ``SCHEMA_SQL`` is the single source of truth — every statement is
    ``CREATE … IF NOT EXISTS``, so this is idempotent: a no-op on an already-current
    DB, and a full build on a fresh one. There is no migration runner (§4); the
    ``schema_version`` marker is stamped into ``meta`` for future use.
    """
    conn = connect(db_file)
    conn.executescript(SCHEMA_SQL)
    with transaction(conn):
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
