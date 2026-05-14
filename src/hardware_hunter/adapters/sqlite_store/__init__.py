"""SQLite-backed :class:`Store` adapter — AR8 / AR9 / AR10.

Public surface re-exported here:

  - :class:`SqliteStore` — the concrete :class:`Store` implementation
  - :class:`Phase2AuditWriter` — append-only Phase 2 audit writer
  - :func:`open_connection` — sync connection factory (WAL + sync=NORMAL)
  - :class:`MigrationRunner` — discover + apply tracked migrations

Everything else in this package is implementation detail.
"""

from hardware_hunter.adapters.sqlite_store.audit_writer import Phase2AuditWriter
from hardware_hunter.adapters.sqlite_store.connection import open_connection
from hardware_hunter.adapters.sqlite_store.migrations import (
    MigrationRunner,
    SchemaDriftError,
)
from hardware_hunter.adapters.sqlite_store.store import SqliteStore

__all__ = [
    "MigrationRunner",
    "Phase2AuditWriter",
    "SchemaDriftError",
    "SqliteStore",
    "open_connection",
]
