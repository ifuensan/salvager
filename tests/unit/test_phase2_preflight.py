"""Pre-flight gate unit tests — Story 5.2.

Each AC condition gets one focused negative case + one happy-path
positive case at the end. The state reader is a tiny fake so the
checks are exercised in isolation from any DB.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.domain.phase2_audit import Phase2StateSnapshot
from salvager.domain.wishlist import Phase2Settings, WishlistEntry
from salvager.orchestration.phase2_preflight import (
    Phase2EligibilityResult,
    Phase2Preflight,
)

_T0 = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)


def _state(**overrides: object) -> Phase2StateSnapshot:
    base: dict[str, object] = {
        "globally_disabled": False,
        "consecutive_failures": 0,
        "last_smoke_result": "pass",
        "last_smoke_at": _T0 - timedelta(hours=2),
    }
    base.update(overrides)
    return Phase2StateSnapshot(**base)  # type: ignore[arg-type]


class _StubReader:
    def __init__(self, state: Phase2StateSnapshot) -> None:
        self._state = state

    async def read(self) -> Phase2StateSnapshot:
        return self._state


def _entry(**phase2_overrides: object) -> WishlistEntry:
    return WishlistEntry.model_validate(
        {
            "manufacturer": "Western Digital",
            "model": "WD Red Plus 4TB",
            "ref": "WD40EFPX",
            "type": "hdd",
            "keywords": ["wd red plus 4tb"],
            "max_price_solo": Decimal("70.00"),
            "confidence_threshold": "medium",
            "phase2": {
                "enabled": True,
                "max_price_eur": "60.00",
                **phase2_overrides,
            },
        }
    )


def _listing(
    price: str = "45.00",
    *,
    marketplace: str = "wallapop",
    shipping_eur: str | None = None,
) -> Listing:
    # Default price leaves headroom for the buyer total (item + shipping
    # buffer + Wallapop Protección) under the 60 € Phase 2 ceiling so the
    # happy path stays eligible (shipping-aware-pricing).
    return Listing(
        listing_id="abc123",
        marketplace=marketplace,  # type: ignore[arg-type]
        url="https://wallapop.com/item/abc123",
        title="WD Red Plus 4TB",
        description="ok",
        price_eur=Decimal(price),
        shipping_eur=Decimal(shipping_eur) if shipping_eur is not None else None,
        location="Madrid",
        photo_urls=[],
        fetched_at=_T0,
    )


def _evaluation(confidence: str = "high") -> ListingEvaluation:
    return ListingEvaluation(
        listing_id="abc123",
        entry_key=("Western Digital", "WD Red Plus 4TB", "WD40EFPX"),
        confidence=confidence,  # type: ignore[arg-type]
        one_line_take="Strong match.",
        is_container=False,
        evaluated_at=_T0,
    )


def _preflight(state: Phase2StateSnapshot, **overrides: object) -> Phase2Preflight:
    base = Phase2Preflight(
        state_reader=_StubReader(state),
        circuit_breaker_threshold=3,
        clock=lambda: _T0,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────


async def test_eligible_when_every_check_passes() -> None:
    result = await _preflight(_state()).check(_entry(), _listing(), _evaluation())
    assert result == Phase2EligibilityResult(eligible=True)


# ─────────────────────────────────────────────────────────────────────────
# Per-entry checks (short-circuit before the DB read)
# ─────────────────────────────────────────────────────────────────────────


async def test_entry_phase2_disabled_is_ineligible() -> None:
    result = await _preflight(_state()).check(_entry(enabled=False), _listing(), _evaluation())
    assert result.eligible is False
    assert result.reason == "phase2_disabled_for_entry"


async def test_reserved_listing_is_ineligible_for_phase2() -> None:
    """A reserved listing can't be Phase-2-bought even if every other
    check would pass — the inventory's gone. The gate downgrades the
    alert to Phase 1 so the operator still sees market signal, no Buy
    CTA.
    """
    reserved = _listing().model_copy(update={"is_reserved": True})
    result = await _preflight(_state()).check(_entry(), reserved, _evaluation())
    assert result.eligible is False
    assert result.reason == "listing_reserved"


async def test_max_price_unset_is_ineligible() -> None:
    entry = _entry()
    entry = entry.model_copy(update={"phase2": Phase2Settings(enabled=True, max_price_eur=None)})
    result = await _preflight(_state()).check(entry, _listing(), _evaluation())
    assert result.reason == "phase2_max_price_unset"


async def test_listing_above_phase2_max_is_ineligible() -> None:
    result = await _preflight(_state()).check(_entry(), _listing(price="61.00"), _evaluation())
    assert result.reason == "phase2_max_price_below_listing"


async def test_item_under_ceiling_but_buyer_total_over_is_ineligible() -> None:
    """The gate compares the delivered total, not the item price.

    A 59 € item is under the 60 € ceiling, but once the shipping buffer and
    Wallapop Protección are added the buyer total exceeds it → ineligible
    (shipping-aware-pricing).
    """
    result = await _preflight(_state()).check(_entry(), _listing(price="59.00"), _evaluation())
    assert result.reason == "phase2_max_price_below_listing"


async def test_known_shipping_within_ceiling_is_eligible() -> None:
    """An eBay listing (no Protección fee) whose item + known shipping sits
    under the ceiling stays buyable."""
    listing = _listing(price="55.00", marketplace="ebay", shipping_eur="3.00")
    result = await _preflight(_state()).check(_entry(), listing, _evaluation())
    assert result == Phase2EligibilityResult(eligible=True)


async def test_confidence_below_threshold_is_ineligible() -> None:
    # entry threshold is medium; an explicit low evaluation must fail.
    result = await _preflight(_state()).check(_entry(), _listing(), _evaluation(confidence="low"))
    assert result.reason == "confidence_below_threshold"


# ─────────────────────────────────────────────────────────────────────────
# Global state checks
# ─────────────────────────────────────────────────────────────────────────


async def test_globally_disabled_is_ineligible() -> None:
    result = await _preflight(_state(globally_disabled=True)).check(
        _entry(), _listing(), _evaluation()
    )
    assert result.reason == "globally_disabled"


async def test_circuit_breaker_open_is_ineligible() -> None:
    result = await _preflight(_state(consecutive_failures=3)).check(
        _entry(), _listing(), _evaluation()
    )
    assert result.reason == "circuit_breaker_open"


async def test_circuit_below_threshold_still_eligible() -> None:
    result = await _preflight(_state(consecutive_failures=2)).check(
        _entry(), _listing(), _evaluation()
    )
    assert result.eligible is True


async def test_smoke_test_never_run_is_ineligible() -> None:
    result = await _preflight(_state(last_smoke_result=None, last_smoke_at=None)).check(
        _entry(), _listing(), _evaluation()
    )
    assert result.reason == "smoke_test_never_run"


async def test_smoke_test_failed_is_ineligible() -> None:
    result = await _preflight(_state(last_smoke_result="fail")).check(
        _entry(), _listing(), _evaluation()
    )
    assert result.reason == "smoke_test_failed"


async def test_smoke_test_stale_is_ineligible() -> None:
    stale_state = _state(last_smoke_at=_T0 - timedelta(hours=25))
    result = await _preflight(stale_state).check(_entry(), _listing(), _evaluation())
    assert result.reason == "smoke_test_stale"
