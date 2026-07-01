"""Shipping-aware buyer-total tests (shipping-aware-pricing)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from salvager.domain.listing import Listing
from salvager.domain.pricing import buyer_cost, buyer_total_eur, proteccion_wallapop_fee

_TS = datetime(2026, 6, 16, tzinfo=UTC)
_BUFFER = Decimal("3.50")


def _listing(**overrides: object) -> Listing:
    base: dict[str, object] = {
        "listing_id": "x",
        "marketplace": "wallapop",
        "url": "https://es.wallapop.com/item/x",
        "title": "Corsair Vengeance LPX 16GB",
        "description": "d",
        "price_eur": Decimal("55.00"),
        "fetched_at": _TS,
    }
    base.update(overrides)
    return Listing(**base)  # type: ignore[arg-type]


# ── Protección Wallapop fee ───────────────────────────────────────────────


def test_proteccion_fee_flat_at_or_below_13() -> None:
    assert proteccion_wallapop_fee(Decimal("13.00")) == Decimal("1.69")
    assert proteccion_wallapop_fee(Decimal("1.00")) == Decimal("1.69")


def test_proteccion_fee_variable_above_13() -> None:
    # 13.01 crosses into the variable band: 0.69 + 7.5%*13.01 = 1.66575 → 1.67
    assert proteccion_wallapop_fee(Decimal("13.01")) == Decimal("1.67")
    # 55 €: 0.69 + 0.075*55 = 4.815 → 4.82 (half-up)
    assert proteccion_wallapop_fee(Decimal("55.00")) == Decimal("4.82")


def test_proteccion_fee_capped() -> None:
    # A very expensive item is clamped to the ~50€ cap.
    assert proteccion_wallapop_fee(Decimal("5000.00")) == Decimal("50")


# ── Buyer total per marketplace ───────────────────────────────────────────


def test_wallapop_total_includes_fee_and_known_shipping() -> None:
    cost = buyer_cost(
        _listing(price_eur=Decimal("55.00"), shipping_eur=Decimal("3.49")),
        assumed_shipping_eur=_BUFFER,
    )
    assert cost.fee_eur == Decimal("4.82")
    assert cost.shipping_eur == Decimal("3.49")
    assert cost.shipping_estimated is False
    assert cost.total_eur == Decimal("63.31")  # 55 + 3.49 + 4.82


def test_ebay_total_has_shipping_no_fee() -> None:
    cost = buyer_cost(
        _listing(marketplace="ebay", price_eur=Decimal("63.66"), shipping_eur=Decimal("16.82")),
        assumed_shipping_eur=_BUFFER,
    )
    assert cost.fee_eur == Decimal("0")
    assert cost.total_eur == Decimal("80.48")  # the screenshot case


def test_unknown_shipping_uses_buffer_and_flags_estimated() -> None:
    cost = buyer_cost(
        _listing(marketplace="ebay", price_eur=Decimal("70.00"), shipping_eur=None),
        assumed_shipping_eur=_BUFFER,
    )
    assert cost.shipping_eur == _BUFFER
    assert cost.shipping_estimated is True
    assert cost.total_eur == Decimal("73.50")
    # never treated as zero
    assert cost.total_eur > Decimal("70.00")


def test_free_shipping_is_not_estimated() -> None:
    cost = buyer_cost(
        _listing(marketplace="ebay", price_eur=Decimal("40.00"), shipping_eur=Decimal("0")),
        assumed_shipping_eur=_BUFFER,
    )
    assert cost.shipping_eur == Decimal("0")
    assert cost.shipping_estimated is False
    assert cost.total_eur == Decimal("40.00")


def test_buyer_total_eur_convenience_matches_cost() -> None:
    listing = _listing(shipping_eur=Decimal("3.49"))
    assert (
        buyer_total_eur(listing, assumed_shipping_eur=_BUFFER)
        == buyer_cost(listing, assumed_shipping_eur=_BUFFER).total_eur
    )
