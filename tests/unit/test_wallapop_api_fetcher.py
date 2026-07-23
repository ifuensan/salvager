"""Tests for the Wallapop unofficial-API adapter — Story 3.4."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from salvager.adapters.wallapop_api import WallapopApiFetcher, load_cookies
from salvager.adapters.wallapop_api.cookies import WallapopCookiesError
from salvager.adapters.wallapop_api.fetcher import WallapopResponse
from salvager.domain.errors import (
    WallapopApiError,
    WallapopSchemaDrift,
    WallapopSessionExpired,
)
from salvager.domain.listing import Listing, SearchQuery

# ─────────────────────────────────────────────────────────────────────────
# Cookies helper
# ─────────────────────────────────────────────────────────────────────────


def _valid_cookies_file(tmp_path: Path) -> Path:
    path = tmp_path / "wallapop_cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".wallapop.com\tTRUE\t/\tTRUE\t9999999999\tsession\tsecret-session\n"
        "#HttpOnly_.wallapop.com\tTRUE\t/\tTRUE\t9999999999\tcsrf\tcsrf-value\n",
        encoding="utf-8",
    )
    return path


def test_load_cookies_returns_httpx_cookies(tmp_path: Path) -> None:
    cookies = load_cookies(_valid_cookies_file(tmp_path))
    # SDK stores cookies in a private jar; assert via API rather than introspection.
    assert cookies.get("session", domain=".wallapop.com") == "secret-session"
    assert cookies.get("csrf", domain=".wallapop.com") == "csrf-value"


def test_load_cookies_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(WallapopCookiesError, match="not found"):
        load_cookies(tmp_path / "missing.txt")


def test_load_cookies_malformed_line_raises(tmp_path: Path) -> None:
    path = tmp_path / "wallapop_cookies.txt"
    path.write_text(".wallapop.com\tonly two fields\n", encoding="utf-8")
    with pytest.raises(WallapopCookiesError, match="expected 7 tab-separated"):
        load_cookies(path)


def test_load_cookies_skips_blank_lines_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "wallapop_cookies.txt"
    path.write_text(
        "# comment\n\n.wallapop.com\tTRUE\t/\tTRUE\t9999999999\tsession\ts1\n",
        encoding="utf-8",
    )
    cookies = load_cookies(path)
    assert cookies.get("session", domain=".wallapop.com") == "s1"


# ─────────────────────────────────────────────────────────────────────────
# Fixtures for the fetcher tests
# ─────────────────────────────────────────────────────────────────────────


def _valid_search_payload() -> dict[str, Any]:
    """Sample matching ``GET /api/v3/search/section`` shape.

    Trimmed from a real capture (2026-05-18) — the live response also
    carries ``taxonomy``, ``shipping``, ``reserved``, ``bump`` etc.,
    but the schema's ``extra='ignore'`` discards them and tests stay
    focused on the fields the projection actually consumes.
    """
    return {
        "data": {
            "section": {
                "type": "organic_search_results",
                "items": [
                    {
                        "id": "abc123",
                        "user_id": "u-42",
                        "title": "WD Red Plus 4TB",
                        "description": "Como nuevo, en caja.",
                        "price": {"amount": "55.00", "currency": "EUR"},
                        "location": {"city": "Madrid", "country_code": "ES"},
                        "images": [
                            {
                                "urls": {
                                    "small": "https://cdn.wallapop.com/abc123-W320.jpg",
                                    "medium": "https://cdn.wallapop.com/abc123-W640.jpg",
                                    "big": "https://cdn.wallapop.com/abc123-W800.jpg",
                                }
                            }
                        ],
                        "web_slug": "wd-red-plus-4tb-abc123",
                        "created_at": 1779047758198,  # unix millis
                    },
                    {
                        "id": "def456",
                        "user_id": "u-99",
                        "title": "Ultrastar 14TB",
                        "price": {"amount": "120.00", "currency": "EUR"},
                        # description omitted (defaults to "")
                        # location absent
                        "images": [],
                        # web_slug omitted — mapper falls back to id
                    },
                ],
            }
        },
        "meta": {"next_page": None},
    }


@dataclass(slots=True)
class _RecordedRequest:
    """What ``_build_fetcher``'s handler captured per call."""

    path: str
    params: dict[str, str]


