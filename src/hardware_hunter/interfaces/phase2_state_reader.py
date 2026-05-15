"""Phase 2 state read port — Story 5.2.

The pre-flight gate and the circuit breaker read the mutable
``phase2_state`` row without going through :class:`Phase2AuditWriter`'s
narrow append-only surface. This Protocol is the contract; the SQLite
adapter implements it.
"""

from __future__ import annotations

from typing import Protocol

from hardware_hunter.domain.phase2_audit import Phase2StateSnapshot


class Phase2StateReader(Protocol):
    """Reads the single Phase 2 state row.

    Implementations may cache, hit the DB on every call, or proxy to a
    fake in tests — callers must treat the returned snapshot as
    point-in-time and re-read when freshness matters.
    """

    async def read(self) -> Phase2StateSnapshot: ...


__all__ = ["Phase2StateReader"]
