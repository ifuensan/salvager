"""``salvager explain <url>`` — Story 4.7 (FR44).

Fetches one listing, runs the full LLM evaluation against every
plausible wishlist entry, and prints the prompt + verdict +
would-be-alert text — so the operator can debug LLM behaviour without
enabling debug logging or re-running the daemon.

"Plausible" entries are found by keyword overlap against the listing's
title + description; ``--entry <ref>`` skips the heuristic and pins a
single entry.

Like the other Epic 4 read commands, this never writes audit state —
the only side effect is the marketplace fetch and (on a cache miss)
the LLM eval cache, which is read-effective for state purposes.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse
from uuid import uuid4

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
from salvager.domain.alert import AlertSnapshot, render_phase1_listing_alert
from salvager.domain.errors import MarketplaceError
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing, Marketplace
from salvager.domain.prompts import PROMPT_VERSION, build_evaluation_prompt
from salvager.domain.wishlist import Wishlist, WishlistEntry
from salvager.interfaces.listing_evaluator import ListingEvaluator
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.observability.styling import print_panel, render_prose
from salvager.orchestration.composer import (
    EBAY_OAUTH_TOKENS_RELPATH,
    WALLAPOP_COOKIES_RELPATH,
    build_inner_evaluator,
)

#: Confidence ordering — mirrors the poll loop's threshold gate.
_CONFIDENCE_RANK: Final[dict[str, int]] = {"low": 0, "medium": 1, "high": 2}

#: Exit code for a fetch failure (malformed URL, 404, adapter error).
_FETCH_FAILURE_EXIT = 3


@dataclass
class _EvalResult:
    entry: WishlistEntry
    prompt: str
    evaluation: ListingEvaluation
    from_cache: bool
    would_alert: bool
    reason_for_skip: str | None
    alert_text: str | None


def run(
    *,
    url: str,
    env: EnvSettings,
    config: ConfigModel,
    wishlist: Wishlist,
    data_dir: Path,
    entry_ref: str | None = None,
    output_format: str = "human",
    width: int = 80,
) -> int:
    """Explain one listing's evaluation. Returns a CLI exit code."""
    if output_format not in ("human", "json"):
        render_prose(
            f"unknown --format value: {output_format!r}",
            style="error",
            hint="use --format human or --format json",
        )
        return 2

    marketplace = _marketplace_for_url(url)
    if marketplace is None:
        render_prose(
            f"failed to fetch listing: unrecognized marketplace URL {url!r}",
            style="error",
            hint="check the URL — it must be an es.wallapop.com or ebay.es listing",
        )
        return _FETCH_FAILURE_EXIT

    return asyncio.run(
        _explain(
            url=url,
            marketplace=marketplace,
            env=env,
            config=config,
            wishlist=wishlist,
            data_dir=data_dir,
            entry_ref=entry_ref,
            output_format=output_format,
            width=width,
        )
    )


async def _explain(
    *,
    url: str,
    marketplace: Marketplace,
    env: EnvSettings,
    config: ConfigModel,
    wishlist: Wishlist,
    data_dir: Path,
    entry_ref: str | None,
    output_format: str,
    width: int,
) -> int:
    fetcher = _build_fetcher(marketplace, env=env, config=config, data_dir=data_dir)
    if fetcher is None:
        render_prose(
            f"failed to fetch listing: {marketplace} credentials not found",
            style="error",
            hint=f"run `salvager login {marketplace}` first",
        )
        return _FETCH_FAILURE_EXIT

    try:
        listing = await fetcher.fetch(url)
    except MarketplaceError as exc:
        render_prose(
            f"failed to fetch listing: {exc}",
            style="error",
            hint="check the URL",
        )
        return _FETCH_FAILURE_EXIT
    finally:
        await _close_fetcher(fetcher)

    entries = _plausible_entries(listing, wishlist, entry_ref=entry_ref)
    if not entries:
        render_prose(
            "no wishlist entries plausibly match this listing",
            style="info",
            hint="pass --entry <ref> to force evaluation against a specific entry",
        )
        return 0

    cache = SqliteLlmEvalCache(data_dir / DEFAULT_CACHE_FILENAME)
    evaluator: ListingEvaluator = CachingListingEvaluator(
        build_inner_evaluator(env, config), cache, PROMPT_VERSION
    )
    try:
        results = [await _evaluate(listing, entry, evaluator) for entry in entries]
    finally:
        await cache.close()

    if output_format == "json":
        print(json.dumps(_to_json(listing, results)))
    else:
        _render_human(listing, results, width=width)
    return 0


