"""Concrete :class:`Store` implementation backed by SQLite.

Threading model
---------------
SQLite's C library is single-threaded per connection. The async API
that :class:`Store` declares wraps every DB call in ``asyncio.to_thread``,
which dispatches to the default executor (a pool). A per-instance
``asyncio.Lock`` serializes writes so the pool never sees two coroutines
holding the same connection at once. Reads are short and synchronous;
the lock takes microseconds.

Phase 2 guardrail
-----------------
``record_tap_event`` and ``record_transaction`` raise
:class:`Phase2GuardrailTripped` immediately — the domain audit
constructors trip the same exception, so any caller that gets to one
of these methods has already broken the v0.x contract somewhere
upstream.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from hardware_hunter.adapters.sqlite_store.connection import open_connection
from hardware_hunter.domain.alert import AlertSnapshot
from hardware_hunter.domain.audit import (
    CallbackAudit,
    Phase2GuardrailTripped,
    TapEventAudit,
    TransactionAudit,
)
from hardware_hunter.domain.evaluation import ListingEvaluation
from hardware_hunter.domain.listing import Listing
from hardware_hunter.interfaces.store import EntryKey, Store


class SqliteStore(Store):
    """SQLite-backed :class:`Store` for Phase 1 persistence."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._connection = open_connection(self._db_path)
        self._write_lock = asyncio.Lock()

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the underlying SQLite connection (called at daemon shutdown)."""
        async with self._write_lock:
            await asyncio.to_thread(self._connection.close)

    # ─────────────────────────────────────────────────────────────────
    # Dedup state
    # ─────────────────────────────────────────────────────────────────

    async def is_seen(self, listing_id: str, entry_key: EntryKey) -> bool:
        def _read() -> bool:
            cursor = self._connection.execute(
                """
                SELECT 1 FROM seen_listings
                WHERE listing_id = ?
                  AND entry_manufacturer = ?
                  AND entry_model = ?
                  AND entry_ref = ?
                """,
                (listing_id, *entry_key),
            )
            return cursor.fetchone() is not None

        return await asyncio.to_thread(_read)

    async def record_seen(self, listing: Listing, entry_key: EntryKey) -> None:
        now = datetime.now(UTC).isoformat()

        def _write() -> None:
            self._connection.execute(
                """
                INSERT INTO seen_listings (
                    listing_id, entry_manufacturer, entry_model, entry_ref,
                    url, first_seen_at, last_seen_at, match_fired
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT (listing_id, entry_manufacturer, entry_model, entry_ref)
                DO UPDATE SET last_seen_at = excluded.last_seen_at
                """,
                (listing.listing_id, *entry_key, listing.url, now, now),
            )

        async with self._write_lock:
            await asyncio.to_thread(_write)

    # ─────────────────────────────────────────────────────────────────
    # Snooze state
    # ─────────────────────────────────────────────────────────────────

    async def get_snooze_until(self, entry_key: EntryKey) -> datetime | None:
        def _read() -> datetime | None:
            cursor = self._connection.execute(
                """
                SELECT snooze_until FROM wishlist_runtime_state
                WHERE entry_manufacturer = ? AND entry_model = ? AND entry_ref = ?
                """,
                entry_key,
            )
            row = cursor.fetchone()
            if row is None or row[0] is None:
                return None
            return datetime.fromisoformat(row[0].replace("Z", "+00:00"))

        return await asyncio.to_thread(_read)

    async def set_snooze(self, entry_key: EntryKey, until: datetime) -> None:
        iso = until.isoformat()

        def _write() -> None:
            self._connection.execute(
                """
                INSERT INTO wishlist_runtime_state (
                    entry_manufacturer, entry_model, entry_ref, snooze_until
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT (entry_manufacturer, entry_model, entry_ref)
                DO UPDATE SET snooze_until = excluded.snooze_until
                """,
                (*entry_key, iso),
            )

        async with self._write_lock:
            await asyncio.to_thread(_write)

    # ─────────────────────────────────────────────────────────────────
    # Alert snapshot + audit (NFR-S4 append-only)
    # ─────────────────────────────────────────────────────────────────

    async def record_alert_snapshot(self, snapshot: AlertSnapshot) -> int:
        listing_json = snapshot.listing.model_dump_json()
        evaluation_json = snapshot.evaluation.model_dump_json()
        phase2_price = (
            str(snapshot.phase2_max_price_eur)
            if snapshot.phase2_max_price_eur is not None
            else None
        )

        def _write() -> int:
            cursor = self._connection.execute(
                """
                INSERT INTO alert_snapshots (
                    alert_id, entry_manufacturer, entry_model, entry_ref,
                    entry_display_name, listing_json, evaluation_json,
                    phase, phase2_max_price_eur, rendered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(snapshot.alert_id),
                    *snapshot.entry_key,
                    snapshot.entry_display_name,
                    listing_json,
                    evaluation_json,
                    snapshot.phase,
                    phase2_price,
                    snapshot.rendered_at.isoformat(),
                ),
            )
            return int(cursor.lastrowid or 0)

        async with self._write_lock:
            return await asyncio.to_thread(_write)

    async def get_alert_snapshot(self, audit_id: int) -> AlertSnapshot | None:
        def _read() -> AlertSnapshot | None:
            cursor = self._connection.execute(
                """
                SELECT alert_id, entry_manufacturer, entry_model, entry_ref,
                       entry_display_name, listing_json, evaluation_json,
                       phase, phase2_max_price_eur, rendered_at
                FROM alert_snapshots
                WHERE audit_id = ?
                """,
                (audit_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return _row_to_alert_snapshot(row)

        return await asyncio.to_thread(_read)

    async def get_alert_snapshot_by_alert_id(self, alert_id: UUID) -> AlertSnapshot | None:
        def _read() -> AlertSnapshot | None:
            cursor = self._connection.execute(
                """
                SELECT alert_id, entry_manufacturer, entry_model, entry_ref,
                       entry_display_name, listing_json, evaluation_json,
                       phase, phase2_max_price_eur, rendered_at
                FROM alert_snapshots
                WHERE alert_id = ?
                """,
                (str(alert_id),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return _row_to_alert_snapshot(row)

        return await asyncio.to_thread(_read)

    async def record_callback(self, callback: CallbackAudit) -> None:
        def _write() -> None:
            self._connection.execute(
                """
                INSERT INTO callbacks (
                    alert_id, telegram_message_id, chat_id,
                    callback_data, verb, received_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(callback.alert_id),
                    callback.telegram_message_id,
                    callback.chat_id,
                    callback.callback_data,
                    callback.verb,
                    callback.occurred_at.isoformat(),
                ),
            )

        async with self._write_lock:
            await asyncio.to_thread(_write)

    # ─────────────────────────────────────────────────────────────────
    # _meta key-value store
    # ─────────────────────────────────────────────────────────────────

    async def set_meta(self, key: str, value: str) -> None:
        def _write() -> None:
            self._connection.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                (key, value),
            )

        async with self._write_lock:
            await asyncio.to_thread(_write)

    async def get_meta(self, key: str) -> str | None:
        def _read() -> str | None:
            cursor = self._connection.execute(
                "SELECT value FROM _meta WHERE key = ?",
                (key,),
            )
            row = cursor.fetchone()
            return None if row is None else str(row["value"])

        return await asyncio.to_thread(_read)

    async def get_all_meta(self) -> dict[str, str]:
        def _read() -> dict[str, str]:
            cursor = self._connection.execute("SELECT key, value FROM _meta")
            return {str(row["key"]): str(row["value"]) for row in cursor.fetchall()}

        return await asyncio.to_thread(_read)

    # ─────────────────────────────────────────────────────────────────
    # Phase 2 stubs — guardrail-tripped per AR24
    # ─────────────────────────────────────────────────────────────────

    async def record_tap_event(self, tap: TapEventAudit) -> None:
        _ = tap  # Phase 2 stub — argument signature kept for ABC contract
        raise Phase2GuardrailTripped(
            "SqliteStore.record_tap_event called at v0.x — Phase 2 is not enabled"
        )

    async def record_transaction(self, transaction: TransactionAudit) -> None:
        _ = transaction
        raise Phase2GuardrailTripped(
            "SqliteStore.record_transaction called at v0.x — Phase 2 is not enabled"
        )


def _row_to_alert_snapshot(row: sqlite3.Row) -> AlertSnapshot:
    """Re-hydrate an :class:`AlertSnapshot` from a raw alert_snapshots row."""
    return AlertSnapshot(
        alert_id=row["alert_id"],
        entry_key=(row["entry_manufacturer"], row["entry_model"], row["entry_ref"]),
        entry_display_name=row["entry_display_name"],
        listing=Listing.model_validate(json.loads(row["listing_json"])),
        evaluation=ListingEvaluation.model_validate(json.loads(row["evaluation_json"])),
        phase=row["phase"],
        phase2_max_price_eur=row["phase2_max_price_eur"],
        rendered_at=datetime.fromisoformat(row["rendered_at"]),
    )
