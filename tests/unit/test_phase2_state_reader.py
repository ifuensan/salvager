"""Tests for :class:`SqlitePhase2StateReader` — Story 5.2."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from hardware_hunter.adapters.sqlite_store import (
    MigrationRunner,
    Phase2AuditWriter,
    open_connection,
)
from hardware_hunter.adapters.sqlite_store.migrations import db_path_under
from hardware_hunter.adapters.sqlite_store.phase2_state_reader import (
    SqlitePhase2StateReader,
)


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    try:
        MigrationRunner().run(connection)
    finally:
        connection.close()
    return db_path


async def test_reads_seeded_default_row(migrated_db: Path) -> None:
    reader = SqlitePhase2StateReader(migrated_db)
    try:
        state = await reader.read()
    finally:
        await reader.close()

    assert state.globally_disabled is False
    assert state.consecutive_failures == 0
    assert state.disabled_at is None
    assert state.disabled_reason is None
    assert state.last_smoke_result is None
    assert state.last_smoke_at is None


async def test_reads_writer_state_changes(migrated_db: Path) -> None:
    writer = Phase2AuditWriter(migrated_db)
    try:
        await writer.set_global_disable("smoke_test_failed")
        await writer.increment_failure_counter()
        await writer.increment_failure_counter()
    finally:
        await writer.close()

    reader = SqlitePhase2StateReader(migrated_db)
    try:
        state = await reader.read()
    finally:
        await reader.close()

    assert state.globally_disabled is True
    assert state.disabled_reason == "smoke_test_failed"
    assert state.disabled_at is not None
    assert isinstance(state.disabled_at, datetime)
    assert state.disabled_at.tzinfo == UTC
    assert state.consecutive_failures == 2
