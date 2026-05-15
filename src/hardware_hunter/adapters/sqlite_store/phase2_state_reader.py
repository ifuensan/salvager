"""SQLite-backed :class:`Phase2StateReader` — Story 5.2.

Reads the single ``phase2_state`` row that migration 0002 seeds. The
adapter owns its own WAL connection so it can run alongside
:class:`SqliteStore` and :class:`Phase2AuditWriter` against the same DB
file without blocking either.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from hardware_hunter.adapters.sqlite_store.connection import open_connection
from hardware_hunter.domain.phase2_audit import Phase2StateSnapshot


def _maybe_dt(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value))


class SqlitePhase2StateReader:
    """Concrete :class:`Phase2StateReader` reading from a SQLite database."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._connection = open_connection(self._db_path)

    async def close(self) -> None:
        await asyncio.to_thread(self._connection.close)

    async def read(self) -> Phase2StateSnapshot:
        def _read() -> Phase2StateSnapshot:
            cursor = self._connection.execute(
                """
                SELECT globally_disabled, disabled_at, disabled_reason,
                       consecutive_failures, last_smoke_result, last_smoke_at
                FROM phase2_state
                WHERE id = 1
                """
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(
                    "phase2_state row missing — has migration 0002 run against this DB?"
                )
            last_smoke_result = row["last_smoke_result"]
            if last_smoke_result not in (None, "pass", "fail"):
                raise RuntimeError(f"unexpected last_smoke_result value: {last_smoke_result!r}")
            return Phase2StateSnapshot(
                globally_disabled=bool(row["globally_disabled"]),
                disabled_at=_maybe_dt(row["disabled_at"]),
                disabled_reason=row["disabled_reason"],
                consecutive_failures=int(row["consecutive_failures"]),
                last_smoke_result=last_smoke_result,
                last_smoke_at=_maybe_dt(row["last_smoke_at"]),
            )

        return await asyncio.to_thread(_read)


__all__ = ["SqlitePhase2StateReader"]
