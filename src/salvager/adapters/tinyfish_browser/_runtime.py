"""Shared TinyFish-buy runtime — Story 5.3.

The piece of plumbing both buy flows reuse:

  - the JSON schema the agent must return (:class:`BuyAgentResponse`);
  - the goal-template assembly helper that injects the operator's
    price ceiling and the structured-output contract;
  - :func:`execute_buy_via_tinyfish` — the single async entry point
    that runs the agent, maps every TinyFish SDK error to a
    :class:`BuyFailure`, parses the response, and returns a
    :class:`BuyResult`.

Per-flow modules (``wallapop_pay.py`` / ``ebay_checkout.py``) only
need to build the per-marketplace goal text + URL and call
:func:`execute_buy_via_tinyfish` — they hold no TinyFish-runtime
knowledge of their own. This keeps the deny-list surface (Story 5.14)
small and audited in one place.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from tinyfish import (
    AgentRunResponse,
    AsyncTinyFish,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
    RunStatus,
    SDKError,
)

from salvager.domain.errors import BuyFailureReason
from salvager.interfaces.browser_session import (
    BuyFailure,
    BuyResult,
    BuySuccess,
)

#: Wall-clock budget per buy. The marketplace flows are
#: human-paced multi-step checkouts; 120 s is comfortable for the
#: happy path and tight enough to bound a stuck agent.
DEFAULT_MAX_DURATION_S: Final[int] = 120

#: The structured-output contract the agent MUST honour. Embedded in
#: every per-flow goal so the parser's contract is part of the prompt.
BUY_OUTPUT_CONTRACT: Final[str] = (
    "Return STRICTLY this JSON shape and nothing else (no prose, no markdown):\n"
    "{\n"
    '  "outcome": "success" | "missing_element" | "screenshot_missing" '
    '| "marketplace_error" | "timeout" | "ui_check_failed",\n'
    '  "price_paid_eur": "<string decimal, e.g. \\"55.00\\", required only when '
    'outcome=success or outcome=screenshot_missing>",\n'
    '  "receipt_id": "<string, required when outcome=success or '
    'outcome=screenshot_missing>",\n'
    '  "screenshot_url": "<string, required when outcome=success>",\n'
    '  "missing": ["<element name>", ...]   // required when outcome=missing_element '
    "or outcome=ui_check_failed\n"
    '  "detail": "<short string explaining the outcome>"  // optional, copied into ctx\n'
    "}\n"
)


class BuyAgentResponse(BaseModel):
    """The shape the agent must return — parsed at the adapter
    boundary so the orchestrator never sees raw JSON.

    A response that fails validation surfaces as
    :class:`BuyFailure(reason=ui_check_failed)` with the validation
    error in the ctx. Treating the schema as a UI invariant (rather
    than a generic schema-drift) matches FR28's intent: from the
    operator's point of view, a buy flow that returns garbage is the
    UI failing the agent.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: Literal[
        "success",
        "missing_element",
        "screenshot_missing",
        "marketplace_error",
        "timeout",
        "ui_check_failed",
    ]
    price_paid_eur: str | None = None
    receipt_id: str | None = None
    screenshot_url: str | None = None
    missing: list[str] = Field(default_factory=list)
    detail: str | None = None


def build_client(api_key: SecretStr) -> AsyncTinyFish:
    """Single point where the API key leaves :class:`SecretStr`."""
    return AsyncTinyFish(api_key=api_key.get_secret_value())


def render_buy_goal(intro: str, *, max_price_eur: Decimal) -> str:
    """Compose the per-flow intro with the universal price-ceiling
    clause and the JSON contract. Every flow passes through here so
    the budget guard is uniform."""
    ceiling_clause = (
        "Hard constraint: if the price displayed on the buy page exceeds "
        f"{max_price_eur} EUR, ABORT the buy without clicking — return "
        '{"outcome": "marketplace_error", "detail": "price above operator ceiling"}.\n\n'
    )
    return f"{intro.rstrip()}\n\n{ceiling_clause}{BUY_OUTPUT_CONTRACT}"


