"""Append-only audit log schema — Story 3.1 (Phase 1 subset) + AR24 stubs.

The audit log is the source of truth for what the daemon did and why.
NFR-S4 mandates append-only — no ``update_*`` or ``delete_*`` methods
exist on the audit tables in either the :class:`Store` ABC (Story 3.2)
or the SQLite implementation (Story 3.3).

Phase 1 variants
----------------
- :class:`AlertSnapshotAudit` — one row per alert dispatched
- :class:`CallbackAudit`      — one row per inline-button tap

Phase 2 stubs (AR24)
--------------------
:class:`TapEventAudit` and :class:`TransactionAudit` are declared so the
discriminated-union shape is complete and the SQLite schema migration
knows the full row set up-front. But they raise
:class:`Phase2GuardrailTripped` at construction time at v0.x — if any
code path tries to write a Phase 2 audit before Phase 2 is enabled,
the operator gets a loud crash, not a silent no-op.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Phase2GuardrailTripped(RuntimeError):
    """Raised when Phase 2 audit variants are constructed at v0.x.

    Per AR24: Phase 2 schema is declared up-front so migrations are
    monotonic, but the variants are *guard-walled* against accidental
    use until Phase 2 has been explicitly enabled. The poll loop and
    every renderer treat this as a fatal — by design.
    """


class AlertSnapshotAudit(BaseModel):
    """One row recording an alert delivered to the operator."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["alert_snapshot"] = "alert_snapshot"
    audit_id: UUID
    alert_id: UUID
    entry_key: tuple[str, str, str]
    listing_id: str
    marketplace: Literal["wallapop", "ebay"]
    phase: Literal["phase1", "phase2"]
    telegram_message_id: int
    occurred_at: datetime


class CallbackAudit(BaseModel):
    """One row recording an operator's inline-button tap."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["callback"] = "callback"
    audit_id: UUID
    alert_id: UUID
    telegram_message_id: int
    callback_data: str
    verb: Literal["view", "skip", "snooze", "buy", "offer"]
    chat_id: int
    occurred_at: datetime


# ─────────────────────────────────────────────────────────────────────────
# Phase 2 stubs — declared but un-instantiable at v0.x (AR24)
# ─────────────────────────────────────────────────────────────────────────


class _Phase2GuardrailMixin(BaseModel):
    """Refuse construction until Phase 2 is enabled (AR24).

    The validator runs ``before`` so the guardrail trips even when the
    caller didn't supply enough fields to make pydantic happy — there
    is no codepath that returns a partially constructed Phase 2 audit
    object.
    """

    @model_validator(mode="before")
    @classmethod
    def _phase2_disabled(cls, _data: Any) -> Any:
        raise Phase2GuardrailTripped(
            f"{cls.__name__} is a Phase 2 audit type; Phase 2 is not enabled at v0.x (AR24)"
        )


class TapEventAudit(_Phase2GuardrailMixin, BaseModel):
    """Phase 2 stub — operator's buy-confirmation tap.

    Schema declared for migration completeness; raises
    :class:`Phase2GuardrailTripped` if any code attempts to build one
    before Phase 2 is enabled.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["tap_event"] = "tap_event"
    audit_id: UUID
    alert_id: UUID
    occurred_at: datetime


class TransactionAudit(_Phase2GuardrailMixin, BaseModel):
    """Phase 2 stub — completed autonomous purchase."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["transaction"] = "transaction"
    audit_id: UUID
    alert_id: UUID
    price_paid_eur: Decimal
    succeeded: bool
    occurred_at: datetime


# Discriminated union over every audit variant — the Store API surface
# accepts ``AuditEntry`` and dispatches on ``kind`` to write to the right
# SQLite table. Pydantic's discriminated-union resolver only looks at
# the ``kind`` literal, so the Phase 2 guard-rail in the model_validator
# above doesn't fire during type-narrowing.
AuditEntry = Annotated[
    AlertSnapshotAudit | CallbackAudit | TapEventAudit | TransactionAudit,
    Field(discriminator="kind"),
]
