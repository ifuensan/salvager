"""Buyer-paid total — shipping-aware price model (shipping-aware-pricing).

Price ceilings (Phase 1 alert gate, Phase 2 buy gate) and the alert display
must reflect what the buyer ACTUALLY pays, not the bare item price. On
Wallapop the buyer always pays, on top of the item:

  - carrier shipping (parsed when the API exposes it, else a configurable
    buffer for the unknown case), and
  - the mandatory **Protección Wallapop** fee.

eBay carries shipping (from the API) but no Protección fee.

Protección Wallapop fee (operator-sourced 2026-06-16; calculator at
https://gualacost.com/ — approximate, Wallapop can change it):

  - item price ≤ 13 €        → fixed 1,69 €
  - item price > 13 €        → 0,69 € + 7,5 % of the item price,
                               clamped to a ~50 € maximum.

Pure decimal arithmetic, zero IO.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from salvager.domain.listing import Listing

_CENT: Final[Decimal] = Decimal("0.01")

#: Default shipping buffer (EUR) when a listing's shipping isn't exposed by the
#: marketplace — light ≤2 kg standard within Spain. Config
#: ``pricing.assumed_shipping_eur`` overrides it in production; this is the
#: fallback for code paths without config (e.g. Phase 1-only / tests).
DEFAULT_ASSUMED_SHIPPING_EUR: Final[Decimal] = Decimal("3.50")

# Protección Wallapop fee schedule. Constants in one place; see module
# docstring for the source + date. A unit test pins the boundaries so a wrong
# edit fails loudly.
_PROTECCION_FLAT_THRESHOLD_EUR: Final[Decimal] = Decimal("13")
_PROTECCION_FLAT_FEE_EUR: Final[Decimal] = Decimal("1.69")
_PROTECCION_BASE_EUR: Final[Decimal] = Decimal("0.69")
_PROTECCION_PCT: Final[Decimal] = Decimal("0.075")
_PROTECCION_CAP_EUR: Final[Decimal] = Decimal("50")

_ZERO: Final[Decimal] = Decimal("0")


def _money(value: Decimal) -> Decimal:
    """Quantize to cents (half-up) — fees/totals are charged to the cent."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def proteccion_wallapop_fee(price_eur: Decimal) -> Decimal:
    """Mandatory Wallapop buyer fee for an item priced ``price_eur``."""
    if price_eur <= _PROTECCION_FLAT_THRESHOLD_EUR:
        return _PROTECCION_FLAT_FEE_EUR
    fee = _PROTECCION_BASE_EUR + price_eur * _PROTECCION_PCT
    return _money(min(fee, _PROTECCION_CAP_EUR))


@dataclass(frozen=True)
class BuyerCost:
    """The delivered cost the buyer pays, broken down for gates + display."""

    item_eur: Decimal
    shipping_eur: Decimal  # value used: parsed cost, or the buffer when estimated
    shipping_estimated: bool  # True when the configurable buffer was applied
    fee_eur: Decimal  # Protección Wallapop (0 on eBay)
    total_eur: Decimal


def buyer_cost(listing: Listing, *, assumed_shipping_eur: Decimal) -> BuyerCost:
    """Compute the buyer total for ``listing``.

    Shipping is the parsed ``listing.shipping_eur`` when known, else the
    configurable ``assumed_shipping_eur`` buffer (flagged ``estimated``) — it
    is never silently treated as zero. The Protección Wallapop fee is added
    for Wallapop listings only.
    """
    if listing.shipping_eur is not None:
        shipping = listing.shipping_eur
        estimated = False
    else:
        shipping = assumed_shipping_eur
        estimated = True

    fee = proteccion_wallapop_fee(listing.price_eur) if listing.marketplace == "wallapop" else _ZERO
    total = _money(listing.price_eur + shipping + fee)
    return BuyerCost(
        item_eur=listing.price_eur,
        shipping_eur=shipping,
        shipping_estimated=estimated,
        fee_eur=fee,
        total_eur=total,
    )


def buyer_total_eur(listing: Listing, *, assumed_shipping_eur: Decimal) -> Decimal:
    """Convenience: the delivered total only (see :func:`buyer_cost`)."""
    return buyer_cost(listing, assumed_shipping_eur=assumed_shipping_eur).total_eur


__all__ = [
    "DEFAULT_ASSUMED_SHIPPING_EUR",
    "BuyerCost",
    "buyer_cost",
    "buyer_total_eur",
    "proteccion_wallapop_fee",
]
