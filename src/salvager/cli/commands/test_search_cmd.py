"""``salvager test-search`` — Story 4.6 (FR43).

A dry-run search for tuning a wishlist entry: it runs the real
marketplace search but sends NO Telegram alert and writes NOTHING to
``seen_listings`` / ``alert_snapshots`` / any audit table. The only
side effect is the actual marketplace API call (and, with
``--evaluate``, the LLM eval cache — read-effective for state, so
still safe).

The positional argument is either a wishlist entry ``ref`` (resolved
to that entry's keywords + price ceiling) or an arbitrary free-text
query passed verbatim to both marketplaces.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from salvager.adapters.ebay_api.fetcher import EbayApiFetcher
from salvager.adapters.ebay_api.quota import DailyQuotaTracker
from salvager.adapters.ebay_api.tokens import OAuthTokenStore
from salvager.adapters.llm_cache_sqlite.cache import (
    DEFAULT_CACHE_FILENAME,
    CachingListingEvaluator,
    SqliteLlmEvalCache,
)
from salvager.adapters.wallapop_api.fetcher import WallapopApiFetcher
from salvager.config.config_yaml import ConfigModel
from salvager.config.env import EnvSettings
from salvager.domain.comps import summarize_comps
from salvager.domain.errors import LlmRateLimited, MarketplaceError, TinyFishRateLimited
from salvager.domain.listing import Listing, Marketplace, SearchQuery
from salvager.domain.prompts import PROMPT_VERSION
from salvager.domain.wishlist import Wishlist, WishlistEntry
from salvager.interfaces.listing_evaluator import ListingEvaluator
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.observability.styling import ColumnSpec, render_prose, render_table
from salvager.observability.styling import print_table as _print_table
from salvager.orchestration.composer import (
    EBAY_OAUTH_TOKENS_RELPATH,
    WALLAPOP_COOKIES_RELPATH,
    build_inner_evaluator,
)

_MARKETPLACES: tuple[Marketplace, ...] = ("wallapop", "ebay")


@dataclass
class _SearchResult:
    marketplace: str
    listing: Listing
    match_probability: float
    confidence: str | None = None
    one_line_take: str | None = None


@dataclass
class _Outcome:
    results: list[_SearchResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rate_limited: bool = False


def run(
    *,
    query_or_entry: str,
    env: EnvSettings,
    config: ConfigModel,
    wishlist: Wishlist,
    data_dir: Path,
    marketplace: str | None = None,
    evaluate: bool = False,
    output_format: str = "human",
    width: int = 80,
) -> int:
    """Run a dry-run marketplace search. Returns a CLI exit code."""
    if output_format not in ("human", "json"):
        render_prose(
            f"unknown --format value: {output_format!r}",
            style="error",
            hint="use --format human or --format json",
        )
        return 2
    if marketplace is not None and marketplace not in _MARKETPLACES:
        render_prose(
            f"unknown --marketplace value: {marketplace!r}",
            style="error",
            hint="use --marketplace wallapop or --marketplace ebay",
        )
        return 2

    entry = _resolve_entry(query_or_entry, wishlist)
    # ``marketplace`` is narrowed to a valid Marketplace literal by the
    # membership guard above.
    targets: tuple[Marketplace, ...] = (marketplace,) if marketplace is not None else _MARKETPLACES
    outcome = asyncio.run(
        _search_all(
            entry=entry,
            raw_query=query_or_entry,
            targets=targets,
            env=env,
            config=config,
            data_dir=data_dir,
            evaluate=evaluate,
        )
    )

    if marketplace is not None and not outcome.results and outcome.notes:
        # An explicitly-requested marketplace that could not even be
        # built (missing credentials) is a usage problem, not "0 hits".
        for note in outcome.notes:
            render_prose(note, style="error")
        return 4

    _render(outcome, evaluate=evaluate, output_format=output_format, width=width)
    if outcome.rate_limited:
        render_prose(
            "rate limit reached during dry-run — results are partial",
            style="warn",
        )
    return 0


# ─────────────────────────────────────────────────────────────────────────
# Entry resolution + query building
# ─────────────────────────────────────────────────────────────────────────


def _resolve_entry(query_or_entry: str, wishlist: Wishlist) -> WishlistEntry | None:
    """Return the wishlist entry whose ``ref`` matches, or None (arbitrary query)."""
    return next((e for e in wishlist.entries if e.ref == query_or_entry), None)


def _build_queries(
    entry: WishlistEntry | None,
    raw_query: str,
    marketplace: Marketplace,
) -> list[SearchQuery]:
    """Mirror ``poll_loop._build_search_queries`` — one query per keyword."""
    if entry is not None:
        keywords = list(entry.keywords) or [entry.model]
        max_price = entry.max_price_solo or entry.max_price_in_device
    else:
        keywords = [raw_query]
        max_price = None
    return [
        SearchQuery(keyword=kw, marketplace=marketplace, max_price_eur=max_price) for kw in keywords
    ]


def _heuristic_keywords(entry: WishlistEntry | None, raw_query: str) -> list[str]:
    if entry is not None:
        return [*entry.keywords, entry.ref, entry.model]
    return raw_query.split()


def _match_probability(listing: Listing, keywords: list[str]) -> float:
    """A cheap keyword-overlap score — NOT the LLM verdict.

    Fraction of the entry/query keywords that appear (case-insensitive)
    in the listing's title + description. Rounded to two decimals.
    """
    if not keywords:
        return 0.0
    haystack = f"{listing.title} {listing.description}".lower()
    hits = sum(1 for kw in keywords if kw and kw.lower() in haystack)
    return round(hits / len(keywords), 2)


# ─────────────────────────────────────────────────────────────────────────
# Search execution — no Telegram, no state writes
# ─────────────────────────────────────────────────────────────────────────


async def _search_all(
    *,
    entry: WishlistEntry | None,
    raw_query: str,
    targets: tuple[Marketplace, ...],
    env: EnvSettings,
    config: ConfigModel,
    data_dir: Path,
    evaluate: bool,
) -> _Outcome:
    outcome = _Outcome()
    keywords = _heuristic_keywords(entry, raw_query)

    evaluator: CachingListingEvaluator | None = None
    cache: SqliteLlmEvalCache | None = None
    if evaluate and entry is not None:
        cache = SqliteLlmEvalCache(data_dir / DEFAULT_CACHE_FILENAME)
        evaluator = CachingListingEvaluator(
            build_inner_evaluator(env, config), cache, PROMPT_VERSION
        )
    elif evaluate and entry is None:
        outcome.notes.append(
            "--evaluate needs a wishlist entry; an arbitrary query has no entry to match against"
        )

    try:
        for market in targets:
            fetcher = _build_fetcher(market, env=env, config=config, data_dir=data_dir)
            if fetcher is None:
                outcome.notes.append(
                    f"{market}: skipped — credentials not found under {data_dir / 'auth'}"
                )
                continue
            try:
                await _search_one(
                    market=market,
                    fetcher=fetcher,
                    entry=entry,
                    raw_query=raw_query,
                    keywords=keywords,
                    evaluator=evaluator,
                    outcome=outcome,
                )
            finally:
                await _close_fetcher(fetcher)
    finally:
        if cache is not None:
            await cache.close()

    return outcome


async def _search_one(
    *,
    market: Marketplace,
    fetcher: PageFetcher,
    entry: WishlistEntry | None,
    raw_query: str,
    keywords: list[str],
    evaluator: ListingEvaluator | None,
    outcome: _Outcome,
) -> None:
    queries = _build_queries(entry, raw_query, market)
    listings_by_id: dict[str, Listing] = {}
    keyword_failures = 0
    for query in queries:
        try:
            sub_listings = await fetcher.search(query)
        except (TinyFishRateLimited, LlmRateLimited):
            outcome.rate_limited = True
            outcome.notes.append(
                f"{market}: rate limited on keyword {query.keyword!r} — partial results"
            )
            keyword_failures += 1
            continue
        except MarketplaceError as exc:
            outcome.notes.append(
                f"{market}: search failed on keyword {query.keyword!r} ({exc.__class__.__name__})"
            )
            keyword_failures += 1
            continue
        for listing in sub_listings:
            listings_by_id.setdefault(listing.listing_id, listing)
    if keyword_failures == len(queries):
        return
    listings = list(listings_by_id.values())

    for listing in listings:
        result = _SearchResult(
            marketplace=market,
            listing=listing,
            match_probability=_match_probability(listing, keywords),
        )
        # Reserved listings are still rendered (they're useful comps for
        # the operator) but we skip the LLM evaluator on them — the
        # eval cost would buy nothing since the inventory is gone.
        if evaluator is not None and entry is not None and not listing.is_reserved:
            try:
                evaluation = await evaluator.evaluate(listing, entry)
                result.confidence = evaluation.confidence
                result.one_line_take = evaluation.one_line_take
            except LlmRateLimited:
                outcome.rate_limited = True
                outcome.notes.append("llm: rate limited — evaluation skipped for some results")
        outcome.results.append(result)


def _build_fetcher(
    market: Marketplace,
    *,
    env: EnvSettings,
    config: ConfigModel,
    data_dir: Path,
) -> PageFetcher | None:
    """Build the marketplace's PageFetcher, or None when creds are absent."""
    if market == "wallapop":
        cookies_path = data_dir / WALLAPOP_COOKIES_RELPATH
        if not cookies_path.exists():
            return None
        return WallapopApiFetcher(
            cookies_path=cookies_path,
            latitude=config.wallapop.latitude,
            longitude=config.wallapop.longitude,
        )
    # eBay
    tokens_path = data_dir / EBAY_OAUTH_TOKENS_RELPATH
    if not tokens_path.exists():
        return None
    return EbayApiFetcher(
        token_store=OAuthTokenStore(tokens_path),
        app_id=env.EBAY_APP_ID,
        cert_id=env.EBAY_CERT_ID,
        quota=DailyQuotaTracker(config.ebay.daily_request_quota),
    )


