"""Behavioural tests for :class:`Phase2AuditWriter` — Story 5.1.

The append-only / no-mutation-method contract is enforced separately in
``test_audit_writer_append_only.py``; this module checks that each
method actually writes the row (or updates the state) it promises.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from hardware_hunter.adapters.sqlite_store import (
    MigrationRunner,
    Phase2AuditWriter,
    open_connection,
)
from hardware_hunter.adapters.sqlite_store.migrations import db_path_under
from hardware_hunter.domain.phase2_audit import (
    SmokeTestRecord,
    TapEventRecord,
    TransactionRecord,
)

_T0 = datetime(2026, 5, 14, 6, 0, 0, tzinfo=UTC)


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    try:
        MigrationRunner().run(connection)
    finally:
        connection.close()
    return db_path


def _row(db_path: Path, sql: str) -> dict[str, object] | None:
    connection = open_connection(db_path)
    try:
        cursor = connection.execute(sql)
        row = cursor.fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


# ─────────────────────────────────────────────────────────────────────────
# Append-only audit rows
# ─────────────────────────────────────────────────────────────────────────


async def test_record_tap_event_inserts_a_row(migrated_db: Path) -> None:
    writer = Phase2AuditWriter(migrated_db)
    alert_id = uuid4()
    try:
        audit_id = await writer.record_tap_event(
            TapEventRecord(
                alert_id=alert_id,
                verb="buy",
                raw_payload={"data": "listing:buy:abc", "from": 99},
                tapped_at=_T0,
                ip_or_chat_id="chat-99",
            )
        )
    finally:
        await writer.close()

    assert audit_id == 1
    row = _row(migrated_db, "SELECT * FROM tap_events WHERE audit_id = 1")
    assert row is not None
    assert row["alert_id"] == str(alert_id)
    assert row["verb"] == "buy"
    assert row["ip_or_chat_id"] == "chat-99"
    assert json.loads(str(row["raw_payload"])) == {"data": "listing:buy:abc", "from": 99}
    assert row["tapped_at"] == _T0.isoformat()


async def test_record_transaction_stores_decimal_as_text(migrated_db: Path) -> None:
    writer = Phase2AuditWriter(migrated_db)
    alert_id = uuid4()
    try:
        audit_id = await writer.record_transaction(
            TransactionRecord(
                alert_id=alert_id,
                price_paid_eur=Decimal("55.00"),
                payment_method="wallapop_pay",
                receipt_id="WP-2026-0001",
                screenshot_path="/app/data/screenshots/WP-2026-0001.png",
                total_seconds=42,
                committed_at=_T0,
            )
        )
    finally:
        await writer.close()

    assert audit_id == 1
    row = _row(migrated_db, "SELECT * FROM transactions WHERE audit_id = 1")
    assert row is not None
    assert row["price_paid_eur"] == "55.00"  # exact Decimal text, no float
    assert row["payment_method"] == "wallapop_pay"
    assert row["receipt_id"] == "WP-2026-0001"
    assert row["total_seconds"] == 42


async def test_record_smoke_test_inserts_row_and_mirrors_state(migrated_db: Path) -> None:
    writer = Phase2AuditWriter(migrated_db)
    try:
        await writer.record_smoke_test(
            SmokeTestRecord(
                run_at=_T0,
                result="fail",
                parsed_price=Decimal("55.00"),
                independent_price=Decimal("70.00"),
                delta_eur=Decimal("15.00"),
                delta_pct=Decimal("21.43"),
            )
        )
    finally:
        await writer.close()

    row = _row(migrated_db, "SELECT * FROM phase2_smoke_tests WHERE audit_id = 1")
    assert row is not None
    assert row["result"] == "fail"
    assert row["delta_eur"] == "15.00"

    # The freshest smoke result is mirrored onto phase2_state for the
    # pre-flight gate to read.
    state = _row(migrated_db, "SELECT * FROM phase2_state WHERE id = 1")
    assert state is not None
    assert state["last_smoke_result"] == "fail"
    assert state["last_smoke_at"] == _T0.isoformat()


# ─────────────────────────────────────────────────────────────────────────
# phase2_state — lockout + circuit-breaker counter
# ─────────────────────────────────────────────────────────────────────────


async def test_set_and_clear_global_disable(migrated_db: Path) -> None:
    writer = Phase2AuditWriter(migrated_db)
    try:
        await writer.set_global_disable("circuit_breaker_open")
        disabled = _row(migrated_db, "SELECT * FROM phase2_state WHERE id = 1")
        assert disabled is not None
        assert disabled["globally_disabled"] == 1
        assert disabled["disabled_reason"] == "circuit_breaker_open"
        assert disabled["disabled_at"] is not None

        await writer.clear_global_disable(("Western Digital", "WD Red Plus 4TB", "WD40EFPX"))
        cleared = _row(migrated_db, "SELECT * FROM phase2_state WHERE id = 1")
        assert cleared is not None
        assert cleared["globally_disabled"] == 0
        assert cleared["disabled_reason"] is None
        assert cleared["disabled_at"] is None
    finally:
        await writer.close()


async def test_failure_counter_increments_and_resets(migrated_db: Path) -> None:
    writer = Phase2AuditWriter(migrated_db)
    try:
        assert await writer.increment_failure_counter() == 1
        assert await writer.increment_failure_counter() == 2
        assert await writer.increment_failure_counter() == 3

        await writer.reset_failure_counter()
        state = _row(migrated_db, "SELECT consecutive_failures FROM phase2_state WHERE id = 1")
        assert state is not None
        assert state["consecutive_failures"] == 0
    finally:
        await writer.close()


async def test_state_survives_a_fresh_writer_instance(migrated_db: Path) -> None:
    """AR13: the lockout is durable — a new writer (daemon restart) reads
    the persisted state, it is not in-memory."""
    writer = Phase2AuditWriter(migrated_db)
    try:
        await writer.increment_failure_counter()
        await writer.increment_failure_counter()
        await writer.set_global_disable("smoke_test_failed")
    finally:
        await writer.close()

    reopened = Phase2AuditWriter(migrated_db)
    try:
        bumped = await reopened.increment_failure_counter()
        assert bumped == 3  # 2 from before the "restart" + 1 now
    finally:
        await reopened.close()

    state = _row(migrated_db, "SELECT * FROM phase2_state WHERE id = 1")
    assert state is not None
    assert state["globally_disabled"] == 1
    assert state["disabled_reason"] == "smoke_test_failed"
