"""Alert-watch domain models — edit-alerts-on-state-change.

A dispatched alert is *watched* for a bounded window: the poll cycle diffs
freshly fetched listings against the watch's last-known state and edits the
original Telegram message when the listing flips reserved (either
direction) or drops its price past the configured threshold.

``AlertWatch`` is MUTABLE daemon state (same class as
``wishlist_runtime_state`` — not audit data). ``AlertUpdate`` is the
append-only audit record of one attempted edit; it carries the full
rendered body so ``audit show`` can replay exactly what the operator's
screen said (NFR-S4: inserts only).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: What changed on the watched listing. "available" is the reserved→available
#: flip-back; sold detection is explicitly out of scope (design.md, Resolved
#: Question 2).
ChangeKind = Literal["reserved", "available", "price_drop"]


class AlertWatch(BaseModel):
    """Last-known state of one dispatched alert's listing."""

    model_config = ConfigDict(extra="forbid")

    alert_id: UUID
    listing_id: str = Field(min_length=1)
    entry_key: tuple[str, str, str]
    telegram_message_id: int
    last_price_eur: Decimal
    last_is_reserved: bool = False
    watch_until: datetime
    last_edited_at: datetime | None = None


class AlertUpdate(BaseModel):
    """Audit record of one attempted alert edit (append-only)."""

    model_config = ConfigDict(extra="forbid")

    alert_id: UUID
    change_kind: ChangeKind
    old_value: str
    new_value: str
    edited_at: datetime
    edit_ok: bool
    rendered_text: str


__all__ = ["AlertUpdate", "AlertWatch", "ChangeKind"]
