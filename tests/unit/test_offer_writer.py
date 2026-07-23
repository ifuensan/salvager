"""Offer schema migration (0004) + :class:`OfferAuditWriter` tests.

Mirrors ``test_phase2_schema.py`` / ``test_audit_writer.py``: the 0004
migration is additive over 0003 (existing rows untouched), `offers` rows
append with Decimals stored as text, the dedupe/daily-budget reads gate
correctly, and the independent `offer_state` lockout round-trips.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from salvager.adapters.sqlite_store.connection import open_connection
from salvager.adapters.sqlite_store.migrations import MigrationRunner, db_path_under
from salvager.adapters.sqlite_store.offer_writer import OfferAuditWriter
from salvager.domain.errors import OfferFailureReason
from salvager.domain.offer_audit import OfferAttemptRecord

_ENTRY_KEY = ("Corsair", "Vengeance LPX 16GB", "CMK16GX4M2D3000C16")


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    MigrationRunner().run(connection)
    connection.close()
    return db_path


def _attempt(**overrides: object) -> OfferAttemptRecord:
    base: dict[str, object] = {
        "alert_id": uuid4(),
        "listing_id": "internal-123",
        "marketplace": "wallapop",
        "entry_key": _ENTRY_KEY,
        "offered_eur": Decimal("70"),
        "asking_eur": Decimal("88.00"),
        "outcome": "success",
        "platform_remaining": 9,
        "attempted_at": datetime.now(UTC),
    }
    base.update(overrides)
    return OfferAttemptRecord(**base)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────
# Migration 0004
# ─────────────────────────────────────────────────────────────────────────


def test_migration_creates_offer_tables_and_seeds_state(migrated_db: Path) -> None:
    connection = open_connection(migrated_db)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "offers" in tables
        assert "offer_state" in tables
        row = connection.execute(
            "SELECT globally_disabled, consecutive_failures FROM offer_state WHERE id = 1"
        ).fetchone()
        assert (row[0], row[1]) == (0, 0)
    finally:
        connection.close()


def test_migration_0004_is_additive_over_existing_rows(tmp_path: Path) -> None:
    # Apply only 0001-0003, insert a Phase 1 row, then apply 0004 on top.
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    try:
        runner = MigrationRunner()
        for version, name in runner.available_migrations():
            if version <= 3:
                connection.executescript(runner._read_migration(name))
        connection.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', '3')"
        )
        seen_at = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO seen_listings (listing_id, entry_manufacturer, entry_model, "
            "entry_ref, url, first_seen_at, last_seen_at) VALUES ('x', ?, ?, ?, 'u', ?, ?)",
            (*_ENTRY_KEY, seen_at, seen_at),
        )
        connection.commit()

        version = runner.run(connection)
        assert version == 4
        count = connection.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0]
        assert count == 1
    finally:
        connection.close()


# ─────────────────────────────────────────────────────────────────────────
# OfferAuditWriter
# ─────────────────────────────────────────────────────────────────────────


async def test_record_offer_attempt_inserts_row_with_text_decimals(migrated_db: Path) -> None:
    writer = OfferAuditWriter(migrated_db)
    try:
        audit_id = await writer.record_offer_attempt(_attempt())
        assert audit_id > 0
        connection = open_connection(migrated_db)
        try:
            row = connection.execute(
                "SELECT offered_eur, asking_eur, outcome, failure_reason, status, "
                "platform_remaining FROM offers WHERE audit_id = ?",
                (audit_id,),
            ).fetchone()
        finally:
            connection.close()
        assert tuple(row) == ("70", "88.00", "success", None, "sent", 9)
    finally:
        await writer.close()


async def test_failure_attempt_records_reason(migrated_db: Path) -> None:
    writer = OfferAuditWriter(migrated_db)
    try:
        audit_id = await writer.record_offer_attempt(
            _attempt(outcome="failure", failure_reason=OfferFailureReason.timeout)
        )
        connection = open_connection(migrated_db)
        try:
            row = connection.execute(
                "SELECT failure_reason FROM offers WHERE audit_id = ?", (audit_id,)
            ).fetchone()
        finally:
            connection.close()
        assert row[0] == "timeout"
    finally:
        await writer.close()


async def test_has_successful_offer_dedupes_per_listing(migrated_db: Path) -> None:
    writer = OfferAuditWriter(migrated_db)
    try:
        assert await writer.has_successful_offer("wallapop", "internal-123") is False
        # A failed attempt does not count as a sent offer.
        await writer.record_offer_attempt(
            _attempt(outcome="failure", failure_reason=OfferFailureReason.timeout)
        )
        assert await writer.has_successful_offer("wallapop", "internal-123") is False
        await writer.record_offer_attempt(_attempt())
        assert await writer.has_successful_offer("wallapop", "internal-123") is True
        # Listing ids are only unique per marketplace.
        assert await writer.has_successful_offer("ebay", "internal-123") is False
    finally:
        await writer.close()


async def test_count_recent_successes_rolls_off_after_24h(migrated_db: Path) -> None:
    writer = OfferAuditWriter(migrated_db)
    try:
        now = datetime.now(UTC)
        await writer.record_offer_attempt(_attempt(attempted_at=now - timedelta(hours=25)))
        await writer.record_offer_attempt(
            _attempt(listing_id="other", attempted_at=now - timedelta(hours=1))
        )
        # Aborts and failures never consume budget.
        await writer.record_offer_attempt(
            _attempt(
                listing_id="failed",
                outcome="failure",
                failure_reason=OfferFailureReason.timeout,
                attempted_at=now,
            )
        )
        assert await writer.count_recent_successes(now=now) == 1
    finally:
        await writer.close()


async def test_lockout_round_trip_and_enable_resets_counter(migrated_db: Path) -> None:
    writer = OfferAuditWriter(migrated_db)
    try:
        assert (await writer.read_state()).globally_disabled is False
        assert await writer.increment_failure_counter() == 1
        assert await writer.increment_failure_counter() == 2
        await writer.set_global_disable("offer_lockout_threshold")
        state = await writer.read_state()
        assert state.globally_disabled is True
        assert state.disabled_reason == "offer_lockout_threshold"
        assert state.consecutive_failures == 2

        await writer.clear_global_disable(_ENTRY_KEY)
        state = await writer.read_state()
        assert state.globally_disabled is False
        assert state.disabled_reason is None
        # Re-enabling is a fresh start, not threshold-minus-one.
        assert state.consecutive_failures == 0
    finally:
        await writer.close()


async def test_success_resets_failure_counter(migrated_db: Path) -> None:
    writer = OfferAuditWriter(migrated_db)
    try:
        await writer.increment_failure_counter()
        await writer.reset_failure_counter()
        assert (await writer.read_state()).consecutive_failures == 0
    finally:
        await writer.close()


async def test_state_survives_a_fresh_writer_instance(migrated_db: Path) -> None:
    writer = OfferAuditWriter(migrated_db)
    await writer.increment_failure_counter()
    await writer.close()

    reopened = OfferAuditWriter(migrated_db)
    try:
        assert (await reopened.read_state()).consecutive_failures == 1
    finally:
        await reopened.close()
