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
    location: str | None = None
    photo_urls: list[str] = Field(default_factory=list)
    seller_id: str | None = None
    seller_history_count: int | None = None
    published_at: datetime | None = None
    fetched_at: datetime
    is_reserved: bool = False

    # Set by the LLM evaluator; None pre-evaluation.
    entry_key_match: tuple[str, str, str] | None = None


class SearchQuery(BaseModel):
    """One search request to a :class:`PageFetcher` adapter.

    The poll loop constructs one per (wishlist entry x marketplace)
    using the entry's ``keywords``. ``max_price_eur`` is a soft hint —
    adapters that can filter at the marketplace level (eBay) should
    pass it through; adapters that can't (Wallapop free-text) ignore
    it and rely on the LLM evaluator + the alert renderer's threshold.
    """

    model_config = ConfigDict(extra="forbid")

    keywords: list[str] = Field(min_length=1)
    marketplace: Marketplace
    max_price_eur: Decimal | None = None
