"""Phase 2 pre-flight gate — Story 5.2 (FR23 / FR26 / FR27 / UX-DR7).

Five checks decide whether a wishlist match is eligible for the
autonomous-buy alert variant. The gate is consulted in two places:

  1. the poll loop, when dispatching a listing whose entry has
     ``phase2.enabled=true`` — a failing check downgrades the render to
     the Phase 1 anatomy silently (the operator never sees a "broken
     Phase 2 alert" with a missing Buy button);
  2. the buy orchestrator (Story 5.7), when an operator's Comprar tap
     arrives — re-running the gate catches lockouts that landed AFTER
     the alert was dispatched (e.g. a smoke-test failure or a circuit
     opening between alert and tap).

The checks are ordered so the cheap per-entry conditions short-circuit
before the gate touches the database.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.domain.pricing import (
    DEFAULT_ASSUMED_IMPORT_CHARGES_EUR,
    DEFAULT_ASSUMED_SHIPPING_EUR,
    buyer_total_eur,
)
from salvager.domain.wishlist import WishlistEntry
from salvager.interfaces.phase2_state_reader import Phase2StateReader

#: Smoke-test freshness window. A pass older than this is treated as
#: stale; the gate fails until the next smoke run renews the signal.
DEFAULT_SMOKE_FRESHNESS_HOURS: Final[int] = 24

#: Confidence ordering: low < medium < high. Mirrors the poll loop's gate.
_CONFIDENCE_RANK: Final[dict[str, int]] = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class Phase2EligibilityResult:
    """Outcome of one pre-flight evaluation.

    ``reason`` is ``None`` exactly when ``eligible`` is True. A failing
    check yields a stable string ID so callers can route by reason
    (e.g. structured logs, the ``phase2_alert_downgraded`` event).
    """

    eligible: bool
    reason: str | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Phase2Preflight:
    """The pre-flight gate. Constructed once per daemon, reused per check."""

    state_reader: Phase2StateReader
    circuit_breaker_threshold: int
    smoke_freshness_hours: int = DEFAULT_SMOKE_FRESHNESS_HOURS
    #: Buffer used for a listing's shipping when the marketplace didn't expose
    #: it, so the buy ceiling is checked against the delivered total — never
    #: the bare item price. Composer passes ``config.pricing.assumed_shipping_eur``.
    assumed_shipping_eur: Decimal = DEFAULT_ASSUMED_SHIPPING_EUR
    #: Estimated flat import charge added when the listing's item-location
    #: country is outside the EU (ebay-import-charges-pricing). Composer
    #: passes ``config.pricing.assumed_import_charges_eur``.
    assumed_import_charges_eur: Decimal = DEFAULT_ASSUMED_IMPORT_CHARGES_EUR
    clock: Callable[[], datetime] = field(default=_utc_now)

    async def check(
        self,
        entry: WishlistEntry,
        listing: Listing,
        evaluation: ListingEvaluation,
    ) -> Phase2EligibilityResult:
        # ── Per-entry checks (no DB round-trip) ──────────────────────
        if not entry.phase2.enabled:
            return Phase2EligibilityResult(False, "phase2_disabled_for_entry")
        # Reserved listings are no longer buyable inventory — let them
        # fall back to Phase 1 (alert-only) so the operator still sees
        # market signal, but never under a Buy CTA they'd tap into a
        # 404. Cheapest check, runs before any DB read.
        if listing.is_reserved:
            return Phase2EligibilityResult(False, "listing_reserved")
        max_price: Decimal | None = entry.phase2.max_price_eur
        if max_price is None:
            return Phase2EligibilityResult(False, "phase2_max_price_unset")
        # Gate on the delivered buyer total (item + shipping + Wallapop fee),
        # NOT the bare item price — otherwise shipping pushes the real cost
        # over the ceiling unnoticed (shipping-aware-pricing).
        if (
            buyer_total_eur(
                listing,
                assumed_shipping_eur=self.assumed_shipping_eur,
                assumed_import_charges_eur=self.assumed_import_charges_eur,
            )
            > max_price
        ):
            return Phase2EligibilityResult(False, "phase2_max_price_below_listing")
        if _CONFIDENCE_RANK[evaluation.confidence] < _CONFIDENCE_RANK[entry.confidence_threshold]:
            return Phase2EligibilityResult(False, "confidence_below_threshold")

        # ── Global Phase 2 state checks (one DB read) ────────────────
        state = await self.state_reader.read()
        if state.globally_disabled:
            return Phase2EligibilityResult(False, "globally_disabled")
        if state.consecutive_failures >= self.circuit_breaker_threshold:
            return Phase2EligibilityResult(False, "circuit_breaker_open")
        if state.last_smoke_at is None:
            return Phase2EligibilityResult(False, "smoke_test_never_run")
        if state.last_smoke_result != "pass":
            return Phase2EligibilityResult(False, "smoke_test_failed")
        if self.clock() - state.last_smoke_at > timedelta(hours=self.smoke_freshness_hours):
            return Phase2EligibilityResult(False, "smoke_test_stale")

        return Phase2EligibilityResult(eligible=True)


__all__ = [
    "DEFAULT_SMOKE_FRESHNESS_HOURS",
    "Phase2EligibilityResult",
    "Phase2Preflight",
]
