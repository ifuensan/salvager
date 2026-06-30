"""eBay.es Browse-API :class:`PageFetcher` — Story 3.7.

OAuth flow: tokens live at ``data_dir/auth/oauth_tokens.json``. On each
call, the fetcher checks whether the access token is within 5 minutes
of expiry (:func:`OAuthTokens.needs_refresh`) and, if so, hits the
refresh endpoint, writes the new tokens atomically (mode 0600), and
proceeds. A 401 from the refresh endpoint surfaces as
:class:`EbayAuthFailed` — the daemon stops polling eBay until the
operator re-runs ``salvager login ebay``.

Daily-quota gate: a :class:`DailyQuotaTracker` is consulted before
each call. A would-be breach raises :class:`EbayQuotaExceeded`; the
poll loop (Story 3.14) reacts by halving the eBay cadence until the
next UTC-midnight reset (FR8 / NFR-I5).

TLS: ``verify=True`` always (NFR-S3). An AST test enforces no
``verify=False`` codepath ever lands.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from salvager.adapters.ebay_api.quota import DailyQuotaTracker
from salvager.adapters.ebay_api.schema import (
    EbayApiItem,
    EbayApiSearchResponse,
)
from salvager.adapters.ebay_api.tokens import (
    OAuthTokens,
    OAuthTokenStore,
    parse_expires_in,
)
from salvager.domain.errors import (
    EbayApiError,
    EbayAuthFailed,
    EbayQuotaExceeded,
    EbaySchemaDrift,
)
from salvager.domain.listing import Listing, SearchQuery
from salvager.domain.pricing import DEFAULT_ASSUMED_SHIPPING_EUR, buyer_total_eur
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.observability.logging import get_logger

_DEFAULT_BASE_URL = "https://api.ebay.com"
_SEARCH_PATH = "/buy/browse/v1/item_summary/search"
_REFRESH_PATH = "/identity/v1/oauth2/token"
_DEFAULT_TIMEOUT = httpx.Timeout(10.0)
_DEFAULT_MARKETPLACE_HEADER = "EBAY_ES"


class EbayApiFetcher(PageFetcher):
    """``PageFetcher`` backed by eBay's official Browse API."""

    def __init__(
        self,
        token_store: OAuthTokenStore,
        app_id: SecretStr,
        cert_id: SecretStr,
        *,
        quota: DailyQuotaTracker,
        assumed_shipping_eur: Decimal = DEFAULT_ASSUMED_SHIPPING_EUR,
        base_url: str = _DEFAULT_BASE_URL,
        marketplace_header: str = _DEFAULT_MARKETPLACE_HEADER,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
    ) -> None:
        self._token_store = token_store
        self._tokens = token_store.load()
        self._app_id = app_id
        self._cert_id = cert_id
        self._quota = quota
        # Buffer for the post-fetch buyer-total filter when an item exposes no
        # shipping. Composer threads ``config.pricing.assumed_shipping_eur`` so
        # this matches the Phase 1/Phase 2 gates; defaults to the documented
        # buffer for CLI/test construction (shipping-aware-pricing).
        self._assumed_shipping_eur = assumed_shipping_eur
        self._marketplace_header = marketplace_header
        self._owned_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                timeout=timeout,
                verify=True,
                base_url=base_url.rstrip("/"),
            )
        self._client = client
        self._log = get_logger("adapter.ebay_api")

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    # ─────────────────────────────────────────────────────────────────
    # PageFetcher
    # ─────────────────────────────────────────────────────────────────

    async def search(self, query: SearchQuery) -> list[Listing]:
        self._gate_or_raise()
        await self._maybe_refresh()

        params: dict[str, Any] = {
            "q": query.keyword,
            "limit": "100",
        }
        if query.max_price_eur is not None:
            params["filter"] = f"price:[..{query.max_price_eur}],priceCurrency:EUR"

        started = time.perf_counter()
        response = await self._client.get(
            _SEARCH_PATH,
            params=params,
            headers=self._auth_headers(),
        )
        self._quota.consume()
        self._raise_for_status(response)

        try:
            payload = EbayApiSearchResponse.model_validate(response.json())
        except ValidationError as exc:
            drift = _from_validation_error(exc)
            self._log.error(
                "ebay_schema_drift",
                extra={
                    "error_class": "EbaySchemaDrift",
                    "marketplace": "ebay",
                    "field_path": drift.field_path,
                },
            )
            raise drift from exc

        listings = [_item_to_listing(item) for item in payload.itemSummaries]

        # The API `price:[..max]` filter is item-level. Enforce the ceiling on
        # the delivered buyer total (item + shipping) post-fetch so shipping
        # can't push a listing over the ceiling unnoticed (shipping-aware-
        # pricing). eBay has no Protección fee; when an item exposes no shipping
        # price the domain buffer applies. The poll loop re-gates with the
        # configured buffer — this is the quota-side first cut.
        result_count = len(listings)
        if query.max_price_eur is not None:
            ceiling = query.max_price_eur
            listings = [
                listing
                for listing in listings
                if buyer_total_eur(listing, assumed_shipping_eur=self._assumed_shipping_eur)
                <= ceiling
            ]

        self._log.info(
            "ebay_search_succeeded",
            extra={
                "marketplace": "ebay",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "result_count": result_count,
                "kept_within_buyer_total": len(listings),
                "daily_quota_remaining": self._quota.remaining(),
            },
        )
        return listings

    async def fetch(self, listing_url: str) -> Listing:
        self._gate_or_raise()
        await self._maybe_refresh()

        item_id = listing_url.rstrip("/").rsplit("/", 1)[-1]
        response = await self._client.get(
            f"/buy/browse/v1/item/{item_id}",
            headers=self._auth_headers(),
        )
        self._quota.consume()
        self._raise_for_status(response)

        try:
            item = EbayApiItem.model_validate(response.json())
        except ValidationError as exc:
            raise _from_validation_error(exc) from exc
        return _item_to_listing(item)

    # ─────────────────────────────────────────────────────────────────
    # Internals — OAuth + quota gating
    # ─────────────────────────────────────────────────────────────────

    def _gate_or_raise(self) -> None:
        if not self._quota.can_consume():
            self._log.warning(
                "ebay_quota_breach",
                extra={
                    "marketplace": "ebay",
                    "used": self._quota.used,
                    "budget": self._quota.budget,
                },
            )
            raise EbayQuotaExceeded(self._quota.used, self._quota.budget)

    async def _maybe_refresh(self) -> None:
        if not self._tokens.needs_refresh():
            return
        self._tokens = await self._refresh_tokens()
        self._token_store.save(self._tokens)
        self._log.info(
            "ebay_token_refreshed",
            extra={
                "marketplace": "ebay",
                "expires_at": self._tokens.expires_at.isoformat(),
            },
        )

    async def _refresh_tokens(self) -> OAuthTokens:
        auth = httpx.BasicAuth(
            self._app_id.get_secret_value(),
            self._cert_id.get_secret_value(),
        )
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._tokens.refresh_token,
        }
        try:
            response = await self._client.post(
                _REFRESH_PATH,
                data=data,
                auth=auth,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise EbayApiError(0, str(exc)) from exc

        if response.status_code == 401:
            self._log.error(
                "ebay_token_refresh_failed",
                extra={"marketplace": "ebay", "status_code": 401},
            )
            raise EbayAuthFailed("eBay refresh token rejected — run `salvager login ebay`")
        if response.status_code >= 400:
            body = response.text[:200] if response.text else None
            raise EbayApiError(response.status_code, body)

        payload = response.json()
        return OAuthTokens(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", self._tokens.refresh_token),
            expires_at=parse_expires_in(int(payload["expires_in"])),
            token_type=payload.get("token_type", "Bearer"),
            scope=payload.get("scope"),
        )

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tokens.access_token}",
            "X-EBAY-C-MARKETPLACE-ID": self._marketplace_header,
        }

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code == 401:
            # An access-token 401 mid-search means a refresh is overdue; the
            # next call will pick it up automatically (we re-check expiry).
            self._log.warning(
                "ebay_access_token_rejected",
                extra={"marketplace": "ebay", "status_code": 401},
            )
            raise EbayApiError(401, response.text[:200] if response.text else None)
        if response.status_code >= 400:
            body = response.text[:200] if response.text else None
            self._log.error(
                "ebay_api_error",
                extra={
                    "marketplace": "ebay",
                    "status_code": response.status_code,
                    "error_class": "EbayApiError",
                },
            )
            raise EbayApiError(response.status_code, body)