async def _close_fetcher(fetcher: PageFetcher) -> None:
    """Close whatever owned resources the fetcher holds (idempotent)."""
    closer = getattr(fetcher, "aclose", None) or getattr(fetcher, "close", None)
    if closer is not None:
        await closer()


# ─────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────


def _render(
    outcome: _Outcome,
    *,
    evaluate: bool,
    output_format: str,
    width: int,
) -> None:
    if output_format == "json":
        print(json.dumps([_result_to_json(r, evaluate=evaluate) for r in outcome.results]))
        # Notes go to stderr so stdout stays pure, jq-parseable JSON.
        for note in outcome.notes:
            render_prose(note, style="warn")
        return

    for note in outcome.notes:
        render_prose(note, style="secondary")

    if not outcome.results:
        render_prose("no listings found for this query", style="info")
        return

    columns: list[ColumnSpec] = [
        {"key": "marketplace", "header": "Marketplace"},
        {"key": "listing_id", "header": "Listing ID"},
        {"key": "title", "header": "Title"},
        {"key": "price", "header": "Price"},
        {"key": "reserved", "header": "Reserved"},
        {"key": "match_probability", "header": "Match Probability"},
    ]
    if evaluate:
        columns += [
            {"key": "confidence", "header": "Confidence"},
            {"key": "one_line_take", "header": "One-line Take"},
        ]
    rows: list[dict[str, object]] = [
        {
            "marketplace": r.marketplace,
            "listing_id": r.listing.listing_id,
            "title": r.listing.title,
            "price": _format_price(r.listing.price_eur),
            "reserved": "✓" if r.listing.is_reserved else "",
            "match_probability": f"{r.match_probability:.2f}",
            "confidence": r.confidence,
            "one_line_take": r.one_line_take,
        }
        for r in outcome.results
    ]
    table = render_table(rows, columns, width=width)
    _print_table(table, width=width)

    comp_note = _comp_summary_line(outcome.results)
    if comp_note is not None:
        render_prose(comp_note, style="secondary")

    if not evaluate:
        render_prose(
            "dry-run heuristic only — no LLM evaluation; pass --evaluate for confidence + take",
            style="secondary",
        )


