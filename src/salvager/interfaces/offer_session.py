"""``OfferSession`` port (wallapop-offer-flow, FR50-FR57).

The port through which the offer orchestrator sends one bounded price
offer on a Wallapop listing. A sibling of :class:`BrowserSession`
rather than an extension of it: offers are Wallapop-only in v1, so the
eBay checkout flow never has to stub an ``execute_offer`` it cannot
implement.

Adapter discipline (NFR-M1) holds the same way as the buy path: the
orchestration layer composes ``OfferSession`` only and never sees the
TinyFish SDK.

The contract is fail-closed on the SEND: any uncertainty about whether
the offer went out MUST surface as :class:`OfferSendFailure` (never a
silent success) — the per-listing dedupe engages only on a verified
sent state. The screenshot is best-effort evidence, not the success
criterion: a verified send whose capture failed is still a success
(with ``screenshot_url = None``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from salvager.domain.errors import OfferFailureReason
from salvager.domain.listing import Listing


class OfferSuccess(BaseModel):
    """The offer form confirmed the sent state.

    ``platform_remaining`` is Wallapop's "N ofertas restantes para hoy"
    counter when the agent saw it on the form (after sending); ``None``
    when not visible. ``screenshot_url`` is the captured confirmation
    when available — evidence, not the success criterion.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["success"] = "success"
    offered_eur: Decimal = Field(gt=0)
    screenshot_url: str | None = None
    platform_remaining: int | None = None
    total_seconds: int = Field(ge=0)


class OfferSendFailure(BaseModel):
    """The offer was NOT verifiably sent.

    ``ctx`` carries the variant-specific detail the renderer needs —
    each :class:`OfferFailureReason` documents its own ctx contract.
    ``screenshot_missing`` is the one deliberately ambiguous variant:
    the agent submitted but could not verify the sent state, so the
    alert copy directs the operator to the Wallapop chat.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["failure"] = "failure"
    reason: OfferFailureReason
    ctx: dict[str, Any] = Field(default_factory=dict)


#: Discriminated union — the orchestrator pattern-matches on ``kind``.
OfferResult = Annotated[OfferSuccess | OfferSendFailure, Field(discriminator="kind")]


class OfferSession(ABC):
    """Port for sending one bounded offer on a marketplace listing."""

    @abstractmethod
    async def execute_offer(self, listing: Listing, amount_eur: Decimal) -> OfferResult:
        """Send an offer of EXACTLY ``amount_eur`` on ``listing``.

        The flow embeds the amount in the agent goal as a hard
        constraint — the agent must never enter any other value, and
        must abort (``amount_rejected``) if the form refuses it.
        """


__all__ = [
    "OfferResult",
    "OfferSendFailure",
    "OfferSession",
    "OfferSuccess",
]