def _cheapest_shipping_eur(item: EbayApiItem) -> Decimal | None:
    """Cheapest priced shipping option (EUR), or None when none is priced.

    eBay returns one or more ``shippingOptions``; some carry no
    ``shippingCost`` (e.g. local pickup). We take the minimum priced one so
    the buyer-total ceiling reflects the cheapest delivery the buyer can pick.
    """
    costs = [
        Decimal(str(opt.shippingCost.value))
        for opt in item.shippingOptions
        if opt.shippingCost is not None
    ]
    return min(costs) if costs else None


def _item_to_listing(item: EbayApiItem) -> Listing:
    """Project an upstream ``EbayApiItem`` onto the domain shape."""
    photo_urls: list[str] = []
    if item.image and item.image.imageUrl:
        photo_urls.append(item.image.imageUrl)

    return Listing(
        listing_id=item.itemId,
        marketplace="ebay",
        url=item.itemWebUrl or f"https://www.ebay.es/itm/{item.itemId}",
        title=item.title,
        description=item.shortDescription,
        price_eur=Decimal(str(item.price.value)),
        shipping_eur=_cheapest_shipping_eur(item),
        location=item.itemLocation.city if item.itemLocation else None,
        photo_urls=photo_urls,
        seller_id=item.seller.username if item.seller else None,
        seller_history_count=item.seller.feedbackScore if item.seller else None,
        published_at=_parse_iso(item.itemCreationDate),
        fetched_at=datetime.now(UTC),
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _from_validation_error(exc: ValidationError) -> EbaySchemaDrift:
    first = exc.errors()[0]
    path = ".".join(str(p) for p in first["loc"])
    return EbaySchemaDrift(
        field_path=f"itemSummaries.{path}" if path else "<root>",
        detail=first["msg"],
    )
