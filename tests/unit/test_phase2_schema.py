"""Phase 2 schema migration — Story 5.1 (migration 0002).

Asserts the 0002 migration creates the four Phase 2 tables on top of
the Phase 1 base, advances ``_meta.schema_version`` to 2, seeds the
single ``phase2_state`` row, and is idempotent.
"""

from __future__ import annotations

from pathlib import Path

from hardware_hunter.adapters.sqlite_store import MigrationRunner, open_connection
from hardware_hunter.adapters.sqlite_store.migrations import db_path_under

_PHASE2_TABLES = ("tap_events", "transactions", "phase2_smoke_tests", "phase2_state")


def _table_names(db_path: Path) -> set[str]:
    connection = open_connection(db_path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {row[0] for row in rows}
    finally:
        connection.close()


def test_migration_creates_phase2_tables_and_advances_version(tmp_path: Path) -> None:
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    try:
        version = MigrationRunner().run(connection)
    finally:
        connection.close()

    assert version == 2
    tables = _table_names(db_path)
    for table in _PHASE2_TABLES:
        assert table in tables, f"migration 0002 must create {table!r}"
    # Phase 1 tables are still present — 0002 is additive.
    assert "alert_snapshots" in tables
    assert "seen_listings" in tables


def test_phase2_state_is_seeded_with_a_single_default_row(tmp_path: Path) -> None:
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    try:
        MigrationRunner().run(connection)
    finally:
        connection.close()

    connection = open_connection(db_path)
    try:
        rows = connection.execute("SELECT * FROM phase2_state").fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == 1
        assert row["globally_disabled"] == 0
        assert row["disabled_at"] is None
        assert row["disabled_reason"] is None
        assert row["consecutive_failures"] == 0
        assert row["last_smoke_result"] is None
        assert row["last_smoke_at"] is None
    finally:
        connection.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    db_path = db_path_under(tmp_path)

    for _ in range(3):
        connection = open_connection(db_path)
        try:
            version = MigrationRunner().run(connection)
        finally:
            connection.close()
        assert version == 2

    # Re-running never duplicates the seeded single-row state.
    connection = open_connection(db_path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM phase2_state").fetchone()[0]
        assert count == 1
    finally:
        connection.close()


def test_phase2_indexes_exist(tmp_path: Path) -> None:
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    try:
        MigrationRunner().run(connection)
        index_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        connection.close()

    for expected in (
        "idx_tap_events_alert_id",
        "idx_tap_events_tapped_at",
        "idx_transactions_alert_id",
        "idx_transactions_committed_at",
        "idx_phase2_smoke_tests_run_at",
    ):
        assert expected in index_names, f"missing index {expected!r}"
