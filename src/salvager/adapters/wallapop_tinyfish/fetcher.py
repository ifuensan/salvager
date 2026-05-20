"""Wallapop TinyFish :class:`PageFetcher` — Story 3.5.

Browser-as-a-service fallback for the Wallapop search path. When the
unofficial-API adapter (Story 3.4) fails — session expiry, anti-bot
challenge, transient 5xx — the two-path orchestrator (Story 3.6) calls
this fetcher to drive a real browser via TinyFish against
``https://es.wallapop.com/app/search``.

Why a separate adapter
----------------------
TinyFish is paid + rate-limited; we don't make it the primary path.
The unofficial-API path is free + fast; we only burn TinyFish calls
when the cheap path can't deliver. Two adapters, one interface,
orchestration picks the path.

Goal template
-------------
The goal hardcoded in :data:`SEARCH_GOAL_TEMPLATE` asks the TinyFish
browser agent to extract listings into a JSON object matching
:class:`TinyfishListingsResult`. The schema is part of the prompt, so
the agent is steered toward the exact shape we parse. A failure to
match (extra fields, missing fields, wrong types) raises
:class:`WallapopSchemaDrift` rather than a generic exception — the
operator sees ``schema_drift_field_path`` in the log and can patch the
template or the parser without guessing.

Rate limiting
-------------
TinyFish's documented Search-API cap is 5 req/min. We enforce that
client-side with a sliding-window counter
(:class:`SlidingWindowRateLimiter`) — NFR-I2 "never trust remote alone"
— and raise :class:`TinyFishRateLimited` if a call would breach it,
without hitting the network. The Story 3.6 orchestrator catches and
defers the path to the next poll cycle.

Test seam
---------
The TinyFish client is constructor-injected so unit tests pass a fake
that records calls and returns preloaded :class:`AgentRunResponse`
instances. Production calls leave ``client=None`` and we construct an
:class:`AsyncTinyFish` with the operator's API key.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import quote_plus

from pydantic import SecretStr, ValidationError
from tinyfish import (
    AsyncTinyFish,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
    RunStatus,
    SDKError,
)

from salvager.adapters.wallapop_tinyfish.rate_limit import (
    SlidingWindowRateLimiter,
)
from salvager.adapters.wallapop_tinyfish.schema import (
    TinyfishListingItem,
    TinyfishListingsResult,
)
from salvager.domain.errors import (
    TinyFishAuthFailed,
    TinyFishRateLimited,
    TinyFishUnavailable,
    WallapopSchemaDrift,
)
from salvager.domain.listing import Listing, SearchQuery
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.observability.logging import get_logger

#: TinyFish Search-API cap per their docs.
_DEFAULT_RATE_LIMIT_PER_MINUTE: Final[int] = 5

#: Max listings to ask the agent to extract per call.
_DEFAULT_RESULTS_LIMIT: Final[int] = 30

#: Per-run wall-clock budget the daemon hands TinyFish.
_DEFAULT_MAX_DURATION_S: Final[int] = 120

_SEARCH_BASE_URL: Final[str] = "https://es.wallapop.com/app/search"

#: Search goal — hardcoded at v0.x. Operator-tunable is post-launch
#: scope; locking it here keeps the parser's contract trustworthy.
SEARCH_GOAL_TEMPLATE: Final[str] = (
    "Open the Wallapop search results page. Wait for the listings grid to load "
    "fully (scroll once if needed to trigger lazy load). Extract the first {limit} "
    "listings shown.\n"
    "\n"
    "For each listing, capture EXACTLY these fields:\n"
    "- listing_id: the slug at the end of the listing URL path (after /item/).\n"
    "- url: the absolute URL to the listing detail page.\n"
    "- title: the title text on the card, unmodified.\n"
    "- price_eur: the numeric price in euros as a string with dot decimal "
    '  separator (e.g. "55.00"); strip the currency symbol.\n'
    "- location: the location label on the card, or null when absent.\n"
    "- description: the short description on the card, or null when absent.\n"
    "- photo_urls: array of image URLs visible on the card (typically the first "
    "  image).\n"
    "\n"
    "Return STRICTLY this JSON shape — no prose, no markdown fences:\n"
    "{{\n"
    '  "listings": [\n'
    "    {{ ...one object per listing matching the fields above... }}\n"
    "  ]\n"
    "}}\n"
)


class WallapopTinyfishFetcher(PageFetcher):
    """``PageFetcher`` backed by TinyFish browser automation."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        client: AsyncTinyFish | None = None,
        results_limit: int = _DEFAULT_RESULTS_LIMIT,
        max_duration_s: int = _DEFAULT_MAX_DURATION_S,
        rate_limit_per_minute: int = _DEFAULT_RATE_LIMIT_PER_MINUTE,
        rate_limiter: SlidingWindowRateLimiter | None = None,
    ) -> None:
        """Build the fetcher.

        ``client`` is dependency-injected for unit tests. Production
        leaves it None and we construct an :class:`AsyncTinyFish` from
        the (unmasked) API key. The unmask happens here and nowhere
        else — every downstream call uses the constructed client.
        """
        self._results_limit = results_limit
        self._max_duration_s = max_duration_s
        self._owned_client = client is None
        if client is None:
            client = AsyncTinyFish(api_key=api_key.get_secret_value())
        self._client = client
        self._rate_limiter = rate_limiter or _default_rate_limiter(rate_limit_per_minute)
        self._log = get_logger("adapter.wallapop_tinyfish")

    async def close(self) -> None:
        """Close the underlying TinyFish client. Idempotent."""
        if self._owned_client:
            await self._client.close()

    # ─────────────────────────────────────────────────────────────────
    # PageFetcher — search / fetch
    # ─────────────────────────────────────────────────────────────────

    async def search(self, query: SearchQuery) -> list[Listing]:
        """Run a TinyFish search; return parsed :class:`Listing` items."""
        self._enforce_rate_limit()

        wallapop_url = _build_search_url(query)
        goal = SEARCH_GOAL_TEMPLATE.format(limit=self._results_limit)
        started = time.perf_counter()

        try:
            response = await self._client.agent.run(goal=goal, url=wallapop_url)
        except AuthenticationError as exc:
            self._log.error(
                "wallapop_tinyfish_auth_failed",
                extra={"error_class": exc.__class__.__name__},
            )
            raise TinyFishAuthFailed("TinyFish rejected the API key (401)") from exc
        except RateLimitError as exc:
            retry_after = _retry_after_seconds(exc)
            self._log.warning(
                "wallapop_tinyfish_rate_limited",
                extra={"retry_after_s": retry_after},
            )
            raise TinyFishRateLimited(retry_after_s=retry_after) from exc
        except PermissionDeniedError as exc:
            # 403 — typically credits exhausted; surface as auth-side
            # for the operator-action flow (replace key or top up).
            self._log.error(
                "wallapop_tinyfish_auth_failed",
                extra={
                    "error_class": exc.__class__.__name__,
                    "reason": "permission_denied",
                },
            )
            raise TinyFishAuthFailed("TinyFish refused the call (403 — credits or scope)") from exc
        except SDKError as exc:
            self._log.error(
                "wallapop_tinyfish_unavailable",
                extra={"error_class": exc.__class__.__name__},
            )
            raise TinyFishUnavailable(
                f"TinyFish call failed: {exc.__class__.__name__}: {exc}"
            ) from exc
        finally:
            # Record the call even on failure — a series of failing calls
            # still counts toward the rate budget at the remote.
            self._rate_limiter.record()

        if response.status != RunStatus.COMPLETED:
            self._log.error(
                "wallapop_tinyfish_run_failed",
                extra={
                    "status": str(response.status),
                    "run_id": response.run_id,
                    "error": _render_run_error(response.error),
                },
            )
            raise TinyFishUnavailable(
                f"TinyFish run {response.run_id} finished with status "
                f"{response.status}: {_render_run_error(response.error)}"
            )

        listings = _parse_listings(response.result, query)
        latency_ms = int((time.perf_counter() - started) * 1000)
        self._log.info(
            "wallapop_tinyfish_search_succeeded",
            extra={
                "latency_ms": latency_ms,
                "result_count": len(listings),
                "marketplace": "wallapop",
                "run_id": response.run_id,
            },
        )
        return listings

    async def fetch(self, listing_url: str) -> Listing:
        """Story 3.5 ships ``search``; ``fetch`` lands when Phase 2's
        pre-buy reconciliation needs single-listing detail (Epic 5)."""
        _ = listing_url
        raise NotImplementedError(
            "Single-listing fetch via TinyFish lands with Phase 2 (Epic 5). "
            "Use the unofficial-API path's fetch() for now."
        )

    # ─────────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────────

    def _enforce_rate_limit(self) -> None:
        if self._rate_limiter.allow():
            return
        retry_after = self._rate_limiter.retry_after_seconds()
        self._log.warning(
            "wallapop_tinyfish_rate_limit_local",
            extra={"retry_after_s": retry_after},
        )
        raise TinyFishRateLimited(retry_after_s=retry_after)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _default_rate_limiter(per_minute: int) -> SlidingWindowRateLimiter:
    return SlidingWindowRateLimiter(
        limit=per_minute,
        window=timedelta(minutes=1),
    )


