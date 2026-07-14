"""SQLite catalog: schema + connection helpers."""

from .connection import Database, connect, init_db, schema_version, transaction
from .schema import SCHEMA_SQL, SCHEMA_VERSION

__all__ = [
    "Database",
    "connect",
    "init_db",
    "schema_version",
    "transaction",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
]
