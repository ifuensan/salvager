"""Tests for the eBay official-API adapter — Story 3.7."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from salvager.adapters.ebay_api import (
    DailyQuotaTracker,
    EbayApiFetcher,
    OAuthTokens,
    OAuthTokenStore,
)
from salvager.adapters.ebay_api.fetcher import _item_to_listing
from salvager.adapters.ebay_api.schema import (
    EbayApiItem,
    EbayApiLocation,
    EbayApiPrice,
    EbayApiShippingOption,
)
from salvager.adapters.ebay_api.tokens import (
    OAUTH_TOKEN_FILE_MODE,
    parse_expires_in,
)
from salvager.domain.errors import (
    EbayApiError,
    EbayAuthFailed,
    EbayQuotaExceeded,
    EbaySchemaDrift,
)
from salvager.domain.listing import SearchQuery

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "ebay_api" / "browse_search_4tb_hdd.json"


# ─────────────────────────────────────────────────────────────────────────
# Token store — atomic write + mode 0600
# ─────────────────────────────────────────────────────────────────────────


def _fresh_tokens(*, expires_in_seconds: int = 3600) -> OAuthTokens:
    return OAuthTokens(
        access_token="access-abc",
        refresh_token="refresh-xyz",
        expires_at=parse_expires_in(expires_in_seconds),
        token_type="Bearer",
        scope="https://api.ebay.com/oauth/api_scope/buy.browse",
    )


def test_token_save_and_reload_round_trip(tmp_path: Path) -> None:
    store = OAuthTokenStore(tmp_path / "auth" / "oauth_tokens.json")
    original = _fresh_tokens()
    store.save(original)

    loaded = store.load()
    assert loaded.access_token == original.access_token
    assert loaded.refresh_token == original.refresh_token
    # ISO round-trip should be loss-less to the microsecond.
    assert abs((loaded.expires_at - original.expires_at).total_seconds()) < 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode check")
def test_token_save_sets_mode_0600(tmp_path: Path) -> None:
    store = OAuthTokenStore(tmp_path / "auth" / "oauth_tokens.json")
    store.save(_fresh_tokens())
    mode = store.path.stat().st_mode & 0o777
    assert mode == OAUTH_TOKEN_FILE_MODE


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode check")
def test_token_save_preserves_0600_after_overwriting_644_target(tmp_path: Path) -> None:
    """If a previous (sloppily-created) token file existed at 0644, the
    atomic save MUST end with 0600 — not inherit the prior mode."""
    target = tmp_path / "auth" / "oauth_tokens.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o644)

    store = OAuthTokenStore(target)
    store.save(_fresh_tokens())
    mode = target.stat().st_mode & 0o777
    assert mode == OAUTH_TOKEN_FILE_MODE


def test_token_save_leaves_no_temp_files_on_success(tmp_path: Path) -> None:
    auth_dir = tmp_path / "auth"
    store = OAuthTokenStore(auth_dir / "oauth_tokens.json")
    store.save(_fresh_tokens())
    leftovers = [p.name for p in auth_dir.iterdir() if p.name.startswith(".oauth_tokens.")]
    assert leftovers == []


def test_needs_refresh_within_lead_time() -> None:
    expiring_soon = OAuthTokens(
        access_token="a",
        refresh_token="r",
        expires_at=datetime.now(UTC) + timedelta(minutes=3),
    )
    assert expiring_soon.needs_refresh() is True

    expiring_later = OAuthTokens(
        access_token="a",
        refresh_token="r",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    assert expiring_later.needs_refresh() is False


# ─────────────────────────────────────────────────────────────────────────
# Daily-quota tracker
# ─────────────────────────────────────────────────────────────────────────


def test_quota_decrements_on_consume() -> None:
    tracker = DailyQuotaTracker(budget=3)
    assert tracker.remaining() == 3
    tracker.consume()
    tracker.consume()
    assert tracker.remaining() == 1
    assert tracker.can_consume() is True
    tracker.consume()
    assert tracker.can_consume() is False


def test_quota_resets_at_utc_midnight() -> None:
    tracker = DailyQuotaTracker(budget=2)
    day_one = datetime(2026, 5, 12, 23, 0, 0, tzinfo=UTC)
    tracker.consume(now=day_one)
    tracker.consume(now=day_one)
    assert tracker.can_consume(now=day_one) is False

    day_two = datetime(2026, 5, 13, 0, 1, 0, tzinfo=UTC)
    assert tracker.can_consume(now=day_two) is True
    assert tracker.remaining(now=day_two) == 2


# ─────────────────────────────────────────────────────────────────────────
# Fetcher harness
# ─────────────────────────────────────────────────────────────────────────


def _seed_tokens(tmp_path: Path, *, expires_in_seconds: int = 3600) -> OAuthTokenStore:
    store = OAuthTokenStore(tmp_path / "auth" / "oauth_tokens.json")
    store.save(_fresh_tokens(expires_in_seconds=expires_in_seconds))
    return store


def _build_fetcher(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    expires_in_seconds: int = 3600,
    quota_budget: int = 100,
    assumed_shipping_eur: Decimal | None = None,
) -> EbayApiFetcher:
    token_store = _seed_tokens(tmp_path, expires_in_seconds=expires_in_seconds)
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://api.ebay.com")
    kwargs: dict[str, Any] = {}
    if assumed_shipping_eur is not None:
        kwargs["assumed_shipping_eur"] = assumed_shipping_eur
    return EbayApiFetcher(
        token_store,
        SecretStr("APP-ID"),
        SecretStr("CERT-ID"),
        quota=DailyQuotaTracker(budget=quota_budget),
        client=client,
        **kwargs,
    )


# ─────────────────────────────────────────────────────────────────────────
# Happy path + fixture replay (AC: golden snapshot match)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_replays_fixture_into_two_listings(tmp_path: Path) -> None:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/buy/browse/v1/item_summary/search"
        assert request.headers.get("X-EBAY-C-MARKETPLACE-ID") == "EBAY_ES"
        return httpx.Response(200, json=payload)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        listings = await fetcher.search(SearchQuery(keyword="WD Red Plus 4TB", marketplace="ebay"))
    finally:
        await fetcher.aclose()

    assert len(listings) == 2
    first = listings[0]
    assert first.marketplace == "ebay"
    assert first.listing_id == "v1|254000000001|0"
    assert first.url == "https://www.ebay.es/itm/254000000001"
    assert first.title == "WD Red Plus 4TB WD40EFPX"
    assert first.price_eur == Decimal("65.00")
    assert first.location == "Barcelona"
    assert first.seller_id == "topdrives_es"
    assert first.seller_history_count == 4321
    assert first.published_at is not None

    second = listings[1]
    assert second.published_at is None  # itemCreationDate missing in fixture


@pytest.mark.asyncio
async def test_search_drops_listings_over_buyer_total_ceiling(tmp_path: Path) -> None:
    """The API ``price:[..max]`` filter is item-level; the fetcher additionally
    drops listings whose item + shipping exceeds the ceiling post-fetch
    (shipping-aware-pricing)."""
    payload = {
        "itemSummaries": [
            {
                "itemId": "v1|within|0",
                "title": "Cheap shipping",
                "price": {"value": "50.00", "currency": "EUR"},
                "shippingOptions": [{"shippingCost": {"value": "5.00", "currency": "EUR"}}],
            },
            {
                "itemId": "v1|over|0",
                "title": "Pricey shipping",
                "price": {"value": "58.00", "currency": "EUR"},
                "shippingOptions": [{"shippingCost": {"value": "10.00", "currency": "EUR"}}],
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        listings = await fetcher.search(
            SearchQuery(keyword="x", marketplace="ebay", max_price_eur=Decimal("60.00"))
        )
    finally:
        await fetcher.aclose()

    # 50 + 5 shipping = 55 ≤ 60 kept; 58 + 10 = 68 > 60 dropped even though its
    # item price (58 €) is under the ceiling.
    assert [listing.listing_id for listing in listings] == ["v1|within|0"]


@pytest.mark.asyncio
async def test_post_fetch_filter_honours_configured_shipping_buffer(tmp_path: Path) -> None:
    """For an item with unknown shipping, the post-fetch filter uses the
    configured buffer — not the hardcoded default — so it matches the Phase
    1/Phase 2 gates (shipping-aware-pricing)."""
    # Item 58 €, no priced shipping option → buffer applies. Ceiling 60 €.
    payload = {
        "itemSummaries": [
            {
                "itemId": "v1|unknownship|0",
                "title": "Unknown shipping",
                "price": {"value": "58.00", "currency": "EUR"},
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    query = SearchQuery(keyword="x", marketplace="ebay", max_price_eur=Decimal("60.00"))

    # Default buffer (3.50) → 58 + 3.50 = 61.50 > 60 → dropped.
    default_fetcher = _build_fetcher(tmp_path, handler)
    try:
        assert await default_fetcher.search(query) == []
    finally:
        await default_fetcher.aclose()

    # A lower configured buffer (1.00) → 58 + 1.00 = 59.00 ≤ 60 → kept.
    low_buffer_fetcher = _build_fetcher(tmp_path, handler, assumed_shipping_eur=Decimal("1.00"))
    try:
        kept = await low_buffer_fetcher.search(query)
    finally:
        await low_buffer_fetcher.aclose()
    assert [listing.listing_id for listing in kept] == ["v1|unknownship|0"]


@pytest.mark.asyncio
async def test_search_succeeded_log_carries_quota_remaining(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    fetcher = _build_fetcher(tmp_path, handler, quota_budget=5)
    try:
        await fetcher.search(SearchQuery(keyword="x", marketplace="ebay"))
    finally:
        await fetcher.aclose()

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    success = [r for r in records if r.get("event") == "ebay_search_succeeded"]
    assert len(success) == 1
    assert success[0]["marketplace"] == "ebay"
    assert success[0]["result_count"] == 2
    assert success[0]["daily_quota_remaining"] == 4  # 5 budget - 1 consumed


# ─────────────────────────────────────────────────────────────────────────
# Quota breach → EbayQuotaExceeded (NFR-I5)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quota_breach_raises_before_any_http_call(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"itemSummaries": []})

    fetcher = _build_fetcher(tmp_path, handler, quota_budget=1)
    try:
        # Burn the only allowed request.
        await fetcher.search(SearchQuery(keyword="x", marketplace="ebay"))
        # The next call should raise BEFORE hitting the transport.
        before = len(calls)
        with pytest.raises(EbayQuotaExceeded) as excinfo:
            await fetcher.search(SearchQuery(keyword="x", marketplace="ebay"))
        assert len(calls) == before  # no new HTTP request issued
        assert excinfo.value.used == 1
        assert excinfo.value.budget == 1
    finally:
        await fetcher.aclose()


# ─────────────────────────────────────────────────────────────────────────
# Token refresh — happy path + revoked refresh token
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_near_expiry_triggers_refresh(tmp_path: Path) -> None:
    """When the access token has < 5 min left, the fetcher refreshes
    BEFORE issuing the search."""
    refresh_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/identity/v1/oauth2/token":
            refresh_calls.append(
                {
                    "auth": request.headers.get("authorization"),
                    "body": request.content.decode("utf-8"),
                }
            )
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "refresh-xyz",
                    "expires_in": 7200,
                    "token_type": "Bearer",
                },
            )
        if request.url.path == "/buy/browse/v1/item_summary/search":
            assert request.headers.get("Authorization") == "Bearer new-access"
            return httpx.Response(200, json={"itemSummaries": []})
        return httpx.Response(404)

    # Token expires in 60 seconds — triggers refresh.
    fetcher = _build_fetcher(tmp_path, handler, expires_in_seconds=60)
    try:
        await fetcher.search(SearchQuery(keyword="x", marketplace="ebay"))
    finally:
        await fetcher.aclose()

    assert len(refresh_calls) == 1
    assert "refresh_token=refresh-xyz" in refresh_calls[0]["body"]
    # The new token is persisted to disk.
    reloaded = OAuthTokenStore(tmp_path / "auth" / "oauth_tokens.json").load()
    assert reloaded.access_token == "new-access"


@pytest.mark.asyncio
async def test_refresh_401_raises_ebay_auth_failed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/identity/v1/oauth2/token":
            return httpx.Response(401, json={"error": "invalid_grant"})
        return httpx.Response(404)

    fetcher = _build_fetcher(tmp_path, handler, expires_in_seconds=60)
    try:
        with pytest.raises(EbayAuthFailed):
            await fetcher.search(SearchQuery(keyword="x", marketplace="ebay"))
    finally:
        await fetcher.aclose()


# ─────────────────────────────────────────────────────────────────────────
# API + schema errors
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_500_raises_api_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream broken")

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        with pytest.raises(EbayApiError) as excinfo:
            await fetcher.search(SearchQuery(keyword="x", marketplace="ebay"))
    finally:
        await fetcher.aclose()
    assert excinfo.value.status_code == 500


@pytest.mark.asyncio
async def test_missing_required_field_raises_schema_drift(tmp_path: Path) -> None:
    bad_payload = {
        "total": 1,
        "itemSummaries": [
            {
                # itemId missing
                "title": "WD Red Plus 4TB",
                "price": {"value": "55.00", "currency": "EUR"},
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bad_payload)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        with pytest.raises(EbaySchemaDrift) as excinfo:
            await fetcher.search(SearchQuery(keyword="x", marketplace="ebay"))
    finally:
        await fetcher.aclose()
    assert "itemId" in excinfo.value.field_path


# ─────────────────────────────────────────────────────────────────────────
# NFR-S3 — no verify=False anywhere in the adapter
# ─────────────────────────────────────────────────────────────────────────


def test_fetcher_module_never_disables_tls_verification() -> None:
    import ast

    src = (REPO_ROOT / "src" / "salvager" / "adapters" / "ebay_api" / "fetcher.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                is_verify_false = (
                    kw.arg == "verify"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is False
                )
                if is_verify_false:
                    pytest.fail(f"fetcher.py passes verify=False at line {node.lineno} — NFR-S3")


def _ebay_item(**overrides: Any) -> EbayApiItem:
    base: dict[str, Any] = {
        "itemId": "v1|1|0",
        "title": "Corsair Vengeance LPX 16GB",
        "price": EbayApiPrice(value=Decimal("63.66"), currency="EUR"),
    }
    base.update(overrides)
    return EbayApiItem(**base)


def test_item_to_listing_parses_cheapest_shipping() -> None:
    item = _ebay_item(
        shippingOptions=[
            EbayApiShippingOption(
                shippingCost=EbayApiPrice(value=Decimal("19.99"), currency="EUR")
            ),
            EbayApiShippingOption(
                shippingCost=EbayApiPrice(value=Decimal("16.82"), currency="EUR")
            ),
            EbayApiShippingOption(shippingCost=None),  # e.g. local pickup
        ]
    )
    assert _item_to_listing(item).shipping_eur == Decimal("16.82")


def test_item_to_listing_shipping_none_when_no_priced_option() -> None:
    assert _item_to_listing(_ebay_item()).shipping_eur is None


def test_item_to_listing_projects_country_uppercased() -> None:
    item = _ebay_item(itemLocation=EbayApiLocation(city="Shenzhen", country="cn"))
    listing = _item_to_listing(item)
    assert listing.country == "CN"
    assert listing.location == "Shenzhen"


def test_item_to_listing_country_none_when_location_missing() -> None:
    assert _item_to_listing(_ebay_item()).country is None
    assert _item_to_listing(_ebay_item(itemLocation=EbayApiLocation(city="Madrid"))).country is None


@pytest.mark.asyncio
async def test_post_fetch_filter_applies_import_buffer_to_non_eu_items(tmp_path: Path) -> None:
    """Identical price/shipping, only the item-location country differs: the
    non-EU copy pays the import buffer and drops over the ceiling; the EU
    copy stays (ebay-import-charges-pricing)."""
    item = {
        "title": "Corsair",
        "price": {"value": "58.00", "currency": "EUR"},
        "shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "EUR"}}],
    }
    payload = {
        "itemSummaries": [
            {**item, "itemId": "v1|cn|0", "itemLocation": {"country": "CN"}},
            {**item, "itemId": "v1|es|0", "itemLocation": {"country": "ES"}},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    # Ceiling 60: EU copy totals 58.00; non-EU copy 58.00 + 3.63 = 61.63 → dropped.
    fetcher = _build_fetcher(tmp_path, handler)
    try:
        listings = await fetcher.search(
            SearchQuery(keyword="x", marketplace="ebay", max_price_eur=Decimal("60.00"))
        )
    finally:
        await fetcher.aclose()

    assert [listing.listing_id for listing in listings] == ["v1|es|0"]


@pytest.mark.asyncio
async def test_fetch_listing_uses_exact_browse_item_id(tmp_path: Path) -> None:
    """Reconciliation re-fetches by ``listing.listing_id`` (the exact
    ``v1|...`` Browse id, percent-encoded) — never by parsing the
    ``itemWebUrl`` tail, which is the legacy numeric id plus query noise."""
    payload = {
        "itemId": "v1|365659770742|635443416947",
        "title": "Corsair RAM",
        "price": {"value": "63.66", "currency": "EUR"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/buy/browse/v1/item/v1%7C365659770742%7C635443416947"
        return httpx.Response(200, json=payload)

    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from salvager.domain.listing import Listing

    known = Listing(
        listing_id="v1|365659770742|635443416947",
        marketplace="ebay",
        url="https://www.ebay.es/itm/365659770742?_skw=corsair&hash=item123",
        title="Corsair RAM",
        description="d",
        price_eur=Decimal("63.66"),
        fetched_at=_dt(2026, 7, 18, tzinfo=_UTC),
    )
    fetcher = _build_fetcher(tmp_path, handler)
    try:
        listing = await fetcher.fetch_listing(known)
    finally:
        await fetcher.aclose()
    assert listing.listing_id == "v1|365659770742|635443416947"
    assert listing.price_eur == Decimal("63.66")