def _build_search_url(query: SearchQuery) -> str:
    """Construct the Wallapop SPA URL the TinyFish agent navigates to."""
    parts = [f"keywords={quote_plus(query.keyword)}"]
    if query.max_price_eur is not None:
        parts.append(f"max_sale_price={query.max_price_eur}")
    return f"{_SEARCH_BASE_URL}?{'&'.join(parts)}"


def _parse_listings(
    result: dict[str, Any] | None,
    query: SearchQuery,
) -> list[Listing]:
    """Translate the TinyFish ``result`` envelope into domain :class:`Listing`.

    Raises :class:`WallapopSchemaDrift` when the shape doesn't match
    :class:`TinyfishListingsResult`. The drift exception carries the
    pydantic-reported field path so the operational log identifies
    which selector / prompt clause needs patching.
    """
    if result is None:
        raise WallapopSchemaDrift("result", "TinyFish returned an empty result envelope")

    try:
        parsed = TinyfishListingsResult.model_validate(result)
    except ValidationError as exc:
        first = exc.errors()[0]
        path = ".".join(str(p) for p in first.get("loc", ()))
        raise WallapopSchemaDrift(
            field_path=path or "<root>",
            detail=first.get("msg") or str(exc),
        ) from exc

    fetched_at = datetime.now(UTC)
    return [
        _to_domain_listing(item, query=query, fetched_at=fetched_at) for item in parsed.listings
    ]