async def _evaluate(
    listing: Listing,
    entry: WishlistEntry,
    evaluator: ListingEvaluator,
) -> _EvalResult:
    evaluation = await evaluator.evaluate(listing, entry)
    prompt = build_evaluation_prompt(listing, entry)
    threshold = entry.confidence_threshold
    would_alert = _CONFIDENCE_RANK[evaluation.confidence] >= _CONFIDENCE_RANK[threshold]

    alert_text: str | None = None
    reason_for_skip: str | None = None
    if would_alert:
        snapshot = AlertSnapshot(
            alert_id=uuid4(),
            entry_key=entry.entry_key,
            entry_display_name=entry.display_name,
            listing=listing,
            evaluation=evaluation,
            phase="phase1",
            rendered_at=datetime.now(UTC),
        )
        alert_text = render_phase1_listing_alert(snapshot).text
    else:
        reason_for_skip = (
            f"confidence {evaluation.confidence!r} is below the entry threshold {threshold!r}"
        )

    return _EvalResult(
        entry=entry,
        prompt=prompt,
        evaluation=evaluation,
        from_cache=evaluation.cache_hit,
        would_alert=would_alert,
        reason_for_skip=reason_for_skip,
        alert_text=alert_text,
    )


# ─────────────────────────────────────────────────────────────────────────
# URL → marketplace + fetcher construction
# ─────────────────────────────────────────────────────────────────────────


def _marketplace_for_url(url: str) -> Marketplace | None:
    try:
        host = (urlparse(url).netloc or "").lower()
    except ValueError:
        return None
    if "wallapop.com" in host:
        return "wallapop"
    if "ebay." in host:
        return "ebay"
    return None


def _build_fetcher(
    marketplace: Marketplace,
    *,
    env: EnvSettings,
    config: ConfigModel,
    data_dir: Path,
) -> PageFetcher | None:
    """Build the marketplace's PageFetcher, or None when creds are absent."""
    if marketplace == "wallapop":
        cookies_path = data_dir / WALLAPOP_COOKIES_RELPATH
        if not cookies_path.exists():
            return None
        return WallapopApiFetcher(
            cookies_path=cookies_path,
            latitude=config.wallapop.latitude,
            longitude=config.wallapop.longitude,
        )
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
    closer = getattr(fetcher, "aclose", None) or getattr(fetcher, "close", None)
    if closer is not None:
        await closer()


# ─────────────────────────────────────────────────────────────────────────
# Plausible-entry heuristic
# ─────────────────────────────────────────────────────────────────────────


def _plausible_entries(
    listing: Listing,
    wishlist: Wishlist,
    *,
    entry_ref: str | None,
) -> list[WishlistEntry]:
    """Wishlist entries worth evaluating against ``listing``.

    With ``--entry``, the heuristic is bypassed: just that entry (if it
    exists). Otherwise an entry is plausible when any of its keywords,
    its model words, or its ref appears in the listing text.
    """
    if entry_ref is not None:
        return [e for e in wishlist.entries if e.ref == entry_ref]

    haystack = f"{listing.title} {listing.description}".lower()
    plausible: list[WishlistEntry] = []
    for entry in wishlist.entries:
        terms = [*entry.keywords, entry.ref, *entry.model.split()]
        if any(term and term.lower() in haystack for term in terms):
            plausible.append(entry)
    return plausible


# ─────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────


def _render_human(listing: Listing, results: list[_EvalResult], *, width: int) -> None:
    render_prose(
        f"Listing: {listing.title} — {listing.price_eur} EUR ({listing.marketplace})",
        style="info",
    )
    for result in results:
        cache_note = " (from cache)" if result.from_cache else ""
        verdict = "WOULD ALERT" if result.would_alert else f"skipped — {result.reason_for_skip}"
        body_lines = [
            f"Entry: {result.entry.display_name}",
            f"Verdict: {verdict}{cache_note}",
            f"Confidence: {result.evaluation.confidence}",
            f"One-line take: {result.evaluation.one_line_take}",
            "",
            "── Prompt ──",
            result.prompt,
            "",
            "── Evaluation (parsed response) ──",
            result.evaluation.model_dump_json(indent=2),
        ]
        if result.alert_text is not None:
            body_lines += ["", "── Would-be alert ──", result.alert_text]
        print_panel(
            "\n".join(body_lines),
            title=result.entry.display_name,
            width=width,
        )


def _to_json(listing: Listing, results: list[_EvalResult]) -> dict[str, Any]:
    return {
        "listing": listing.model_dump(mode="json"),
        "evaluations": [
            {
                "entry_key": list(result.entry.entry_key),
                "prompt": result.prompt,
                "response": result.evaluation.model_dump(mode="json"),
                "from_cache": result.from_cache,
                "would_alert": result.would_alert,
                "reason_for_skip": result.reason_for_skip,
                "would_be_alert_text": result.alert_text,
            }
            for result in results
        ],
    }


__all__ = ["run"]
