"""Tests for the marketplace-dispatching wrappers — composer glue layer.

The wrappers exist so the `BuyOrchestrator` can hold one `BrowserSession`
and one `PageFetcher` while the deployment runs two of each. The tests
confirm dispatch routes by `listing.marketplace` (browser + search) and
by URL host (fetch), and that unknown marketplaces raise a clear error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from salvager.adapters.tinyfish_browser.marketplace_dispatch import (
    MarketplaceDispatchingBrowser,
    MarketplaceDispatchingPageFetcher,
)
from salvager.domain.errors import BuyFailureReason
from salvager.domain.listing import Listing, Marketplace, SearchQuery
from salvager.interfaces.browser_session import BrowserSession, BuyFailure, BuyResult
from salvager.interfaces.page_fetcher import PageFetcher

_T0 = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


def _listing(marketplace: Marketplace, listing_id: str = "abc") -> Listing:
    host = "wallapop.com" if marketplace == "wallapop" else "ebay.es"
    return Listing(
        listing_id=listing_id,
        marketplace=marketplace,
        url=f"https://{host}/item/{listing_id}",
        title="t",
        description="d",
        price_eur=Decimal("10"),
        fetched_at=_T0,
    )


def _failure(detail: str) -> BuyFailure:
    return BuyFailure(reason=BuyFailureReason.marketplace_error, ctx={"detail": detail})


# ─────────────────────────────────────────────────────────────────────────
# MarketplaceDispatchingBrowser
# ─────────────────────────────────────────────────────────────────────────


class _RecordingBrowser(BrowserSession):
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.calls: list[tuple[Listing, Decimal]] = []

    async def execute_buy(self, listing: Listing, max_price_eur: Decimal) -> BuyResult:
        self.calls.append((listing, max_price_eur))
        return _failure(f"called-{self.tag}")


async def test_browser_dispatches_wallapop_listing_to_wallapop_flow() -> None:
    wallapop = _RecordingBrowser("wallapop")
    ebay = _RecordingBrowser("ebay")
    dispatcher = MarketplaceDispatchingBrowser(wallapop=wallapop, ebay=ebay)

    result = await dispatcher.execute_buy(_listing("wallapop"), Decimal("50"))

    assert len(wallapop.calls) == 1
    assert len(ebay.calls) == 0
    assert isinstance(result, BuyFailure)
    assert result.ctx == {"detail": "called-wallapop"}


async def test_browser_dispatches_ebay_listing_to_ebay_flow() -> None:
    wallapop = _RecordingBrowser("wallapop")
    ebay = _RecordingBrowser("ebay")
    dispatcher = MarketplaceDispatchingBrowser(wallapop=wallapop, ebay=ebay)

    await dispatcher.execute_buy(_listing("ebay"), Decimal("40"))

    assert len(wallapop.calls) == 0
    assert len(ebay.calls) == 1


async def test_browser_unknown_marketplace_raises_value_error() -> None:
    wallapop = _RecordingBrowser("wallapop")
    ebay = _RecordingBrowser("ebay")
    dispatcher = MarketplaceDispatchingBrowser(wallapop=wallapop, ebay=ebay)

    bad = _listing("wallapop")
    # Force an invalid value past pydantic by mutating after construction.
    object.__setattr__(bad, "marketplace", "amazon")

    with pytest.raises(ValueError, match=r"unknown listing\.marketplace 'amazon'"):
        await dispatcher.execute_buy(bad, Decimal("10"))


# ─────────────────────────────────────────────────────────────────────────
# MarketplaceDispatchingPageFetcher
# ─────────────────────────────────────────────────────────────────────────


class _RecordingFetcher(PageFetcher):
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.searches: list[SearchQuery] = []
        self.fetches: list[str] = []

    async def search(self, query: SearchQuery) -> list[Listing]:
        self.searches.append(query)
        return []

    async def fetch(self, listing_url: str) -> Listing:
        self.fetches.append(listing_url)
        return _listing("wallapop" if self.tag == "wallapop" else "ebay")


async def test_fetcher_search_dispatches_by_marketplace_query() -> None:
    wallapop = _RecordingFetcher("wallapop")
    ebay = _RecordingFetcher("ebay")
    dispatcher = MarketplaceDispatchingPageFetcher(wallapop=wallapop, ebay=ebay)

    await dispatcher.search(SearchQuery(keyword="kw", marketplace="wallapop"))
    await dispatcher.search(SearchQuery(keyword="kw", marketplace="ebay"))

    assert len(wallapop.searches) == 1
    assert len(ebay.searches) == 1


async def test_fetcher_fetch_dispatches_by_url_host() -> None:
    wallapop = _RecordingFetcher("wallapop")
    ebay = _RecordingFetcher("ebay")
    dispatcher = MarketplaceDispatchingPageFetcher(wallapop=wallapop, ebay=ebay)

    await dispatcher.fetch("https://wallapop.com/item/abc")
    await dispatcher.fetch("https://es.wallapop.com/item/xyz")
    await dispatcher.fetch("https://www.ebay.es/itm/123")
    await dispatcher.fetch("https://www.ebay.com/itm/456")

    assert wallapop.fetches == [
        "https://wallapop.com/item/abc",
        "https://es.wallapop.com/item/xyz",
    ]
    assert ebay.fetches == [
        "https://www.ebay.es/itm/123",
        "https://www.ebay.com/itm/456",
    ]


async def test_fetcher_fetch_unknown_host_raises_value_error() -> None:
    wallapop = _RecordingFetcher("wallapop")
    ebay = _RecordingFetcher("ebay")
    dispatcher = MarketplaceDispatchingPageFetcher(wallapop=wallapop, ebay=ebay)

    with pytest.raises(ValueError, match="cannot route fetch"):
        await dispatcher.fetch("https://amazon.com/dp/B0XXX")


async def test_fetcher_fetch_marketplace_string_in_path_does_not_dispatch() -> None:
    """Routing keys off the parsed hostname, not substring match — so a
    URL that merely *mentions* a marketplace domain in its path or query
    string is rejected. Defence-in-depth against a future caller passing
    an externally-sourced URL into the reconciliation path."""
    wallapop = _RecordingFetcher("wallapop")
    ebay = _RecordingFetcher("ebay")
    dispatcher = MarketplaceDispatchingPageFetcher(wallapop=wallapop, ebay=ebay)

    with pytest.raises(ValueError, match="cannot route fetch"):
        await dispatcher.fetch("https://attacker.example/?next=ebay.com/itm/1")
    with pytest.raises(ValueError, match="cannot route fetch"):
        await dispatcher.fetch("https://notwallapop.com/item/123")
    assert wallapop.fetches == []
    assert ebay.fetches == []


async def test_fetcher_search_unknown_marketplace_raises_value_error() -> None:
    wallapop = _RecordingFetcher("wallapop")
    ebay = _RecordingFetcher("ebay")
    dispatcher = MarketplaceDispatchingPageFetcher(wallapop=wallapop, ebay=ebay)

    query = SearchQuery(keyword="kw", marketplace="wallapop")
    object.__setattr__(query, "marketplace", "amazon")

    with pytest.raises(ValueError, match=r"unknown query\.marketplace 'amazon'"):
        await dispatcher.search(query)


# Silence unused-import warning when type-checkers don't see Any usage.
_ = Any
