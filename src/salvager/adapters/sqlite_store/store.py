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
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from salvager.adapters.sqlite_store.connection import open_connection
from salvager.domain.alert import AlertSnapshot
from salvager.domain.alert_watch import AlertUpdate, AlertWatch
from salvager.domain.audit import (
    CallbackAudit,
    Phase2GuardrailTripped,
    TapEventAudit,
    TransactionAudit,
)
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.interfaces.store import EntryKey, Store


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

    async def record_seen(
        self,
        listing: Listing,
        entry_key: EntryKey,
        *,
        match_fired: bool = False,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        fired = 1 if match_fired else 0

        def _write() -> None:
            # On conflict we bump last_seen_at and only ever ratchet
            # match_fired up (0 → 1) — a later dropped sighting must not
            # erase the record that this pairing once alerted.
            self._connection.execute(
                """
                INSERT INTO seen_listings (
                    listing_id, entry_manufacturer, entry_model, entry_ref,
                    url, first_seen_at, last_seen_at, match_fired
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (listing_id, entry_manufacturer, entry_model, entry_ref)
                DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    match_fired = MAX(seen_listings.match_fired, excluded.match_fired)
                """,
                (listing.listing_id, *entry_key, listing.url, now, now, fired),
            )

        async with self._write_lock:
            await asyncio.to_thread(_write)

    # ─────────────────────────────────────────────────────────────────
    # Alert watches — mutable state (edit-alerts-on-state-change)
    # ─────────────────────────────────────────────────────────────────

    async def create_watch(self, watch: AlertWatch) -> None:
        def _write() -> None:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO alert_watches (
                    alert_id, listing_id, marketplace, entry_manufacturer,
                    entry_model, entry_ref, telegram_message_id, last_price_eur,
                    last_is_reserved, watch_until, last_edited_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(watch.alert_id),
                    watch.listing_id,
                    watch.marketplace,
                    *watch.entry_key,
                    watch.telegram_message_id,
                    str(watch.last_price_eur),
                    1 if watch.last_is_reserved else 0,
                    watch.watch_until.isoformat(),
                    watch.last_edited_at.isoformat() if watch.last_edited_at else None,
                ),
            )

        async with self._write_lock:
            await asyncio.to_thread(_write)

    async def active_watches(
        self, entry_key: EntryKey, *, marketplace: str, now: datetime
    ) -> list[AlertWatch]:
        iso_now = now.isoformat()

        def _read() -> list[AlertWatch]:
            cursor = self._connection.execute(
                """
                SELECT alert_id, listing_id, marketplace, entry_manufacturer,
                       entry_model, entry_ref, telegram_message_id, last_price_eur,
                       last_is_reserved, watch_until, last_edited_at
                FROM alert_watches
                WHERE entry_manufacturer = ? AND entry_model = ? AND entry_ref = ?
                  AND marketplace = ?
                  AND watch_until > ?
                """,
                (*entry_key, marketplace, iso_now),
            )
            return [_row_to_alert_watch(row) for row in cursor.fetchall()]

        return await asyncio.to_thread(_read)

    async def advance_watch(
        self,
        alert_id: UUID,
        *,
        price_eur: Decimal,
        is_reserved: bool,
        edited_at: datetime | None = None,
    ) -> None:
        def _write() -> None:
            if edited_at is not None:
                self._connection.execute(
                    """
                    UPDATE alert_watches
                    SET last_price_eur = ?, last_is_reserved = ?, last_edited_at = ?
                    WHERE alert_id = ?
                    """,
                    (str(price_eur), 1 if is_reserved else 0, edited_at.isoformat(), str(alert_id)),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE alert_watches
                    SET last_price_eur = ?, last_is_reserved = ?
                    WHERE alert_id = ?
                    """,
                    (str(price_eur), 1 if is_reserved else 0, str(alert_id)),
                )

        async with self._write_lock:
            await asyncio.to_thread(_write)

    async def close_watch(self, alert_id: UUID) -> None:
        def _write() -> None:
            self._connection.execute(
                "DELETE FROM alert_watches WHERE alert_id = ?",
                (str(alert_id),),
            )

        async with self._write_lock:
            await asyncio.to_thread(_write)

    async def prune_expired_watches(self, *, now: datetime) -> int:
        iso_now = now.isoformat()

        def _write() -> int:
            cursor = self._connection.execute(
                "DELETE FROM alert_watches WHERE watch_until <= ?",
                (iso_now,),
            )
            return int(cursor.rowcount or 0)

        async with self._write_lock:
            return await asyncio.to_thread(_write)

    async def get_last_callback_verb(self, alert_id: UUID) -> tuple[str, datetime] | None:
        def _read() -> tuple[str, datetime] | None:
            cursor = self._connection.execute(
                """
                SELECT verb, received_at FROM callbacks
                WHERE alert_id = ?
                ORDER BY audit_id DESC
                LIMIT 1
                """,
                (str(alert_id),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return str(row[0]), datetime.fromisoformat(row[1])

        return await asyncio.to_thread(_read)

    # ─────────────────────────────────────────────────────────────────
    # Alert updates — append-only audit (NFR-S4)
    # ─────────────────────────────────────────────────────────────────

    async def record_alert_update(self, update: AlertUpdate) -> None:
        def _write() -> None:
            self._connection.execute(
                """
                INSERT INTO alert_updates (
                    alert_id, change_kind, old_value, new_value,
                    edited_at, edit_ok, rendered_text
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(update.alert_id),
                    update.change_kind,
                    update.old_value,
                    update.new_value,
                    update.edited_at.isoformat(),
                    1 if update.edit_ok else 0,
                    update.rendered_text,
                ),
            )

        async with self._write_lock:
            await asyncio.to_thread(_write)

    async def get_alert_updates(self, alert_id: UUID) -> list[AlertUpdate]:
        def _read() -> list[AlertUpdate]:
            cursor = self._connection.execute(
                """
                SELECT alert_id, change_kind, old_value, new_value,
                       edited_at, edit_ok, rendered_text
                FROM alert_updates
                WHERE alert_id = ?
                ORDER BY audit_id ASC
                """,
                (str(alert_id),),
            )
            return [
                AlertUpdate(
                    alert_id=row["alert_id"],
                    change_kind=row["change_kind"],
                    old_value=row["old_value"],
                    new_value=row["new_value"],
                    edited_at=datetime.fromisoformat(row["edited_at"]),
                    edit_ok=bool(row["edit_ok"]),
                    rendered_text=row["rendered_text"],
                )
                for row in cursor.fetchall()
            ]

        return await asyncio.to_thread(_read)

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
                    phase, phase2_max_price_eur, rendered_at,
                    telegram_message_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    snapshot.telegram_message_id,
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
                       phase, phase2_max_price_eur, rendered_at,
                       telegram_message_id
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
                       phase, phase2_max_price_eur, rendered_at,
                       telegram_message_id
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
        telegram_message_id=row["telegram_message_id"],
    )


def _row_to_alert_watch(row: sqlite3.Row) -> AlertWatch:
    """Re-hydrate an :class:`AlertWatch` from a raw alert_watches row."""
    return AlertWatch(
        alert_id=row["alert_id"],
        listing_id=row["listing_id"],
        marketplace=row["marketplace"],
        entry_key=(row["entry_manufacturer"], row["entry_model"], row["entry_ref"]),
        telegram_message_id=row["telegram_message_id"],
        last_price_eur=Decimal(row["last_price_eur"]),
        last_is_reserved=bool(row["last_is_reserved"]),
        watch_until=datetime.fromisoformat(row["watch_until"]),
        last_edited_at=(
            datetime.fromisoformat(row["last_edited_at"])
            if row["last_edited_at"] is not None
            else None
        ),
    )
