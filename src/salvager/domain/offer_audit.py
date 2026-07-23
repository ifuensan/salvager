"""Offer audit input models (wallapop-offer-flow).

Typed write/read contracts for :class:`OfferAuditWriter` — the same split
as :mod:`salvager.domain.phase2_audit`: one append-only record model for
the ``offers`` table, one snapshot model for the mutable single-row
``offer_state`` lockout. Offer *taps* have no model here — they ride the
existing ``callbacks`` audit path like every other verb.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from salvager.domain.errors import OfferFailureReason


class OfferAttemptRecord(BaseModel):
    """One executed offer attempt — written to ``offers``.

    ``platform_remaining`` is Wallapop's "N ofertas restantes" counter when
    the agent saw it on the offer form (free observability of the platform's
    10/day budget); ``None`` when not visible. ``status`` is ``"sent"`` for
    every v1 row — a future acceptance-detection change extends the set.
    """

    model_config = ConfigDict(extra="forbid")

    alert_id: UUID
    listing_id: str
    marketplace: str
    entry_key: tuple[str, str, str]
    offered_eur: Decimal
    asking_eur: Decimal
    outcome: Literal["success", "failure", "aborted"]
    failure_reason: OfferFailureReason | None = None
    screenshot_path: str | None = None
    platform_remaining: int | None = None
    status: Literal["sent"] = "sent"
    attempted_at: datetime


class OfferStateSnapshot(BaseModel):
    """The mutable ``offer_state`` row, as read at one point in time."""

    model_config = ConfigDict(extra="forbid")

    globally_disabled: bool
    disabled_at: datetime | None = None
    disabled_reason: str | None = None
    consecutive_failures: int


__all__ = [
    "OfferAttemptRecord",
    "OfferStateSnapshot",
]
