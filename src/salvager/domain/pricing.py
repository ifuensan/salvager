"""Buyer-paid total — shipping-aware price model (shipping-aware-pricing).

Price ceilings (Phase 1 alert gate, Phase 2 buy gate) and the alert display
must reflect what the buyer ACTUALLY pays, not the bare item price. On
Wallapop the buyer always pays, on top of the item:

  - carrier shipping (parsed when the API exposes it, else a configurable
    buffer for the unknown case), and
  - the mandatory **Protección Wallapop** fee.

eBay carries shipping (from the API) but no Protección fee. eBay also
charges a flat import fee on items shipped into Spain from outside the EU
(operator-observed 3,63 €/item, 2026-07-07); the Browse API search response
does not expose it, so a configurable flat buffer is added whenever the
listing's item-location country is known and outside the EU
(ebay-import-charges-pricing).

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

#: Default import-charges buffer (EUR) for a listing located outside the EU —
#: eBay's flat import fee as observed by the operator (3,63 €/item,
#: 2026-07-07; not exposed by the search API, so always estimated). Config
#: ``pricing.assumed_import_charges_eur`` overrides it in production.
DEFAULT_ASSUMED_IMPORT_CHARGES_EUR: Final[Decimal] = Decimal("3.63")

#: EU member states (ISO 3166-1 alpha-2), as of 2026-07 — the 27 post-Brexit
#: members. A listing located outside this set pays eBay's import charge; GB
#: is deliberately absent. Membership changes are rare enough to be a code
#: change, not config.
EU_COUNTRY_CODES: Final[frozenset[str]] = frozenset(
    {
        "AT",
        "BE",
        "BG",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GR",
        "HR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
    }
)

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
    import_charges_eur: Decimal  # non-EU import buffer (0 when not applied)
    import_estimated: bool  # True whenever non-zero — the value is never parsed
    total_eur: Decimal


def buyer_cost(
    listing: Listing,
    *,
    assumed_shipping_eur: Decimal,
    assumed_import_charges_eur: Decimal = DEFAULT_ASSUMED_IMPORT_CHARGES_EUR,
) -> BuyerCost:
    """Compute the buyer total for ``listing``.

    Shipping is the parsed ``listing.shipping_eur`` when known, else the
    configurable ``assumed_shipping_eur`` buffer (flagged ``estimated``) — it
    is never silently treated as zero. The Protección Wallapop fee is added
    for Wallapop listings only. When ``listing.country`` is known and outside
    :data:`EU_COUNTRY_CODES`, the ``assumed_import_charges_eur`` buffer is
    added (always flagged estimated — search payloads never carry the value);
    an unknown country adds nothing, so domestic/Wallapop listings are never
    taxed by mistake.
    """
    if listing.shipping_eur is not None:
        shipping = listing.shipping_eur
        estimated = False
    else:
        shipping = assumed_shipping_eur
        estimated = True

    fee = proteccion_wallapop_fee(listing.price_eur) if listing.marketplace == "wallapop" else _ZERO
    non_eu = listing.country is not None and listing.country not in EU_COUNTRY_CODES
    import_charges = assumed_import_charges_eur if non_eu else _ZERO
    total = _money(listing.price_eur + shipping + fee + import_charges)
    return BuyerCost(
        item_eur=listing.price_eur,
        shipping_eur=shipping,
        shipping_estimated=estimated,
        fee_eur=fee,
        import_charges_eur=import_charges,
        import_estimated=import_charges > 0,
        total_eur=total,
    )


# Wallapop's offer form rejects discounts deeper than 30 % of the asking
# price ("Tu oferta debe ser de al menos X € (-30%)", operator-captured
# 2026-07-22). The floor is inclusive: exactly 70 % of asking is accepted.
OFFER_PLATFORM_FLOOR_RATIO: Final[Decimal] = Decimal("0.70")

_ONE_EURO: Final[Decimal] = Decimal("1")


def offer_item_price_eur(
    listing: Listing,
    *,
    target_total_eur: Decimal,
    assumed_shipping_eur: Decimal,
) -> Decimal | None:
    """Largest whole-euro item price whose Wallapop buyer total fits the target.

    The returned price ``O`` satisfies ``O + shipping + proteccion(O) <=
    target_total_eur`` (shipping = the parsed value when known, else the
    ``assumed_shipping_eur`` buffer — never zero). Whole euros because that is
    how offers read in a negotiation, and flooring is always conservative.

    Returns ``None`` — offer not possible — when the listing is not Wallapop,
    when no positive whole-euro price fits the target, when the fit price is
    not strictly below the asking price (nothing to negotiate: the listing
    already fits, or the target is above asking), or when the fit price falls
    under the platform floor of :data:`OFFER_PLATFORM_FLOOR_RATIO` x asking
    (Wallapop rejects offers below 70 % of the asking price).
    """
    if listing.marketplace != "wallapop":
        return None
    shipping = listing.shipping_eur if listing.shipping_eur is not None else assumed_shipping_eur
    budget = target_total_eur - shipping
    # Closed-form guess on the variable-fee branch, then settle with the real
    # fee schedule (flat branch ≤ 13 €, cap at the documented maximum).
    if budget > 0:
        guess = int((budget - _PROTECCION_BASE_EUR) / (_ONE_EURO + _PROTECCION_PCT))
    else:
        guess = 0
    candidate = Decimal(max(guess, 0))
    while candidate + _ONE_EURO + proteccion_wallapop_fee(candidate + _ONE_EURO) <= budget:
        candidate += _ONE_EURO
    while candidate >= _ONE_EURO and candidate + proteccion_wallapop_fee(candidate) > budget:
        candidate -= _ONE_EURO
    if candidate < _ONE_EURO:
        return None
    if candidate >= listing.price_eur:
        return None
    if candidate < listing.price_eur * OFFER_PLATFORM_FLOOR_RATIO:
        return None
    return candidate


def buyer_total_eur(
    listing: Listing,
    *,
    assumed_shipping_eur: Decimal,
    assumed_import_charges_eur: Decimal = DEFAULT_ASSUMED_IMPORT_CHARGES_EUR,
) -> Decimal:
    """Convenience: the delivered total only (see :func:`buyer_cost`)."""
    return buyer_cost(
        listing,
        assumed_shipping_eur=assumed_shipping_eur,
        assumed_import_charges_eur=assumed_import_charges_eur,
    ).total_eur


__all__ = [
    "DEFAULT_ASSUMED_IMPORT_CHARGES_EUR",
    "DEFAULT_ASSUMED_SHIPPING_EUR",
    "EU_COUNTRY_CODES",
    "OFFER_PLATFORM_FLOOR_RATIO",
    "BuyerCost",
    "buyer_cost",
    "buyer_total_eur",
    "offer_item_price_eur",
    "proteccion_wallapop_fee",
]
