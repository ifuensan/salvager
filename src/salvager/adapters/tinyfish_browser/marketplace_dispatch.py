"""Marketplace-dispatching wrappers — composer-only glue.

The :class:`BuyOrchestrator` (orchestration/buy_orchestrator.py) takes one
:class:`BrowserSession` and one :class:`PageFetcher`. The deployment has
two of each, one per marketplace. The wrappers in this module own both
inner adapters and route per call based on the listing's marketplace
field (for the browser, a domain object always carries it) or the URL
host (for ``fetch``, where the listing object isn't reachable).

The dispatch lives in the composition layer rather than inside the
orchestrator so the orchestrator's "single browser, single fetcher"
abstraction stays clean and the per-marketplace adapters stay free of
cross-marketplace knowledge.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlparse

from salvager.domain.listing import Listing, SearchQuery
from salvager.interfaces.browser_session import BrowserSession, BuyResult
from salvager.interfaces.page_fetcher import PageFetcher

# Apex hosts used to route ``fetch(listing_url)`` calls when the only
# information we have is the URL. Listings from the primary search path
# always populate ``listing.marketplace``; the URL fallback is for code
# paths that pass a bare URL (e.g. the reconciler's pre-buy refetch).
# Matching is exact or proper-suffix on the parsed hostname so that
# ``https://attacker.example/?next=ebay.com`` does not dispatch to eBay.
_WALLAPOP_HOSTS = ("wallapop.com",)
_EBAY_HOSTS = ("ebay.es", "ebay.com")


def _host_matches(host: str, allowed: tuple[str, ...]) -> bool:
    return any(host == apex or host.endswith(f".{apex}") for apex in allowed)


class MarketplaceDispatchingBrowser(BrowserSession):
    """Route ``execute_buy`` to the per-marketplace flow.

    The dispatcher does not perform any retry, rate-limit, or fail-over
    logic — those concerns live inside the per-marketplace adapters.
    Its only job is to pick the right adapter and forward the call.
    """

    def __init__(self, *, wallapop: BrowserSession, ebay: BrowserSession) -> None:
        self._wallapop = wallapop
        self._ebay = ebay

    async def execute_buy(self, listing: Listing, max_price_eur: Decimal) -> BuyResult:
        if listing.marketplace == "wallapop":
            return await self._wallapop.execute_buy(listing, max_price_eur)
        if listing.marketplace == "ebay":
            return await self._ebay.execute_buy(listing, max_price_eur)
        raise ValueError(
            f"unknown listing.marketplace {listing.marketplace!r}; expected 'wallapop' or 'ebay'"
        )


class MarketplaceDispatchingPageFetcher(PageFetcher):
    """Route ``search``/``fetch`` to the per-marketplace fetcher.

    ``search`` keys off ``query.marketplace`` (set by the caller). ``fetch``
    keys off the URL host because the reconciler's pre-buy refetch passes
    only the listing URL — there's no listing or query object in scope.
    """

    def __init__(self, *, wallapop: PageFetcher, ebay: PageFetcher) -> None:
        self._wallapop = wallapop
        self._ebay = ebay

    async def search(self, query: SearchQuery) -> list[Listing]:
        if query.marketplace == "wallapop":
            return await self._wallapop.search(query)
        if query.marketplace == "ebay":
            return await self._ebay.search(query)
        raise ValueError(
            f"unknown query.marketplace {query.marketplace!r}; expected 'wallapop' or 'ebay'"
        )

    async def fetch(self, listing_url: str) -> Listing:
        host = (urlparse(listing_url).hostname or "").lower()
        if _host_matches(host, _WALLAPOP_HOSTS):
            return await self._wallapop.fetch(listing_url)
        if _host_matches(host, _EBAY_HOSTS):
            return await self._ebay.fetch(listing_url)
        raise ValueError(
            f"cannot route fetch({listing_url!r}) — host does not match "
            "any known marketplace (wallapop.com, ebay.es, ebay.com)"
        )

    async def aclose(self) -> None:
        """Close both inner fetchers that own OS resources.

        The :class:`PageFetcher` port doesn't declare ``aclose`` — only
        the concrete adapters that hold an ``httpx`` client do (the eBay
        reconciliation fetcher's client is the live case) — so we close
        defensively, skipping inners that don't expose the method.
        Idempotent: adapters guard double-close internally.
        """
        for inner in (self._wallapop, self._ebay):
            aclose = getattr(inner, "aclose", None)
            if aclose is not None:
                await aclose()


__all__ = [
    "MarketplaceDispatchingBrowser",
    "MarketplaceDispatchingPageFetcher",
]