def _build_fetcher(
    tmp_path: Path,
    handler: Callable[[_RecordedRequest], WallapopResponse],
    *,
    recorded: list[_RecordedRequest] | None = None,
) -> WallapopApiFetcher:
    """Build a fetcher with a synchronous handler at the
    :data:`WallapopRequestCallable` seam.

    The seam was changed from ``httpx.MockTransport`` to a plain
    callable when the adapter migrated to ``curl_cffi`` (curl_cffi
    has no MockTransport equivalent; cookies/headers/TLS would be
    untestable through it anyway). Tests now operate purely on the
    ``WallapopResponse`` value the fetcher consumes.
    """

    async def _request(path: str, params: dict[str, str]) -> WallapopResponse:
        record = _RecordedRequest(path=path, params=dict(params))
        if recorded is not None:
            recorded.append(record)
        return handler(record)

    cookies_path = _valid_cookies_file(tmp_path)
    return WallapopApiFetcher(cookies_path, request=_request)


# ─────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_returns_domain_listings(tmp_path: Path) -> None:
    def handler(request: _RecordedRequest) -> WallapopResponse:
        assert request.path == "/api/v3/search/section"
        # The SPA sends a leading space in `keywords` for the
        # search_box source path; the adapter mirrors that.
        assert request.params["keywords"] == " WD Red Plus 4TB"
        assert request.params["source"] == "search_box"
        assert request.params["section_type"] == "organic_search_results"
        # Coords default to Madrid centre when not configured.
        assert request.params["latitude"] == "40.4168"
        assert request.params["longitude"] == "-3.7038"
        # search_id is a UUID per call — just assert presence.
        assert request.params.get("search_id")
        payload = _valid_search_payload()
        return WallapopResponse(status_code=200, text=json.dumps(payload), json_data=payload)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        listings = await fetcher.search(
            SearchQuery(keyword="WD Red Plus 4TB", marketplace="wallapop")
        )
    finally:
        await fetcher.aclose()

    assert len(listings) == 2

    first = listings[0]
    assert first.marketplace == "wallapop"
    assert first.listing_id == "abc123"
    # URL builds from web_slug, not from raw id.
    assert first.url == "https://es.wallapop.com/item/wd-red-plus-4tb-abc123"
    assert first.title == "WD Red Plus 4TB"
    assert first.price_eur == Decimal("55.00")
    # The search API doesn't expose a fixed shipping cost → None (unknown), so
    # the buyer total falls back to the configurable buffer (shipping-aware-
    # pricing). Never silently 0.
    assert first.shipping_eur is None
    assert first.location == "Madrid"
    # Prefers `big` size from images[].urls; falls back to medium/small.
    assert first.photo_urls == ["https://cdn.wallapop.com/abc123-W800.jpg"]
    assert first.seller_id == "u-42"
    # v3 endpoint omits seller history count; populated via separate
    # /api/v3/users/{id} call by callers that need it (today: None).
    assert first.seller_history_count is None
    # created_at unix millis → datetime.
    assert first.published_at is not None

    second = listings[1]
    # Missing web_slug → fallback to id.
    assert second.url == "https://es.wallapop.com/item/def456"
    assert second.location is None
    assert second.photo_urls == []
    assert second.description == ""
    # No created_at → published_at None.
    assert second.published_at is None
    # No `reserved` envelope on either item → both default to not-reserved.
    assert first.is_reserved is False
    assert second.is_reserved is False


@pytest.mark.asyncio
async def test_search_maps_reserved_flag_to_is_reserved(tmp_path: Path) -> None:
    """``reserved: {flag: true}`` on the upstream item must surface as
    ``Listing.is_reserved=True``; ``flag: false`` and missing envelope
    both surface as False. The orchestrator relies on this for the
    "skip eval + alert on reserved" routing.
    """

    def handler(_: _RecordedRequest) -> WallapopResponse:
        payload = _valid_search_payload()
        items = payload["data"]["section"]["items"]
        items[0]["reserved"] = {"flag": True}
        items[1]["reserved"] = {"flag": False}
        return WallapopResponse(status_code=200, text=json.dumps(payload), json_data=payload)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        listings = await fetcher.search(
            SearchQuery(keyword="WD Red Plus 4TB", marketplace="wallapop")
        )
    finally:
        await fetcher.aclose()

    assert listings[0].is_reserved is True
    assert listings[1].is_reserved is False


@pytest.mark.asyncio
async def test_search_maps_refurbished_flag_to_is_refurbished(tmp_path: Path) -> None:
    """``is_refurbished: {flag: true}`` must surface as
    ``Listing.is_refurbished=True``; ``flag: false`` and a missing
    envelope both surface as False. The offer surface pre-filters on
    this — refurbished listings don't accept offers (wallapop-offer-flow,
    live-probed 2026-07-22).
    """

    def handler(_: _RecordedRequest) -> WallapopResponse:
        payload = _valid_search_payload()
        items = payload["data"]["section"]["items"]
        items[0]["is_refurbished"] = {"flag": True}
        items[1]["is_refurbished"] = {"flag": False}
        return WallapopResponse(status_code=200, text=json.dumps(payload), json_data=payload)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        listings = await fetcher.search(
            SearchQuery(keyword="WD Red Plus 4TB", marketplace="wallapop")
        )
    finally:
        await fetcher.aclose()

    assert listings[0].is_refurbished is True
    assert listings[1].is_refurbished is False