async def execute_buy_via_tinyfish(
    client: AsyncTinyFish,
    *,
    goal: str,
    url: str,
    payment_method: Literal["wallapop_pay", "ebay_checkout"],
    max_duration_s: int,
    log: logging.Logger,
) -> BuyResult:
    """Drive one buy through the TinyFish agent and return a typed
    :class:`BuyResult`. Never raises — every failure path produces a
    :class:`BuyFailure` so the orchestrator branches on a single value.
    """
    started = time.perf_counter()

    try:
        response = await asyncio.wait_for(
            client.agent.run(goal=goal, url=url),
            timeout=max_duration_s,
        )
    except TimeoutError:
        log.warning("tinyfish_buy_timeout", extra={"url": url, "budget_s": max_duration_s})
        return BuyFailure(
            reason=BuyFailureReason.timeout,
            ctx={"budget_s": max_duration_s, "url": url},
        )
    except AuthenticationError as exc:
        log.exception("tinyfish_buy_auth_failed", extra={"error_class": exc.__class__.__name__})
        return BuyFailure(
            reason=BuyFailureReason.payment_rail_unavailable,
            ctx={"error_class": exc.__class__.__name__, "detail": "tinyfish_auth_failed"},
        )
    except PermissionDeniedError as exc:
        log.exception(
            "tinyfish_buy_permission_denied", extra={"error_class": exc.__class__.__name__}
        )
        return BuyFailure(
            reason=BuyFailureReason.payment_rail_unavailable,
            ctx={
                "error_class": exc.__class__.__name__,
                "detail": "tinyfish_permission_denied",
            },
        )
    except RateLimitError as exc:
        log.warning("tinyfish_buy_rate_limited", extra={"error_class": exc.__class__.__name__})
        return BuyFailure(
            reason=BuyFailureReason.marketplace_error,
            ctx={"error_class": exc.__class__.__name__, "detail": "tinyfish_rate_limited"},
        )
    except SDKError as exc:
        log.exception("tinyfish_buy_sdk_error", extra={"error_class": exc.__class__.__name__})
        return BuyFailure(
            reason=BuyFailureReason.marketplace_error,
            ctx={"error_class": exc.__class__.__name__, "detail": str(exc)},
        )

    elapsed = int(time.perf_counter() - started)

    if response.status != RunStatus.COMPLETED:
        log.error(
            "tinyfish_buy_run_not_completed",
            extra={"run_id": response.run_id, "status": str(response.status)},
        )
        return BuyFailure(
            reason=BuyFailureReason.marketplace_error,
            ctx={
                "run_id": response.run_id,
                "status": str(response.status),
                "detail": "tinyfish run did not complete",
            },
        )

    parsed = _parse_response(response, log=log)
    if isinstance(parsed, BuyFailure):
        return parsed

    return _payload_to_buy_result(
        parsed,
        payment_method=payment_method,
        total_seconds=elapsed,
        log=log,
    )


def _parse_response(
    response: AgentRunResponse,
    *,
    log: logging.Logger,
) -> BuyAgentResponse | BuyFailure:
    """Walk the TinyFish ``response.result`` into :class:`BuyAgentResponse`.

    Both ``None`` and a payload that fails schema validation surface
    as ``BuyFailure(reason=ui_check_failed)`` — the UI did not produce
    a parseable confirmation, which is operationally indistinguishable
    from a missing element from the operator's perspective.
    """
    if response.result is None:
        log.error("tinyfish_buy_empty_result", extra={"run_id": response.run_id})
        return BuyFailure(
            reason=BuyFailureReason.ui_check_failed,
            ctx={"run_id": response.run_id, "detail": "agent returned no result"},
        )
    try:
        return BuyAgentResponse.model_validate(response.result)
    except ValidationError as exc:
        log.exception(
            "tinyfish_buy_response_schema_drift",
            extra={"run_id": response.run_id, "errors": str(exc)},
        )
        return BuyFailure(
            reason=BuyFailureReason.ui_check_failed,
            ctx={
                "run_id": response.run_id,
                "detail": "agent response failed schema validation",
            },
        )


def _payload_to_buy_result(
    payload: BuyAgentResponse,
    *,
    payment_method: Literal["wallapop_pay", "ebay_checkout"],
    total_seconds: int,
    log: logging.Logger,
) -> BuyResult:
    """Translate the parsed agent payload into a :class:`BuyResult`."""
    if payload.outcome == "success":
        price = _coerce_price(payload.price_paid_eur)
        if price is None:
            return BuyFailure(
                reason=BuyFailureReason.ui_check_failed,
                ctx={"detail": "agent claimed success but price_paid_eur was unparseable"},
            )
        if not payload.receipt_id or not payload.screenshot_url:
            return BuyFailure(
                reason=BuyFailureReason.screenshot_missing,
                ctx={
                    "receipt_id": payload.receipt_id,
                    "detail": "agent claimed success but receipt/screenshot were missing",
                },
            )
        log.info(
            "tinyfish_buy_success",
            extra={"receipt_id": payload.receipt_id, "total_seconds": total_seconds},
        )
        return BuySuccess(
            price_paid_eur=price,
            payment_method=payment_method,
            receipt_id=payload.receipt_id,
            screenshot_url=payload.screenshot_url,
            total_seconds=total_seconds,
        )

    return _payload_to_failure(payload)


def _payload_to_failure(payload: BuyAgentResponse) -> BuyFailure:
    """Map the failure-outcome payloads to :class:`BuyFailure`."""
    reason_map: dict[str, BuyFailureReason] = {
        "missing_element": BuyFailureReason.missing_element,
        "screenshot_missing": BuyFailureReason.screenshot_missing,
        "marketplace_error": BuyFailureReason.marketplace_error,
        "timeout": BuyFailureReason.timeout,
        "ui_check_failed": BuyFailureReason.ui_check_failed,
    }
    reason = reason_map[payload.outcome]

    ctx: dict[str, Any] = {}
    if payload.detail:
        ctx["detail"] = payload.detail
    if payload.missing:
        ctx["missing"] = list(payload.missing)
    if payload.receipt_id:
        ctx["receipt_id"] = payload.receipt_id
    return BuyFailure(reason=reason, ctx=ctx)


def _coerce_price(raw: str | None) -> Decimal | None:
    """Parse a positive decimal price string; return None on garbage.

    The agent's contract asks for ``"55.00"`` (string, dot-separator);
    we still accept comma-separator for resilience, but a negative or
    non-numeric value is rejected — the caller turns that into a
    :class:`BuyFailure`.
    """
    if not raw:
        return None
    cleaned = raw.strip().replace(",", ".")
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if value <= 0:
        return None
    return value


__all__ = [
    "BUY_OUTPUT_CONTRACT",
    "DEFAULT_MAX_DURATION_S",
    "BuyAgentResponse",
    "build_client",
    "execute_buy_via_tinyfish",
    "render_buy_goal",
]
