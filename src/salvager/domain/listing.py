"""Marketplace listing + search-query schema — Story 3.1 / Story 3.2.

A ``Listing`` is the normalized shape every adapter (Wallapop unofficial-
API, Wallapop TinyFish, eBay official API) returns. Down-stream code —
the LLM evaluator, the alert renderer, the SQLite ``seen_listings``
dedup index — consumes ``Listing`` only; the per-marketplace payload
shapes never leak past the adapter boundary.

``SearchQuery`` is the dual: what the poll loop hands to a ``PageFetcher``
to ask "find me listings matching these keywords on this marketplace".
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Marketplace = Literal["wallapop", "ebay"]


class Listing(BaseModel):
    """Normalized marketplace listing — the boundary type between every
    adapter and the rest of the system.

    ``entry_key_match`` is the wishlist entry the LLM evaluator decided
    this listing matches; it is None until evaluation runs and is the
    join column for ``alert_snapshots`` and the SQLite dedup index.

    ``is_reserved`` distinguishes listings the operator can still buy
    (False) from those that have been marked unavailable by the seller
    (True). Reserved listings remain useful as price comps — they
    capture "what someone was willing to pay" — but they never trigger
    buy alerts, since the inventory is gone.
    """

    model_config = ConfigDict(extra="forbid")

    listing_id: str = Field(min_length=1)
    marketplace: Marketplace
    url: str = Field(min_length=1)
    title: str
    description: str
    price_eur: Decimal
    #: Carrier shipping cost the buyer pays, when the marketplace exposes it.
    #: ``None`` = unknown / not parsed (e.g. Wallapop in-person-only); ``0`` =
    #: free / included. The delivered buyer total (price + shipping + any
    #: marketplace fee) is computed by :func:`salvager.domain.pricing.buyer_total_eur`.
    #: Non-negative when present — a negative shipping cost would understate the
    #: buyer total and could let an over-ceiling listing through the gate.
    shipping_eur: Decimal | None = Field(default=None, ge=0)
    #: Item-location country (ISO 3166-1 alpha-2, uppercased), when the
    #: marketplace exposes it. ``None`` = unknown — treated as domestic/EU so
    #: no import-charges buffer is applied (ebay-import-charges-pricing).
    #: Adapters set it (eBay ``itemLocation.country``); Wallapop is a
    #: domestic marketplace and leaves it ``None``.
    country: str | None = None
    location: str | None = None
    photo_urls: list[str] = Field(default_factory=list)
    seller_id: str | None = None
    seller_history_count: int | None = None
    published_at: datetime | None = None
    fetched_at: datetime
    is_reserved: bool = False
    #: Wallapop marks refurbished listings in the search payload
    #: (``is_refurbished.flag``, live-probed 2026-07-22). Refurbished
    #: products don't accept offers, so the offer surface pre-filters on
    #: this instead of burning a tap on a guaranteed ``offer_unavailable``.
    #: ``False`` = not refurbished or marketplace doesn't expose it.
    is_refurbished: bool = False

    # Set by the LLM evaluator; None pre-evaluation.
    entry_key_match: tuple[str, str, str] | None = None


class SearchQuery(BaseModel):
    """One search request to a :class:`PageFetcher` adapter.

    Carries a single ``keyword`` phrase — the wishlist's list of
    alternative phrases is fanned out at the caller (``poll_loop``):
    one ``SearchQuery`` per phrase, with results unioned and de-duped
    by ``listing_id``. Adapters that joined a list of phrases into a
    single query string were silently AND-ing tokens at the
    marketplace, so e.g. ``["Ultrastar 14TB", "WUH721414"]`` searched
    for the literal concatenation and matched nothing.

    ``max_price_eur`` is a soft hint — adapters that can filter at the
    marketplace level (eBay) should pass it through; adapters that
    can't (Wallapop free-text) ignore it and rely on the LLM evaluator
    + the alert renderer's threshold.
    """

    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=1)
    marketplace: Marketplace
    max_price_eur: Decimal | None = None