# ─────────────────────────────────────────────────────────────────────────
# Error mapping (NFR-I4)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_401_raises_session_expired(tmp_path: Path) -> None:
    def handler(request: _RecordedRequest) -> WallapopResponse:
        return WallapopResponse(status_code=401, text='{"error":"unauthorized"}', json_data=None)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        with pytest.raises(WallapopSessionExpired):
            await fetcher.search(SearchQuery(keyword="x", marketplace="wallapop"))
    finally:
        await fetcher.aclose()


@pytest.mark.asyncio
async def test_http_500_raises_api_error_with_status_and_body(tmp_path: Path) -> None:
    def handler(request: _RecordedRequest) -> WallapopResponse:
        return WallapopResponse(status_code=500, text="upstream broken", json_data=None)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        with pytest.raises(WallapopApiError) as excinfo:
            await fetcher.search(SearchQuery(keyword="x", marketplace="wallapop"))
    finally:
        await fetcher.aclose()

    assert excinfo.value.status_code == 500
    assert excinfo.value.body_excerpt == "upstream broken"


@pytest.mark.asyncio
async def test_http_429_raises_api_error(tmp_path: Path) -> None:
    """429 (rate limited) is treated as a generic API error — the
    orchestration layer handles backoff, not the adapter."""

    def handler(request: _RecordedRequest) -> WallapopResponse:
        return WallapopResponse(status_code=429, text="", json_data=None)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        with pytest.raises(WallapopApiError) as excinfo:
            await fetcher.search(SearchQuery(keyword="x", marketplace="wallapop"))
    finally:
        await fetcher.aclose()
    assert excinfo.value.status_code == 429


@pytest.mark.asyncio
async def test_missing_required_field_raises_schema_drift(tmp_path: Path) -> None:
    """A 200 response missing a required field surfaces as schema drift."""
    bad_payload = {
        "data": {
            "section": {
                "items": [
                    {
                        # 'id' missing — required field
                        "title": "WD Red Plus 4TB",
                        "price": {"amount": "55.00", "currency": "EUR"},
                    }
                ]
            }
        }
    }

    def handler(request: _RecordedRequest) -> WallapopResponse:
        return WallapopResponse(
            status_code=200, text=json.dumps(bad_payload), json_data=bad_payload
        )

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        with pytest.raises(WallapopSchemaDrift) as excinfo:
            await fetcher.search(SearchQuery(keyword="x", marketplace="wallapop"))
    finally:
        await fetcher.aclose()
    # The path mentions the missing field under the v3 envelope.
    assert "id" in excinfo.value.field_path


@pytest.mark.asyncio
async def test_unknown_extra_fields_are_tolerated(tmp_path: Path) -> None:
    """Wallapop adds fields over time; ignoring extras is the design."""
    payload = _valid_search_payload()
    payload["data"]["section"]["items"][0]["surprise_field"] = "this is fine"
    payload["data"]["section"]["new_section_field"] = {"x": 1}
    payload["new_top_level"] = {"x": 1}

    def handler(request: _RecordedRequest) -> WallapopResponse:
        return WallapopResponse(status_code=200, text=json.dumps(payload), json_data=payload)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        listings = await fetcher.search(SearchQuery(keyword="x", marketplace="wallapop"))
    finally:
        await fetcher.aclose()
    assert len(listings) == 2