def _comp_summary_line(results: list[_SearchResult]) -> str | None:
    """One-line summary of comp prices from reserved listings, or None.

    Sellers flag listings reserved when they're no longer for sale but
    still on the marketplace — those carry useful signal about the
    going rate. We surface min/median/max here so the operator can
    eyeball whether a buyable listing's price is reasonable without
    having to scan the table by hand.
    """
    # The count / min / median / max math (incl. the even-length-median
    # fix Devin caught on PR #7) lives in the shared domain builder so this
    # footer and the Telegram alert comp line cannot drift.
    summary = summarize_comps(r.listing.price_eur for r in results if r.listing.is_reserved)
    if summary is None:
        return None
    return (
        f"{summary.count} reserved listing(s) used as comps: "
        f"min {_format_price(summary.min_eur)}, "
        f"median {_format_price(summary.median_eur)}, "
        f"max {_format_price(summary.max_eur)}"
    )


def _result_to_json(result: _SearchResult, *, evaluate: bool) -> dict[str, Any]:
    payload: dict[str, Any] = result.listing.model_dump(mode="json")
    payload["match_probability"] = result.match_probability
    if evaluate:
        payload["confidence"] = result.confidence
        payload["one_line_take"] = result.one_line_take
    return payload


def _format_price(amount: Decimal) -> str:
    return f"{amount:.2f} EUR"


__all__ = ["run"]
