"""Offer-amount derivation tests (wallapop-offer-flow).

The offer is the largest whole-euro item price whose Wallapop buyer total
(item + shipping + Protección) fits the entry's offer target, bounded by
the platform rules: strictly below the asking price and at or above 70 %
of it (Wallapop rejects discounts deeper than 30 %).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from salvager.domain.listing import Listing
from salvager.domain.pricing import (
    OFFER_PLATFORM_FLOOR_RATIO,
    buyer_total_eur,
    offer_item_price_eur,
)

_TS = datetime(2026, 7, 22, tzinfo=UTC)
_BUFFER = Decimal("3.50")


def _listing(**overrides: object) -> Listing:
    base: dict[str, object] = {
        "listing_id": "x",
        "marketplace": "wallapop",
        "url": "https://es.wallapop.com/item/x",
        "title": "Corsair Vengeance LPX 16GB",
        "description": "d",
        "price_eur": Decimal("88.00"),
        "shipping_eur": Decimal("3.50"),
        "fetched_at": _TS,
    }
    base.update(overrides)
    return Listing(**base)  # type: ignore[arg-type]


def test_band_listing_gets_ceiling_fit_offer() -> None:
    # 88 € asking against an 80 € target: largest whole-euro O with
    # O + 3.50 + (0.69 + 7.5 % O) ≤ 80 is 70 €.
    listing = _listing()
    offer = offer_item_price_eur(
        listing, target_total_eur=Decimal("80"), assumed_shipping_eur=_BUFFER
    )
    assert offer == Decimal("70")
    # The fit is genuine and maximal: 70 fits the target, 71 would not.
    assert buyer_total_eur(
        _listing(price_eur=Decimal("70")), assumed_shipping_eur=_BUFFER
    ) <= Decimal("80")
    assert buyer_total_eur(
        _listing(price_eur=Decimal("71")), assumed_shipping_eur=_BUFFER
    ) > Decimal("80")


def test_under_ceiling_listing_with_default_target_yields_no_offer() -> None:
    # Buyer total already fits the target → the fit price lands at or above
    # asking → nothing to negotiate.
    listing = _listing(price_eur=Decimal("55.00"), shipping_eur=Decimal("3.49"))
    assert (
        offer_item_price_eur(listing, target_total_eur=Decimal("80"), assumed_shipping_eur=_BUFFER)
        is None
    )


def test_lower_per_entry_target_activates_offers_under_the_ceiling() -> None:
    # Asking 70 € (buyer total ~79.44 €, under an 80 € ceiling) with a 70 €
    # target: the offer aims the delivered total at the operator's target.
    listing = _listing(price_eur=Decimal("70.00"))
    offer = offer_item_price_eur(
        listing, target_total_eur=Decimal("70"), assumed_shipping_eur=_BUFFER
    )
    assert offer == Decimal("61")
    assert offer < listing.price_eur


def test_platform_floor_blocks_too_deep_offers() -> None:
    # Fit price 33 € against a 60 € asking price is under the 70 % floor.
    listing = _listing(price_eur=Decimal("60.00"))
    assert (
        offer_item_price_eur(listing, target_total_eur=Decimal("40"), assumed_shipping_eur=_BUFFER)
        is None
    )
    # Exactly at the floor is accepted (the UI says "al menos").
    at_floor = _listing(price_eur=Decimal("100.00"))
    offer = offer_item_price_eur(
        at_floor, target_total_eur=Decimal("79.50"), assumed_shipping_eur=_BUFFER
    )
    assert offer == Decimal("70")
    assert offer == at_floor.price_eur * OFFER_PLATFORM_FLOOR_RATIO


def test_proteccion_flat_threshold_branch() -> None:
    # Budget 16 € after shipping: 14 € pays the variable fee (0.69 + 7.5 %),
    # 13 € would pay the flat 1.69 € — the fit must settle with the real
    # schedule across the boundary, not the linear closed form alone.
    listing = _listing(price_eur=Decimal("18.00"))
    offer = offer_item_price_eur(
        listing, target_total_eur=Decimal("19.50"), assumed_shipping_eur=_BUFFER
    )
    assert offer == Decimal("14")


def test_unknown_shipping_uses_the_buffer() -> None:
    known = offer_item_price_eur(
        _listing(), target_total_eur=Decimal("80"), assumed_shipping_eur=_BUFFER
    )
    unknown = offer_item_price_eur(
        _listing(shipping_eur=None), target_total_eur=Decimal("80"), assumed_shipping_eur=_BUFFER
    )
    assert known == unknown == Decimal("70")


def test_non_wallapop_listing_never_offers() -> None:
    listing = _listing(marketplace="ebay", url="https://www.ebay.es/itm/x")
    assert (
        offer_item_price_eur(listing, target_total_eur=Decimal("80"), assumed_shipping_eur=_BUFFER)
        is None
    )


def test_target_below_shipping_yields_no_offer() -> None:
    listing = _listing()
    assert (
        offer_item_price_eur(listing, target_total_eur=Decimal("3"), assumed_shipping_eur=_BUFFER)
        is None
    )
