"""Tests for the Phase 2 buy flows — Story 5.3.

The TinyFish SDK is mocked at the constructor seam: a fake async
client records every ``agent.run`` call and returns a preloaded
:class:`AgentRunResponse` (or raises a preloaded exception). No
network, no real API key.

Coverage:

  - happy path → :class:`BuySuccess` with the expected payment_method;
  - every :class:`BuyFailureReason` variant the adapter can produce
    from the agent's response or from a TinyFish SDK error;
  - wrong-marketplace guard (cross-flow refusal);
  - structural assertions on the goal text (the buy contract, the
    price-ceiling clause, and the JSON output contract are all
    embedded);
  - the fixture autouses a sentinel that crashes if any test causes
    the real :class:`AsyncTinyFish` to be constructed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from tinyfish import (
    AgentRunResponse,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
    RunStatus,
    SDKError,
)

from salvager.adapters.tinyfish_browser import (
    EbayCheckoutFlow,
    WallapopPayFlow,
)
from salvager.domain.errors import BuyFailureReason
from salvager.domain.listing import Listing
from salvager.interfaces.browser_session import (
    BuyFailure,
    BuySuccess,
)

_FAKE_KEY = SecretStr("sk-tinyfish-fake-deadbeefcafebabe0123456789abcdef")
_FIXED_TS = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
_MAX_PRICE = Decimal("60.00")


# ─────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────


def _fake_http_response(status_code: int) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=b"{}",
        request=httpx.Request("POST", "https://agent.tinyfish.ai/v1/automation/run"),
    )


class _FakeAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.next_response: AgentRunResponse | None = None
        self.next_exception: BaseException | None = None

    async def run(self, *, goal: str, url: str, **kwargs: Any) -> AgentRunResponse:
        self.calls.append({"goal": goal, "url": url, **kwargs})
        if self.next_exception is not None:
            raise self.next_exception
        assert self.next_response is not None, "test forgot to preload a response"
        return self.next_response


class _FakeClient:
    def __init__(self) -> None:
        self.agent = _FakeAgent()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _make_response(
    *,
    status: RunStatus = RunStatus.COMPLETED,
    result: dict[str, Any] | None = None,
    run_id: str = "run-buy-abc",
    error: Any = None,
) -> AgentRunResponse:
    return AgentRunResponse(
        status=status,
        run_id=run_id,
        result=result,
        error=error,
        num_of_steps=9,
        started_at=_FIXED_TS,
        finished_at=_FIXED_TS,
    )


def _wallapop_listing(**overrides: Any) -> Listing:
    base: dict[str, Any] = {
        "listing_id": "abc123",
        "marketplace": "wallapop",
        "url": "https://es.wallapop.com/item/abc123",
        "title": "WD Red Plus 4TB",
        "description": "Como nuevo.",
        "price_eur": Decimal("55.00"),
        "location": "Madrid",
        "photo_urls": ["https://cdn/photo.jpg"],
        "fetched_at": _FIXED_TS,
    }
    base.update(overrides)
    return Listing(**base)


def _ebay_listing(**overrides: Any) -> Listing:
    base: dict[str, Any] = {
        "listing_id": "1234567890",
        "marketplace": "ebay",
        "url": "https://www.ebay.es/itm/1234567890",
        "title": "Crucial 16GB DDR4 3200",
        "description": "Used, tested.",
        "price_eur": Decimal("40.00"),
        "location": None,
        "photo_urls": [],
        "fetched_at": _FIXED_TS,
    }
    base.update(overrides)
    return Listing(**base)


# ─────────────────────────────────────────────────────────────────────────
# Sentinel: production AsyncTinyFish must never be constructed in tests
# ─────────────────────────────────────────────────────────────────────────


class _RaisingRealClient:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("production AsyncTinyFish was constructed during a unit test")


@pytest.fixture(autouse=True)
def _block_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "salvager.adapters.tinyfish_browser._runtime.AsyncTinyFish",
        _RaisingRealClient,
    )


# ─────────────────────────────────────────────────────────────────────────
# Happy path — both flows
# ─────────────────────────────────────────────────────────────────────────


async def test_wallapop_pay_happy_path_returns_buy_success() -> None:
    client = _FakeClient()
    client.agent.next_response = _make_response(
        result={
            "outcome": "success",
            "price_paid_eur": "55.00",
            "receipt_id": "WP-2026-0001",
            "screenshot_url": "/app/data/screenshots/WP-2026-0001.png",
        }
    )
    flow = WallapopPayFlow(_FAKE_KEY, client=client)  # type: ignore[arg-type]

    result = await flow.execute_buy(_wallapop_listing(), _MAX_PRICE)

    assert isinstance(result, BuySuccess)
    assert result.kind == "success"
    assert result.payment_method == "wallapop_pay"
    assert result.price_paid_eur == Decimal("55.00")
    assert result.receipt_id == "WP-2026-0001"
    assert result.screenshot_url == "/app/data/screenshots/WP-2026-0001.png"
    # The agent was called against the listing URL and received the
    # composed goal carrying the price ceiling + JSON contract.
    assert len(client.agent.calls) == 1
    call = client.agent.calls[0]
    assert call["url"] == "https://es.wallapop.com/item/abc123"
    assert "Wallapop Pay" in call["goal"]
    assert "60.00 EUR" in call["goal"]
    assert '"outcome"' in call["goal"]


async def test_ebay_checkout_happy_path_returns_buy_success() -> None:
    client = _FakeClient()
    client.agent.next_response = _make_response(
        result={
            "outcome": "success",
            "price_paid_eur": "40.00",
            "receipt_id": "EB-9988-7766",
            "screenshot_url": "/app/data/screenshots/EB-9988-7766.png",
        }
    )
    flow = EbayCheckoutFlow(_FAKE_KEY, client=client)  # type: ignore[arg-type]

    result = await flow.execute_buy(_ebay_listing(), _MAX_PRICE)

    assert isinstance(result, BuySuccess)
    assert result.payment_method == "ebay_checkout"
    assert result.price_paid_eur == Decimal("40.00")
    assert len(client.agent.calls) == 1, "successful buy must have driven exactly one agent run"
    call = client.agent.calls[0]
    assert call["url"] == "https://www.ebay.es/itm/1234567890"
    assert "eBay" in call["goal"]


# ─────────────────────────────────────────────────────────────────────────
# Failure mappings from the agent's structured outcomes
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "outcome,expected_reason,extra_payload",
    [
        (
            "missing_element",
            BuyFailureReason.missing_element,
            {"missing": ["buy_button"]},
        ),
        (
            "screenshot_missing",
            BuyFailureReason.screenshot_missing,
            {"receipt_id": "WP-2026-0001"},
        ),
        (
            "marketplace_error",
            BuyFailureReason.marketplace_error,
            {"detail": "listing already sold"},
        ),
        ("timeout", BuyFailureReason.timeout, {"detail": "confirmation timed out"}),
        ("ui_check_failed", BuyFailureReason.ui_check_failed, {"missing": ["seller_block"]}),
    ],
)
async def test_agent_failure_outcome_maps_to_buy_failure(
    outcome: str,
    expected_reason: BuyFailureReason,
    extra_payload: dict[str, Any],
) -> None:
    client = _FakeClient()
    client.agent.next_response = _make_response(result={"outcome": outcome, **extra_payload})
    flow = WallapopPayFlow(_FAKE_KEY, client=client)  # type: ignore[arg-type]

    result = await flow.execute_buy(_wallapop_listing(), _MAX_PRICE)

    assert isinstance(result, BuyFailure)
    assert result.reason is expected_reason
    if "missing" in extra_payload:
        assert result.ctx["missing"] == extra_payload["missing"]
    if "detail" in extra_payload:
        assert result.ctx["detail"] == extra_payload["detail"]
    if "receipt_id" in extra_payload:
        assert result.ctx["receipt_id"] == extra_payload["receipt_id"]


# ─────────────────────────────────────────────────────────────────────────
# Failure mappings from TinyFish SDK exceptions + non-COMPLETED runs
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exception_factory,expected_reason",
    [
        (
            lambda: AuthenticationError("invalid key", response=_fake_http_response(401)),
            BuyFailureReason.payment_rail_unavailable,
        ),
        (
            lambda: PermissionDeniedError("out of credits", response=_fake_http_response(403)),
            BuyFailureReason.payment_rail_unavailable,
        ),
        (
            lambda: RateLimitError("rate-limited", response=_fake_http_response(429)),
            BuyFailureReason.marketplace_error,
        ),
        (lambda: SDKError("boom"), BuyFailureReason.marketplace_error),
    ],
)
async def test_tinyfish_sdk_errors_map_to_buy_failure(
    exception_factory: Any,
    expected_reason: BuyFailureReason,
) -> None:
    client = _FakeClient()
    client.agent.next_exception = exception_factory()
    flow = WallapopPayFlow(_FAKE_KEY, client=client)  # type: ignore[arg-type]

    result = await flow.execute_buy(_wallapop_listing(), _MAX_PRICE)

    assert isinstance(result, BuyFailure)
    assert result.reason is expected_reason


async def test_run_not_completed_maps_to_marketplace_error() -> None:
    client = _FakeClient()
    client.agent.next_response = _make_response(status=RunStatus.FAILED, result=None)
    flow = WallapopPayFlow(_FAKE_KEY, client=client)  # type: ignore[arg-type]

    result = await flow.execute_buy(_wallapop_listing(), _MAX_PRICE)

    assert isinstance(result, BuyFailure)
    assert result.reason is BuyFailureReason.marketplace_error
    assert result.ctx["status"] == "FAILED"


async def test_completed_with_no_result_maps_to_ui_check_failed() -> None:
    client = _FakeClient()
    client.agent.next_response = _make_response(result=None)
    flow = WallapopPayFlow(_FAKE_KEY, client=client)  # type: ignore[arg-type]

    result = await flow.execute_buy(_wallapop_listing(), _MAX_PRICE)

    assert isinstance(result, BuyFailure)
    assert result.reason is BuyFailureReason.ui_check_failed


async def test_schema_drift_maps_to_ui_check_failed() -> None:
    """A payload missing the discriminator (or carrying extras) is
    treated as UI-check-failed, not silent success."""
    client = _FakeClient()
    client.agent.next_response = _make_response(result={"price_paid_eur": "55.00"})
    flow = WallapopPayFlow(_FAKE_KEY, client=client)  # type: ignore[arg-type]

    result = await flow.execute_buy(_wallapop_listing(), _MAX_PRICE)

    assert isinstance(result, BuyFailure)
    assert result.reason is BuyFailureReason.ui_check_failed


# ─────────────────────────────────────────────────────────────────────────
# Defensive guards inside the success branch
# ─────────────────────────────────────────────────────────────────────────


async def test_success_outcome_with_garbage_price_fails_ui_check() -> None:
    client = _FakeClient()
    client.agent.next_response = _make_response(
        result={
            "outcome": "success",
            "price_paid_eur": "not-a-number",
            "receipt_id": "WP-2026-0001",
            "screenshot_url": "/app/data/screenshots/x.png",
        }
    )
    flow = WallapopPayFlow(_FAKE_KEY, client=client)  # type: ignore[arg-type]

    result = await flow.execute_buy(_wallapop_listing(), _MAX_PRICE)

    assert isinstance(result, BuyFailure)
    assert result.reason is BuyFailureReason.ui_check_failed


async def test_success_outcome_without_screenshot_falls_back_to_screenshot_missing() -> None:
    """UX-DR9 — the buy may have completed but we cannot prove it
    without the receipt screenshot. That collapses to the
    ``screenshot_missing`` reassurance variant."""
    client = _FakeClient()
    client.agent.next_response = _make_response(
        result={
            "outcome": "success",
            "price_paid_eur": "55.00",
            "receipt_id": "WP-2026-0001",
            "screenshot_url": "",
        }
    )
    flow = WallapopPayFlow(_FAKE_KEY, client=client)  # type: ignore[arg-type]

    result = await flow.execute_buy(_wallapop_listing(), _MAX_PRICE)

    assert isinstance(result, BuyFailure)
    assert result.reason is BuyFailureReason.screenshot_missing
    assert result.ctx["receipt_id"] == "WP-2026-0001"


# ─────────────────────────────────────────────────────────────────────────
# Cross-flow marketplace guard
# ─────────────────────────────────────────────────────────────────────────


async def test_wallapop_flow_refuses_ebay_listing_without_calling_tinyfish() -> None:
    client = _FakeClient()
    flow = WallapopPayFlow(_FAKE_KEY, client=client)  # type: ignore[arg-type]

    result = await flow.execute_buy(_ebay_listing(), _MAX_PRICE)

    assert isinstance(result, BuyFailure)
    assert result.reason is BuyFailureReason.marketplace_error
    assert client.agent.calls == []  # never touched the network


async def test_ebay_flow_refuses_wallapop_listing_without_calling_tinyfish() -> None:
    client = _FakeClient()
    flow = EbayCheckoutFlow(_FAKE_KEY, client=client)  # type: ignore[arg-type]

    result = await flow.execute_buy(_wallapop_listing(), _MAX_PRICE)

    assert isinstance(result, BuyFailure)
    assert result.reason is BuyFailureReason.marketplace_error
    assert client.agent.calls == []


# ─────────────────────────────────────────────────────────────────────────
# Lifecycle
# ─────────────────────────────────────────────────────────────────────────


async def test_close_is_idempotent_when_client_was_injected() -> None:
    """The injected client is OWNED by the test (not the flow), so the
    flow must NOT close it."""
    client = _FakeClient()
    flow = WallapopPayFlow(_FAKE_KEY, client=client)  # type: ignore[arg-type]

    await flow.close()
    await flow.close()

    assert client.closed is False
