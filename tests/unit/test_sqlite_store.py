"""Tests for the SQLite store adapter — Story 3.3 (AR8/AR9/AR10)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from hardware_hunter.adapters.sqlite_store import (
    MigrationRunner,
    SchemaDriftError,
    SqliteStore,
    open_connection,
)
from hardware_hunter.adapters.sqlite_store.migrations import db_path_under
from hardware_hunter.domain.alert import AlertSnapshot
from hardware_hunter.domain.audit import (
    CallbackAudit,
    Phase2GuardrailTripped,
    TapEventAudit,
    TransactionAudit,
)
from hardware_hunter.domain.evaluation import ListingEvaluation
from hardware_hunter.domain.listing import Listing

# ─────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────


def _make_listing(listing_id: str = "lst-001", **overrides: object) -> Listing:
    base: dict[str, object] = {
        "listing_id": listing_id,
        "marketplace": "wallapop",
        "url": f"https://wallapop.com/item/{listing_id}",
        "title": "WD Red Plus 4TB",
        "description": "Used, like new",
        "price_eur": Decimal("55.00"),
        "fetched_at": datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return Listing(**base)  # type: ignore[arg-type]


def _make_evaluation(**overrides: object) -> ListingEvaluation:
    base: dict[str, object] = {
        "listing_id": "lst-001",
        "entry_key": ("WD", "Red Plus 4TB", "WD40EFPX"),
        "confidence": "high",
        "one_line_take": "Strong match at €55.",
        "is_container": False,
        "evaluated_at": datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return ListingEvaluation(**base)  # type: ignore[arg-type]


def _make_snapshot(**overrides: object) -> AlertSnapshot:
    base: dict[str, object] = {
        "alert_id": uuid4(),
        "entry_key": ("WD", "Red Plus 4TB", "WD40EFPX"),
        "entry_display_name": "WD Red Plus 4TB (WD40EFPX)",
        "listing": _make_listing(),
        "evaluation": _make_evaluation(),
        "phase": "phase1",
        "rendered_at": datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return AlertSnapshot(**base)  # type: ignore[arg-type]


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    """A SQLite DB with all Phase 1 migrations applied."""
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    MigrationRunner().run(connection)
    connection.close()
    return db_path


# ─────────────────────────────────────────────────────────────────────────
# Connection contract
# ─────────────────────────────────────────────────────────────────────────


def test_connection_enables_wal_mode(tmp_path: Path) -> None:
    db_path = tmp_path / "hardware_hunter.db"
    connection = open_connection(db_path)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        connection.close()


def test_connection_sets_synchronous_normal(tmp_path: Path) -> None:
    db_path = tmp_path / "hardware_hunter.db"
    connection = open_connection(db_path)
    try:
        # synchronous=NORMAL maps to integer 1
        result = connection.execute("PRAGMA synchronous").fetchone()[0]
        assert result == 1
    finally:
        connection.close()


def test_db_path_under_returns_canonical_path(tmp_path: Path) -> None:
    assert db_path_under(tmp_path) == tmp_path / "hardware_hunter.db"


# ─────────────────────────────────────────────────────────────────────────
# Migration runner
# ─────────────────────────────────────────────────────────────────────────


def test_migration_runner_lists_available_migrations() -> None:
    available = MigrationRunner().available_migrations()
    versions = [v for (v, _) in available]
    assert 1 in versions
    assert versions == sorted(versions)


def test_migration_runner_starts_at_version_zero(tmp_path: Path) -> None:
    connection = open_connection(tmp_path / "hardware_hunter.db")
    try:
        assert MigrationRunner().current_version(connection) == 0
    finally:
        connection.close()


def test_migration_runner_applies_pending_migrations(tmp_path: Path) -> None:
    connection = open_connection(tmp_path / "hardware_hunter.db")
    try:
        version = MigrationRunner().run(connection)
        assert version == 2
        assert MigrationRunner().current_version(connection) == 2
    finally:
        connection.close()


def test_migration_runner_is_idempotent(tmp_path: Path) -> None:
    connection = open_connection(tmp_path / "hardware_hunter.db")
    try:
        runner = MigrationRunner()
        first = runner.run(connection)
        second = runner.run(connection)
        assert first == second  # no-op on the second run

    finally:
        connection.close()


def test_migration_runner_creates_all_phase1_tables(migrated_db: Path) -> None:
    connection = open_connection(migrated_db)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {row[0] for row in rows}
    finally:
        connection.close()
    assert {
        "_meta",
        "wishlist_runtime_state",
        "seen_listings",
        "alert_snapshots",
        "callbacks",
    } <= names


def test_migration_runner_creates_audit_indexes(migrated_db: Path) -> None:
    connection = open_connection(migrated_db)
    try:
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "idx_alert_snapshots_entry" in indexes
    assert "idx_alert_snapshots_rendered_at" in indexes
    assert "idx_callbacks_alert_id" in indexes
    assert "idx_seen_listings_entry" in indexes


def test_migration_runner_detects_schema_drift(tmp_path: Path) -> None:
    """If the DB is ahead of the binary, raise instead of silently
    re-using a future schema."""
    db_path = tmp_path / "hardware_hunter.db"
    connection = open_connection(db_path)
    try:
        MigrationRunner().run(connection)
        # Pretend we're on a schema 99 versions ahead.
        connection.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', '99')"
        )
        with pytest.raises(SchemaDriftError):
            MigrationRunner().run(connection)
    finally:
        connection.close()


def test_migration_runner_persists_schema_version(migrated_db: Path) -> None:
    connection = open_connection(migrated_db)
    try:
        row = connection.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
        assert int(row[0]) == 2
    finally:
        connection.close()


# ─────────────────────────────────────────────────────────────────────────
# SqliteStore — dedup + snooze + alert + audit
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_seen_false_for_unrecorded_listing(migrated_db: Path) -> None:
    store = SqliteStore(migrated_db)
    try:
        seen = await store.is_seen("lst-001", ("WD", "Red Plus 4TB", "WD40EFPX"))
        assert seen is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_record_seen_then_is_seen(migrated_db: Path) -> None:
    store = SqliteStore(migrated_db)
    try:
        listing = _make_listing()
        key = ("WD", "Red Plus 4TB", "WD40EFPX")
        await store.record_seen(listing, key)
        assert await store.is_seen(listing.listing_id, key) is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_record_seen_is_idempotent(migrated_db: Path) -> None:
    store = SqliteStore(migrated_db)
    try:
        listing = _make_listing()
        key = ("WD", "Red Plus 4TB", "WD40EFPX")
        await store.record_seen(listing, key)
        await store.record_seen(listing, key)
        assert await store.is_seen(listing.listing_id, key) is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_snooze_round_trip(migrated_db: Path) -> None:
    store = SqliteStore(migrated_db)
    try:
        key = ("WD", "Red Plus 4TB", "WD40EFPX")
        assert await store.get_snooze_until(key) is None
        until = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
        await store.set_snooze(key, until)
        loaded = await store.get_snooze_until(key)
        assert loaded == until
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_record_alert_snapshot_returns_audit_id(migrated_db: Path) -> None:
    store = SqliteStore(migrated_db)
    try:
        audit_id = await store.record_alert_snapshot(_make_snapshot())
        assert audit_id >= 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_alert_snapshot_round_trip(migrated_db: Path) -> None:
    store = SqliteStore(migrated_db)
    try:
        snapshot = _make_snapshot()
        audit_id = await store.record_alert_snapshot(snapshot)
        loaded = await store.get_alert_snapshot(audit_id)
        assert loaded is not None
        assert loaded.alert_id == snapshot.alert_id
        assert loaded.entry_display_name == snapshot.entry_display_name
        assert loaded.listing.listing_id == snapshot.listing.listing_id
        assert loaded.evaluation.confidence == snapshot.evaluation.confidence
        assert loaded.phase == "phase1"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_get_alert_snapshot_missing_returns_none(migrated_db: Path) -> None:
    store = SqliteStore(migrated_db)
    try:
        assert await store.get_alert_snapshot(999) is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_get_alert_snapshot_by_alert_id_round_trip(migrated_db: Path) -> None:
    store = SqliteStore(migrated_db)
    try:
        snapshot = _make_snapshot()
        await store.record_alert_snapshot(snapshot)
        loaded = await store.get_alert_snapshot_by_alert_id(snapshot.alert_id)
        assert loaded is not None
        assert loaded.alert_id == snapshot.alert_id
        assert loaded.entry_key == snapshot.entry_key
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_get_alert_snapshot_by_alert_id_missing_returns_none(
    migrated_db: Path,
) -> None:
    store = SqliteStore(migrated_db)
    try:
        assert await store.get_alert_snapshot_by_alert_id(uuid4()) is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_record_callback_writes_audit_row(migrated_db: Path) -> None:
    store = SqliteStore(migrated_db)
    try:
        callback = CallbackAudit(
            audit_id=uuid4(),
            alert_id=uuid4(),
            telegram_message_id=42,
            callback_data="phase1:view:abc",
            verb="view",
            chat_id=12345,
            occurred_at=datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
        )
        await store.record_callback(callback)
        # Verify the row landed via a direct SQL read.
        connection = open_connection(migrated_db)
        try:
            row = connection.execute(
                "SELECT verb, chat_id, telegram_message_id FROM callbacks"
            ).fetchone()
            assert row["verb"] == "view"
            assert row["chat_id"] == 12345
            assert row["telegram_message_id"] == 42
        finally:
            connection.close()
    finally:
        await store.close()


# ─────────────────────────────────────────────────────────────────────────
# Phase 2 guardrails (AR24)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_tap_event_raises_phase2_guardrail(migrated_db: Path) -> None:
    store = SqliteStore(migrated_db)
    try:
        # Constructing the audit object already trips the guardrail; we
        # bypass it for this test by hand-rolling a sentinel.
        sentinel = object()
        with pytest.raises(Phase2GuardrailTripped):
            await store.record_tap_event(sentinel)  # type: ignore[arg-type]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_record_transaction_raises_phase2_guardrail(migrated_db: Path) -> None:
    store = SqliteStore(migrated_db)
    try:
        sentinel = object()
        with pytest.raises(Phase2GuardrailTripped):
            await store.record_transaction(sentinel)  # type: ignore[arg-type]
    finally:
        await store.close()


def test_phase2_domain_constructors_also_guard() -> None:
    """Defence-in-depth: the domain ctors trip the guardrail too, so a
    caller can't sneak past the store by building the audit object
    first."""
    with pytest.raises(Phase2GuardrailTripped):
        TapEventAudit(
            audit_id=uuid4(),
            alert_id=uuid4(),
            occurred_at=datetime.now(UTC),
        )
    with pytest.raises(Phase2GuardrailTripped):
        TransactionAudit(
            audit_id=uuid4(),
            alert_id=uuid4(),
            price_paid_eur=Decimal("55.00"),
            succeeded=True,
            occurred_at=datetime.now(UTC),
        )


# ─────────────────────────────────────────────────────────────────────────
# NFR-S4 — no UPDATE/DELETE triggers on audit tables
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("audit_table", ["alert_snapshots", "callbacks"])
def test_audit_tables_have_no_update_or_delete_triggers(
    migrated_db: Path, audit_table: str
) -> None:
    """NFR-S4: enforcement is the application-level Store ABC, so the
    DB has no triggers that would re-enable mutation. The check here
    catches anyone who later tries to 'help' by adding them."""
    connection = open_connection(migrated_db)
    try:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name = ?",
            (audit_table,),
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        sql_upper = (row[1] or "").upper()
        assert "UPDATE" not in sql_upper and "DELETE" not in sql_upper, (
            f"trigger {row[0]!r} on {audit_table} would mutate audit data"
        )
