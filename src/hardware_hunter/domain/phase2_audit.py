"""Phase 2 audit input models — Story 5.1 (AR8 / AR9 / NFR-S4).

These are the typed write-contracts for :class:`Phase2AuditWriter`: one
model per append-only Phase 2 audit table. They are deliberately *not*
the AR24 placeholder stubs in :mod:`hardware_hunter.domain.audit`
(``TapEventAudit`` / ``TransactionAudit``) — those remain guard-walled
shapes for the discriminated union. With Epic 5 building Phase 2 for
real, these models carry the full column set the 0002 migration
declares, and they are freely constructible.

There is no Phase 2 *state* model here: ``phase2_state`` is mutable,
single-row, and read back through the writer's counter/lockout methods —
it is not an append-only audit row.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TapEventRecord(BaseModel):
    """One Phase 2 inline-button tap — written to ``tap_events``."""

    model_config = ConfigDict(extra="forbid")

    alert_id: UUID
    verb: Literal["buy", "skip", "view"]
    raw_payload: dict[str, object]
    tapped_at: datetime
    ip_or_chat_id: str


class TransactionRecord(BaseModel):
    """One completed autonomous purchase — written to ``transactions``."""

    model_config = ConfigDict(extra="forbid")

    alert_id: UUID
    price_paid_eur: Decimal
    payment_method: Literal["wallapop_pay", "ebay_checkout"]
    receipt_id: str
    screenshot_path: str
    total_seconds: int
    committed_at: datetime


class SmokeTestRecord(BaseModel):
    """One daily synthetic smoke-test run — written to ``phase2_smoke_tests``."""

    model_config = ConfigDict(extra="forbid")

    run_at: datetime
    result: Literal["pass", "fail"]
    parsed_price: Decimal
    independent_price: Decimal
    delta_eur: Decimal
    delta_pct: Decimal


__all__ = ["SmokeTestRecord", "TapEventRecord", "TransactionRecord"]
