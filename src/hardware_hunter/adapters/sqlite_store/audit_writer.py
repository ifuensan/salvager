"""Append-only Phase 2 audit writer — Story 5.1 (AR8 / AR9 / AR13 / NFR-S4).

:class:`Phase2AuditWriter` is the *only* write path into the Phase 2
audit tables. Its surface is deliberately narrow:

  - three ``record_*`` methods, each a single INSERT into an
    append-only audit table (``tap_events`` / ``transactions`` /
    ``phase2_smoke_tests``) — no row is ever updated or deleted;
  - four ``phase2_state`` methods (``set_global_disable`` /
    ``clear_global_disable`` / ``increment_failure_counter`` /
    ``reset_failure_counter``) that UPDATE the single mutable
    lockout/circuit-breaker row in place.

NFR-S4 mechanical enforcement: no method on this class is named
``update_*`` or ``delete_*``. ``tests/unit/test_audit_writer_append_only.py``
introspects the class and fails the build if one is ever added — so a
PR that tries to make an audit row mutable cannot land.

``clear_global_disable`` takes an ``entry_key`` argument it does not
persist: the argument *is* the contract. Per FR35, the global lockout
may only be lifted by an explicit operator action that names the entry
being re-enabled (``phase2 enable <entry>``), never by an automatic
recovery path.

Threading model mirrors :class:`SqliteStore`: every DB call runs in
``asyncio.to_thread`` and a per-instance ``asyncio.Lock`` serializes
writes. The writer owns its own WAL connection — safe to run alongside
the daemon's :class:`SqliteStore` connection on the same database file.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from hardware_hunter.adapters.sqlite_store.connection import open_connection
from hardware_hunter.domain.phase2_audit import (
    SmokeTestRecord,
    TapEventRecord,
    TransactionRecord,
)
from hardware_hunter.interfaces.store import EntryKey
from hardware_hunter.observability.logging import get_logger


class Phase2AuditWriter:
    """INSERT-only writer for the Phase 2 audit + state tables."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._connection = open_connection(self._db_path)
        self._write_lock = asyncio.Lock()
        self._log = get_logger("adapter.phase2_audit_writer")

    async def close(self) -> None:
        """Close the underlying SQLite connection."""
        async with self._write_lock:
            await asyncio.to_thread(self._connection.close)

    # ─────────────────────────────────────────────────────────────────
    # Append-only audit rows — one INSERT each, returns the audit_id
    # ─────────────────────────────────────────────────────────────────

    async def record_tap_event(self, tap: TapEventRecord) -> int:
        raw_payload = json.dumps(tap.raw_payload, sort_keys=True)

        def _write() -> int:
            cursor = self._connection.execute(
                """
                INSERT INTO tap_events (
                    alert_id, verb, raw_payload, tapped_at, ip_or_chat_id
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(tap.alert_id),
                    tap.verb,
                    raw_payload,
                    tap.tapped_at.isoformat(),
                    tap.ip_or_chat_id,
                ),
            )
            return int(cursor.lastrowid or 0)

        async with self._write_lock:
            return await asyncio.to_thread(_write)

    async def record_transaction(self, txn: TransactionRecord) -> int:
        def _write() -> int:
            cursor = self._connection.execute(
                """
                INSERT INTO transactions (
                    alert_id, price_paid_eur, payment_method, receipt_id,
                    screenshot_path, total_seconds, committed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(txn.alert_id),
                    str(txn.price_paid_eur),
                    txn.payment_method,
                    txn.receipt_id,
                    txn.screenshot_path,
                    txn.total_seconds,
                    txn.committed_at.isoformat(),
                ),
            )
            return int(cursor.lastrowid or 0)

        async with self._write_lock:
            return await asyncio.to_thread(_write)

    async def record_smoke_test(self, result: SmokeTestRecord) -> int:
        def _write() -> int:
            cursor = self._connection.execute(
                """
                INSERT INTO phase2_smoke_tests (
                    run_at, result, parsed_price, independent_price,
                    delta_eur, delta_pct
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    result.run_at.isoformat(),
                    result.result,
                    str(result.parsed_price),
                    str(result.independent_price),
                    str(result.delta_eur),
                    str(result.delta_pct),
                ),
            )
            audit_id = int(cursor.lastrowid or 0)
            # The smoke-test outcome is also the freshest signal the
            # pre-flight gate reads — mirror it onto the state row.
            self._connection.execute(
                """
                UPDATE phase2_state
                SET last_smoke_result = ?, last_smoke_at = ?
                WHERE id = 1
                """,
                (result.result, result.run_at.isoformat()),
            )
            return audit_id

        async with self._write_lock:
            return await asyncio.to_thread(_write)

    # ─────────────────────────────────────────────────────────────────
    # phase2_state — the single mutable lockout / circuit-breaker row
    # ─────────────────────────────────────────────────────────────────

    async def set_global_disable(self, reason: str) -> None:
        """Lock Phase 2 globally. Durable across restarts (AR13)."""
        now = datetime.now(UTC).isoformat()

        def _write() -> None:
            self._connection.execute(
                """
                UPDATE phase2_state
                SET globally_disabled = 1, disabled_at = ?, disabled_reason = ?
                WHERE id = 1
                """,
                (now, reason),
            )

        async with self._write_lock:
            await asyncio.to_thread(_write)
        self._log.warning("phase2_globally_disabled", extra={"reason": reason})

    async def clear_global_disable(self, entry_key: EntryKey) -> None:
        """Lift the global lockout — the ONLY method that does so.

        ``entry_key`` is the explicit operator-action context required by
        FR35: the lockout never clears automatically, only via
        ``phase2 enable <entry>`` naming the entry being re-enabled.
        """

        def _write() -> None:
            self._connection.execute(
                """
                UPDATE phase2_state
                SET globally_disabled = 0, disabled_at = NULL, disabled_reason = NULL
                WHERE id = 1
                """,
            )

        async with self._write_lock:
            await asyncio.to_thread(_write)
        self._log.info(
            "phase2_global_disable_cleared",
            extra={"entry_key": list(entry_key)},
        )

    async def increment_failure_counter(self) -> int:
        """Bump the consecutive-failure counter; return the new value."""

        def _write() -> int:
            self._connection.execute(
                "UPDATE phase2_state SET consecutive_failures = consecutive_failures + 1 "
                "WHERE id = 1"
            )
            cursor = self._connection.execute(
                "SELECT consecutive_failures FROM phase2_state WHERE id = 1"
            )
            return int(cursor.fetchone()[0])

        async with self._write_lock:
            return await asyncio.to_thread(_write)

    async def reset_failure_counter(self) -> None:
        """Zero the consecutive-failure counter (a Phase 2 success)."""

        def _write() -> None:
            self._connection.execute(
                "UPDATE phase2_state SET consecutive_failures = 0 WHERE id = 1"
            )

        async with self._write_lock:
            await asyncio.to_thread(_write)


__all__ = ["Phase2AuditWriter"]
