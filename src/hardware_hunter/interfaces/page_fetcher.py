"""``PageFetcher`` ABC — Story 3.2.

The port through which the poll loop talks to *any* marketplace. Two
adapters implement it at v1:

  - ``adapters/wallapop_api`` — unofficial-API path (primary)
  - ``adapters/wallapop_tinyfish`` — TinyFish-via-Hermes fallback
  - ``adapters/ebay_api`` — eBay official API

The orchestration layer composes ``PageFetcher`` only — it never sees a
Wallapop or eBay SDK directly. This is how NFR-M1 (adapter discipline)
holds at runtime.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from hardware_hunter.domain.listing import Listing, SearchQuery


class PageFetcher(ABC):
    """Port for one marketplace's listing-read operations."""

    @abstractmethod
    async def search(self, query: SearchQuery) -> list[Listing]:
        """Return all listings currently matching ``query``.

        Empty result set is valid (the operator's wishlist may simply
        have no matches today). Adapter-specific errors surface as
        :class:`PageFetcherError` (declared below).
        """

    @abstractmethod
    async def fetch(self, listing_url: str) -> Listing:
        """Fetch a single listing by URL — used by ``explain <url>`` and
        the Phase 2 pre-buy reconciliation."""


class PageFetcherError(RuntimeError):
    """An adapter failed to complete a fetch operation.

    Concrete adapters wrap their marketplace-specific exceptions in
    this type so the orchestration layer has a single error class to
    catch — the actual cause lives in ``__cause__``.
    """