# ─────────────────────────────────────────────────────────────────────────
# Logging — wallapop_search_succeeded carries the documented fields
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_search_logs_event_with_latency_and_count(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The structured logger writes JSON Lines to stdout (NFR-O1); the
    package-root logger has ``propagate=False`` so pytest's ``caplog``
    can't see it. Parsing the captured stdout is the right surface."""

    def handler(request: _RecordedRequest) -> WallapopResponse:
        payload = _valid_search_payload()
        return WallapopResponse(status_code=200, text=json.dumps(payload), json_data=payload)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        await fetcher.search(SearchQuery(keyword="x", marketplace="wallapop"))
    finally:
        await fetcher.aclose()

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    success = [r for r in records if r.get("event") == "wallapop_search_succeeded"]
    assert len(success) == 1
    record = success[0]
    assert record["marketplace"] == "wallapop"
    assert record["result_count"] == 2
    assert isinstance(record["latency_ms"], int)


# ─────────────────────────────────────────────────────────────────────────
# Single-listing fetch (used by `explain <url>` later)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_single_listing(tmp_path: Path) -> None:
    """``fetch()`` against the legacy ``/api/v3/items/{id}`` endpoint
    still returns a single-item payload with the same per-item shape
    as the search results — kept for ``salvager explain <url>`` and
    Phase 2 pre-buy reconciliation."""
    item = _valid_search_payload()["data"]["section"]["items"][0]

    def handler(request: _RecordedRequest) -> WallapopResponse:
        assert request.path == "/api/v3/items/abc123"
        return WallapopResponse(status_code=200, text=json.dumps(item), json_data=item)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        listing = await fetcher.fetch("https://es.wallapop.com/item/abc123")
    finally:
        await fetcher.aclose()
    assert listing.listing_id == "abc123"
    assert listing.price_eur == Decimal("55.00")


# ─────────────────────────────────────────────────────────────────────────
# verify=True — no codepath downgrades TLS (NFR-S3)
# ─────────────────────────────────────────────────────────────────────────


def test_fetcher_module_never_disables_tls_verification() -> None:
    """AST check: no call passes ``verify=False`` anywhere in the
    adapter source — covers both ``httpx`` (legacy) and ``curl_cffi``
    (current). curl_cffi's ``AsyncSession`` defaults to ``verify=True``
    and the adapter never overrides; this test fences future drift
    (NFR-S3)."""
    import ast

    src_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "salvager"
        / "adapters"
        / "wallapop_api"
        / "fetcher.py"
    )
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
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


# ─────────────────────────────────────────────────────────────────────────
# fetch_listing — reconciliation re-fetch by INTERNAL id (2026-07-18 drift)
# ─────────────────────────────────────────────────────────────────────────


def _detail_payload() -> dict[str, object]:
    """The per-item DETAIL shape (differs from the search shape)."""
    return {
        "id": "4z48xyk4ymjy",
        "title": {"original": "2x8GB Corsair DDR4 3000MHz RAM"},
        "description": {"original": "Dos módulos de memoria RAM."},
        "price": {"cash": {"amount": 60.0, "currency": "EUR"}},
        "slug": "2x8gb-corsair-ddr4-3000mhz-ram-1282474986",
        "user": {"id": "nzx59xek0m62"},
    }


def _known_listing() -> Listing:
    return Listing(
        listing_id="4z48xyk4ymjy",
        marketplace="wallapop",
        url="https://es.wallapop.com/item/2x8gb-corsair-ddr4-3000mhz-ram-1282474986",
        title="2x8GB Corsair DDR4 3000MHz RAM",
        description="d",
        price_eur=Decimal("60.00"),
        fetched_at=datetime(2026, 7, 18, tzinfo=UTC),
    )


async def test_fetch_listing_uses_internal_id_not_url_slug(tmp_path: Path) -> None:
    """Wallapop's item endpoint stopped accepting URL slugs (2026-07-18:
    slug and numeric tail both 404; only the internal id works) — the
    reconciliation re-fetch must key on ``listing.listing_id``."""
    payload = _detail_payload()

    def handler(request: _RecordedRequest) -> WallapopResponse:
        assert request.path == "/api/v3/items/4z48xyk4ymjy"  # id, NOT the slug
        return WallapopResponse(status_code=200, text=json.dumps(payload), json_data=payload)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        listing = await fetcher.fetch_listing(_known_listing())
    finally:
        await fetcher.aclose()
    assert listing.listing_id == "4z48xyk4ymjy"
    assert listing.price_eur == Decimal("60.0")
    assert listing.title == "2x8GB Corsair DDR4 3000MHz RAM"


async def test_fetch_listing_parses_the_detail_shape(tmp_path: Path) -> None:
    """The detail payload nests prose in {"original"} and price under
    {"cash"} — the search-shape model would reject it."""
    payload = _detail_payload()

    def handler(_: _RecordedRequest) -> WallapopResponse:
        return WallapopResponse(status_code=200, text=json.dumps(payload), json_data=payload)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        listing = await fetcher.fetch_listing(_known_listing())
    finally:
        await fetcher.aclose()
    assert listing.description == "Dos módulos de memoria RAM."
    assert listing.url.endswith("/item/2x8gb-corsair-ddr4-3000mhz-ram-1282474986")


async def test_fetch_listing_404_raises_api_error(tmp_path: Path) -> None:
    """A 404 by-id now genuinely means the listing is gone — the
    listing_gone classification downstream stays truthful."""

    def handler(_: _RecordedRequest) -> WallapopResponse:
        return WallapopResponse(status_code=404, text="", json_data=None)

    fetcher = _build_fetcher(tmp_path, handler)
    try:
        with pytest.raises(WallapopApiError) as excinfo:
            await fetcher.fetch_listing(_known_listing())
    finally:
        await fetcher.aclose()
    assert excinfo.value.status_code == 404
