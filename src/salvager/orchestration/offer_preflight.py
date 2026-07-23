"""Offer pre-flight gate (wallapop-offer-flow, FR58-FR65).

The offer sibling of :class:`Phase2Preflight`: consulted when an
operator's Ofertar tap arrives, re-checking everything that may have
changed since the alert was dispatched. Checks are ordered cheap →
expensive: per-entry/listing conditions short-circuit before the gate
touches the database.

The gate deliberately does NOT check the offer amount — the orchestrator
recomputes it from the reconciled listing right after this gate passes
(the amount check needs the fresh listing anyway).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from salvager.adapters.sqlite_store.offer_writer import OfferAuditWriter
from salvager.domain.listing import Listing
from salvager.domain.wishlist import WishlistEntry


@dataclass(frozen=True)
class OfferEligibilityResult:
    """Outcome of one offer pre-flight evaluation.

    ``reason`` is ``None`` exactly when ``eligible`` is True; failing
    checks yield stable string IDs for logs and the outcome ctx.
    """

    eligible: bool
    reason: str | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class OfferPreflight:
    """The offer pre-flight gate. Constructed once per daemon."""

    offer_writer: OfferAuditWriter
    kill_switch_global: bool
    lockout_threshold: int
    daily_limit: int
    clock: Callable[[], datetime] = field(default=_utc_now)

    async def check(self, entry: WishlistEntry, listing: Listing) -> OfferEligibilityResult:
        # ── Per-entry / per-listing checks (no DB round-trip) ────────
        if not entry.offer.enabled:
            return OfferEligibilityResult(False, "offer_disabled_for_entry")
        if listing.marketplace != "wallapop":
            return OfferEligibilityResult(False, "not_wallapop")
        if listing.is_refurbished:
            return OfferEligibilityResult(False, "listing_refurbished")
        if listing.is_reserved:
            return OfferEligibilityResult(False, "listing_reserved")
        if self.kill_switch_global:
            return OfferEligibilityResult(False, "offer_kill_switch")

        # ── Global offer state checks (DB reads) ─────────────────────
        state = await self.offer_writer.read_state()
        if state.globally_disabled:
            return OfferEligibilityResult(False, "offer_lockout_engaged")
        if state.consecutive_failures >= self.lockout_threshold:
            return OfferEligibilityResult(False, "offer_lockout_engaged")
        recent = await self.offer_writer.count_recent_successes(now=self.clock())
        if recent >= self.daily_limit:
            return OfferEligibilityResult(False, "offer_daily_limit_reached")
        if await self.offer_writer.has_successful_offer(listing.marketplace, listing.listing_id):
            return OfferEligibilityResult(False, "duplicate_offer")

        return OfferEligibilityResult(eligible=True)


__all__ = ["OfferEligibilityResult", "OfferPreflight"]
