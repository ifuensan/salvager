"""Append-only offer audit writer (wallapop-offer-flow, NFR-S4).

:class:`OfferAuditWriter` is the *only* write path into the ``offers``
audit table and the mutable single-row ``offer_state`` lockout —
mirroring :class:`Phase2AuditWriter`'s split exactly, and covered by the
same mechanical append-only lint (``test_audit_writer_append_only.py``):
no ``update_*``/``delete_*`` method, no UPDATE/DELETE against ``offers``.

The lockout is deliberately independent from ``phase2_state``: offer
failures must never block real buys, and vice versa (design D4).

Read helpers live here too (same connection, read-only queries): the
per-listing dedupe (``has_successful_offer``) and the rolling-24 h daily
budget count (``count_recent_successes``) that the offer preflight gates
on, plus the ``offer_state`` snapshot.

Threading model mirrors :class:`Phase2AuditWriter`: every DB call runs in
``asyncio.to_thread`` and a per-instance ``asyncio.Lock`` serializes
writes; the writer owns its own WAL connection.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from salvager.adapters.sqlite_store.connection import open_connection
from salvager.domain.offer_audit import OfferAttemptRecord, OfferStateSnapshot
from salvager.interfaces.store import EntryKey
from salvager.observability.logging import get_logger

#: The self-imposed daily budget counts successful sends inside this window.
_DAILY_WINDOW = timedelta(hours=24)


class OfferAuditWriter:
    """INSERT-only writer for ``offers`` + the mutable ``offer_state`` row."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._connection = open_connection(self._db_path)
        self._write_lock = asyncio.Lock()
        self._log = get_logger("adapter.offer_audit_writer")

    async def close(self) -> None:
        """Close the underlying SQLite connection."""
        async with self._write_lock:
            await asyncio.to_thread(self._connection.close)

    # ─────────────────────────────────────────────────────────────────
    # Append-only audit rows
    # ─────────────────────────────────────────────────────────────────

    async def record_offer_attempt(self, attempt: OfferAttemptRecord) -> int:
        def _write() -> int:
            cursor = self._connection.execute(
                """
                INSERT INTO offers (
                    alert_id, listing_id, marketplace,
                    entry_manufacturer, entry_model, entry_ref,
                    offered_eur, asking_eur, outcome, failure_reason,
                    screenshot_path, platform_remaining, status, attempted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(attempt.alert_id),
                    attempt.listing_id,
                    attempt.marketplace,
                    attempt.entry_key[0],
                    attempt.entry_key[1],
                    attempt.entry_key[2],
                    str(attempt.offered_eur),
                    str(attempt.asking_eur),
                    attempt.outcome,
                    attempt.failure_reason.value if attempt.failure_reason else None,
                    attempt.screenshot_path,
                    attempt.platform_remaining,
                    attempt.status,
                    attempt.attempted_at.isoformat(),
                ),
            )
            return int(cursor.lastrowid or 0)

        async with self._write_lock:
            return await asyncio.to_thread(_write)

    # ─────────────────────────────────────────────────────────────────
    # Read helpers — dedupe, daily budget, lockout snapshot
    # ─────────────────────────────────────────────────────────────────

    async def has_successful_offer(self, marketplace: str, listing_id: str) -> bool:
        """Per-listing dedupe: one successful send per listing, ever (v1)."""

        def _read() -> bool:
            cursor = self._connection.execute(
                "SELECT 1 FROM offers WHERE marketplace = ? AND listing_id = ? "
                "AND outcome = 'success' LIMIT 1",
                (marketplace, listing_id),
            )
            return cursor.fetchone() is not None

        return await asyncio.to_thread(_read)

    async def count_recent_successes(self, *, now: datetime | None = None) -> int:
        """Successful sends inside the trailing 24 h — the daily-budget count."""
        reference = now if now is not None else datetime.now(UTC)
        cutoff = (reference - _DAILY_WINDOW).isoformat()

        def _read() -> int:
            cursor = self._connection.execute(
                "SELECT COUNT(*) FROM offers WHERE outcome = 'success' AND attempted_at >= ?",
                (cutoff,),
            )
            return int(cursor.fetchone()[0])

        return await asyncio.to_thread(_read)

    async def read_state(self) -> OfferStateSnapshot:
        """The mutable lockout row, as of now."""

        def _read() -> OfferStateSnapshot:
            cursor = self._connection.execute(
                "SELECT globally_disabled, disabled_at, disabled_reason, consecutive_failures "
                "FROM offer_state WHERE id = 1"
            )
            row = cursor.fetchone()
            return OfferStateSnapshot(
                globally_disabled=bool(row[0]),
                disabled_at=datetime.fromisoformat(row[1]) if row[1] else None,
                disabled_reason=row[2],
                consecutive_failures=int(row[3]),
            )

        return await asyncio.to_thread(_read)

    # ─────────────────────────────────────────────────────────────────
    # offer_state — the single mutable lockout row
    # ─────────────────────────────────────────────────────────────────

    async def set_global_disable(self, reason: str) -> None:
        """Lock the offer path globally. Durable across restarts."""
        now = datetime.now(UTC).isoformat()

        def _write() -> None:
            self._connection.execute(
                """
                UPDATE offer_state
                SET globally_disabled = 1, disabled_at = ?, disabled_reason = ?
                WHERE id = 1
                """,
                (now, reason),
            )

        async with self._write_lock:
            await asyncio.to_thread(_write)
        self._log.warning("offer_globally_disabled", extra={"reason": reason})

    async def clear_global_disable(self, entry_key: EntryKey) -> None:
        """Lift the lockout — only via ``salvager offer enable <entry>``.

        ``entry_key`` is the explicit operator-action context (same contract
        as the Phase 2 sibling): the lockout never clears automatically.
        Also zeroes the consecutive-failure counter — re-enabling is a fresh
        start, not a resume at threshold-minus-one.
        """

        def _write() -> None:
            self._connection.execute(
                """
                UPDATE offer_state
                SET globally_disabled = 0, disabled_at = NULL, disabled_reason = NULL,
                    consecutive_failures = 0
                WHERE id = 1
                """,
            )

        async with self._write_lock:
            await asyncio.to_thread(_write)
        self._log.info(
            "offer_global_disable_cleared",
            extra={"entry_key": list(entry_key)},
        )

    async def increment_failure_counter(self) -> int:
        """Bump the consecutive-failure counter; return the new value."""

        def _write() -> int:
            self._connection.execute(
                "UPDATE offer_state SET consecutive_failures = consecutive_failures + 1 "
                "WHERE id = 1"
            )
            cursor = self._connection.execute(
                "SELECT consecutive_failures FROM offer_state WHERE id = 1"
            )
            return int(cursor.fetchone()[0])

        async with self._write_lock:
            return await asyncio.to_thread(_write)

    async def reset_failure_counter(self) -> None:
        """Zero the consecutive-failure counter (a successful send)."""

        def _write() -> None:
            self._connection.execute("UPDATE offer_state SET consecutive_failures = 0 WHERE id = 1")

        async with self._write_lock:
            await asyncio.to_thread(_write)


__all__ = ["OfferAuditWriter"]
