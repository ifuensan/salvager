"""Tests for the Wallapop offer flow (wallapop-offer-flow).

Same seam as the buy-flow tests: a fake async TinyFish client records
every ``agent.stream`` call and returns a preloaded response (or raises a
preloaded exception). Coverage: happy path (with and without a
screenshot), every agent-reported failure outcome, SDK errors, echoed-
amount mismatch, malformed payload, wrong-marketplace guard, and the
goal contract's structural clauses.
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
    RateLimitError,
    RunStatus,
    SDKError,
)
from tinyfish_stream_fakes import FakeClient as _FakeClient

from salvager.adapters.tinyfish_browser import WallapopOfferFlow
from salvager.adapters.tinyfish_browser.wallapop_offer import (
    DEFAULT_MAX_OFFER_STEPS,
    OFFER_OUTPUT_CONTRACT,
    render_offer_goal,
)
from salvager.domain.errors import OfferFailureReason
from salvager.domain.listing import Listing
from salvager.interfaces.offer_session import OfferSendFailure, OfferSuccess

_FAKE_KEY = SecretStr("sk-tinyfish-fake-deadbeefcafebabe0123456789abcdef")
_FIXED_TS = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
_AMOUNT = Decimal("70")


def _make_response(
    *,
    status: RunStatus = RunStatus.COMPLETED,
    result: dict[str, Any] | None = None,
    run_id: str = "run-offer-abc",
) -> AgentRunResponse:
    return AgentRunResponse(
        status=status,
        run_id=run_id,
        result=result,
        error=None,
        num_of_steps=9,
        started_at=_FIXED_TS,
        finished_at=_FIXED_TS,
    )


def _listing(marketplace: str = "wallapop") -> Listing:
    return Listing(
        listing_id="internal-123",
        marketplace=marketplace,  # type: ignore[arg-type]
        url="https://es.wallapop.com/item/corsair-abc",
        title="Corsair Vengeance LPX 16GB",
        description="d",
        price_eur=Decimal("88.00"),
        fetched_at=_FIXED_TS,
    )


def _flow() -> tuple[WallapopOfferFlow, _FakeClient]:
    client = _FakeClient()
    flow = WallapopOfferFlow(_FAKE_KEY, client=client)  # type: ignore[arg-type]
    return flow, client


async def test_happy_path_returns_success_with_counter() -> None:
    flow, client = _flow()
    client.agent.next_response = _make_response(
        result={
            "outcome": "success",
            "offered_eur": "70",
            "screenshot_url": "https://shots/offer.png",
            "platform_remaining": 9,
        }
    )
    result = await flow.execute_offer(_listing(), _AMOUNT)
    assert isinstance(result, OfferSuccess)
    assert result.offered_eur == _AMOUNT
    assert result.screenshot_url == "https://shots/offer.png"
    assert result.platform_remaining == 9


async def test_verified_send_without_screenshot_is_still_success() -> None:
    flow, client = _flow()
    client.agent.next_response = _make_response(
        result={"outcome": "success", "offered_eur": "70", "screenshot_url": None}
    )
    result = await flow.execute_offer(_listing(), _AMOUNT)
    assert isinstance(result, OfferSuccess)
    assert result.screenshot_url is None


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("offer_unavailable", OfferFailureReason.offer_unavailable),
        ("amount_rejected", OfferFailureReason.amount_rejected),
        ("daily_limit_reached", OfferFailureReason.daily_limit_reached),
        ("missing_element", OfferFailureReason.missing_element),
        ("screenshot_missing", OfferFailureReason.screenshot_missing),
        ("marketplace_error", OfferFailureReason.marketplace_error),
        ("timeout", OfferFailureReason.timeout),
        ("ui_check_failed", OfferFailureReason.ui_check_failed),
    ],
)
async def test_agent_failure_outcomes_map_to_reasons(
    outcome: str, reason: OfferFailureReason
) -> None:
    flow, client = _flow()
    client.agent.next_response = _make_response(
        result={"outcome": outcome, "detail": "x", "missing": ["offer_button"]}
    )
    result = await flow.execute_offer(_listing(), _AMOUNT)
    assert isinstance(result, OfferSendFailure)
    assert result.reason is reason
    assert result.ctx.get("detail") == "x"


async def test_exhausted_platform_counter_carries_remaining_zero() -> None:
    flow, client = _flow()
    client.agent.next_response = _make_response(
        result={"outcome": "daily_limit_reached", "platform_remaining": 0}
    )
    result = await flow.execute_offer(_listing(), _AMOUNT)
    assert isinstance(result, OfferSendFailure)
    assert result.reason is OfferFailureReason.daily_limit_reached
    assert result.ctx["platform_remaining"] == 0


async def test_echoed_amount_mismatch_is_ui_check_failed() -> None:
    flow, client = _flow()
    client.agent.next_response = _make_response(result={"outcome": "success", "offered_eur": "65"})
    result = await flow.execute_offer(_listing(), _AMOUNT)
    assert isinstance(result, OfferSendFailure)
    assert result.reason is OfferFailureReason.ui_check_failed
    assert result.ctx["expected"] == "70"


async def test_malformed_payload_is_marketplace_error() -> None:
    flow, client = _flow()
    client.agent.next_response = _make_response(result={"outcome": "nonsense", "extra": 1})
    result = await flow.execute_offer(_listing(), _AMOUNT)
    assert isinstance(result, OfferSendFailure)
    assert result.reason is OfferFailureReason.marketplace_error


async def test_sdk_error_is_marketplace_error() -> None:
    flow, client = _flow()
    client.agent.next_exception = SDKError("boom")
    result = await flow.execute_offer(_listing(), _AMOUNT)
    assert isinstance(result, OfferSendFailure)
    assert result.reason is OfferFailureReason.marketplace_error


async def test_rate_limit_is_marketplace_error_with_detail() -> None:
    flow, client = _flow()
    client.agent.next_exception = RateLimitError(
        "slow down",
        response=httpx.Response(
            status_code=429,
            content=b"{}",
            request=httpx.Request("POST", "https://agent.tinyfish.ai/v1/automation/run"),
        ),
    )
    result = await flow.execute_offer(_listing(), _AMOUNT)
    assert isinstance(result, OfferSendFailure)
    assert result.reason is OfferFailureReason.marketplace_error
    assert result.ctx["detail"] == "tinyfish_rate_limited"


async def test_incomplete_run_is_marketplace_error() -> None:
    flow, client = _flow()
    client.agent.next_response = _make_response(status=RunStatus.FAILED, result=None)
    result = await flow.execute_offer(_listing(), _AMOUNT)
    assert isinstance(result, OfferSendFailure)
    assert result.reason is OfferFailureReason.marketplace_error


async def test_runaway_agent_is_cancelled_at_the_step_budget() -> None:
    flow, client = _flow()
    client.agent.next_response = _make_response(result={"outcome": "success", "offered_eur": "70"})
    client.agent.steps = 500

    result = await flow.execute_offer(_listing(), _AMOUNT)

    assert isinstance(result, OfferSendFailure)
    assert result.reason is OfferFailureReason.timeout
    assert result.ctx["max_steps"] == DEFAULT_MAX_OFFER_STEPS
    assert client.cancelled == ["/v1/runs/run-offer-abc/cancel"]
    assert client.agent.progress_emitted == DEFAULT_MAX_OFFER_STEPS + 1, "stops consuming at once"


async def test_wall_clock_timeout_cancels_the_remote_run() -> None:
    client = _FakeClient()
    flow = WallapopOfferFlow(_FAKE_KEY, client=client, max_duration_s=1)  # type: ignore[arg-type]
    client.agent.next_response = _make_response(result={"outcome": "success", "offered_eur": "70"})
    client.agent.steps = 1
    client.agent.hang = True

    result = await flow.execute_offer(_listing(), _AMOUNT)

    assert isinstance(result, OfferSendFailure)
    assert result.reason is OfferFailureReason.timeout
    assert result.ctx["budget_s"] == 1
    assert client.cancelled == ["/v1/runs/run-offer-abc/cancel"]


async def test_wrong_marketplace_is_refused_without_agent_call() -> None:
    flow, client = _flow()
    result = await flow.execute_offer(_listing(marketplace="ebay"), _AMOUNT)
    assert isinstance(result, OfferSendFailure)
    assert result.reason is OfferFailureReason.marketplace_error
    assert client.agent.calls == []


def test_goal_embeds_amount_contract_and_platform_rules() -> None:
    goal = render_offer_goal(_AMOUNT)
    assert "EXACTLY 70 EUR" in goal
    assert OFFER_OUTPUT_CONTRACT in goal
    assert "Hacer oferta" in goal
    assert "ofertas restantes" in goal
    assert "do NOT buy" in goal