def _to_domain_listing(
    item: TinyfishListingItem,
    *,
    query: SearchQuery,
    fetched_at: datetime,
) -> Listing:
    return Listing(
        listing_id=item.listing_id,
        marketplace=query.marketplace,
        url=item.url,
        title=item.title,
        description=item.description or "",
        price_eur=item.price_eur,
        location=item.location,
        photo_urls=list(item.photo_urls),
        fetched_at=fetched_at,
    )


def _retry_after_seconds(exc: RateLimitError) -> float | None:
    """Pull ``retry_after`` (or equivalent) out of a TinyFish RateLimitError.

    The SDK exposes the parsed error envelope in different fields
    depending on version; we probe a handful of likely names and fall
    back to None when we can't find it (the caller treats None as
    "unknown — pick a sensible default like 60s upstream").
    """
    for attr in ("retry_after", "retry_after_seconds", "retry_after_s"):
        value = getattr(exc, attr, None)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    body = getattr(exc, "body", None) or getattr(exc, "response_body", None)
    if isinstance(body, dict):
        candidate = body.get("retry_after") or body.get("error", {}).get("retry_after")
        if isinstance(candidate, int | float):
            return float(candidate)
    return None


def _render_run_error(error: Any) -> str:
    """Render a :class:`RunError` (or None) as a single log-friendly string."""
    if error is None:
        return "<no detail>"
    try:
        return json.dumps(error.model_dump(), default=str)
    except (AttributeError, TypeError):
        return str(error)


__all__ = [
    "SEARCH_GOAL_TEMPLATE",
    "WallapopTinyfishFetcher",
]
