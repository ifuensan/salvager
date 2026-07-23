"""Wallapop "hacer oferta" flow (wallapop-offer-flow, FR58-FR65).

Drives Wallapop's native offer form through TinyFish. The operator's
authenticated session opens the listing page, taps the "Hacer oferta"
button (present next to the buy button on offer-eligible listings —
operator-captured 2026-07-22, `openspec/changes/wallapop-make-offer/
captures/`), enters EXACTLY the bounded amount, submits, verifies the
sent state, and captures a screenshot.

Platform rules the goal encodes (from Wallapop's help pages + the
operator's captures):

  - offers are unavailable on PRO-seller / refurbished / excluded-
    category listings — the button simply isn't there (`offer_unavailable`);
  - the form rejects amounts below 70 % of asking ("Tu oferta debe ser
    de al menos X €") — `amount_rejected`; the domain pre-validates this
    floor so it should never fire on a computed amount;
  - the form shows "N ofertas restantes para hoy" (10/day account cap);
    an exhausted counter is `daily_limit_reached`, and the value is
    captured into the audit row when visible.

No money moves in this flow — an offer is a negotiation message, and
the purchase (if the seller accepts) stays behind the Comprar path.
The sent state, not the screenshot, is the success criterion: a
verified send with a failed capture returns success with
``screenshot_url = None``; an unverified submit returns the ambiguous
``screenshot_missing`` failure so the operator checks the chat.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
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

from salvager.adapters.tinyfish_browser._runtime import (
    DEFAULT_MAX_DURATION_S,
    build_client,
)
from salvager.domain.errors import OfferFailureReason
from salvager.domain.listing import Listing
from salvager.interfaces.offer_session import (
    OfferResult,
    OfferSendFailure,
    OfferSession,
    OfferSuccess,
)
from salvager.observability.logging import get_logger

#: The structured-output contract the offer agent MUST honour.
OFFER_OUTPUT_CONTRACT: Final[str] = (
    "Return STRICTLY this JSON shape and nothing else (no prose, no markdown):\n"
    "{\n"
    '  "outcome": "success" | "offer_unavailable" | "amount_rejected" '
    '| "daily_limit_reached" | "missing_element" | "screenshot_missing" '
    '| "marketplace_error" | "timeout" | "ui_check_failed",\n'
    '  "offered_eur": "<string decimal echo of the amount entered, required when '
    'outcome=success>",\n'
    '  "screenshot_url": "<string, when a confirmation screenshot was captured>",\n'
    '  "platform_remaining": <integer, the "ofertas restantes" counter when visible, '
    "else null>,\n"
    '  "missing": ["<element name>", ...]   // required when outcome=missing_element '
    "or outcome=ui_check_failed\n"
    '  "detail": "<short string explaining the outcome>"  // optional, copied into ctx\n'
    "}\n"
)

#: The offer-flow agent goal. ``render_offer_goal`` injects the exact
#: amount as a hard constraint before this contract.
_WALLAPOP_OFFER_GOAL: Final[str] = (
    "Open the Wallapop listing page and send a price offer via the native "
    '"Hacer oferta" flow. This sends a negotiation message to the seller — '
    "do NOT buy, do NOT initiate any checkout, do NOT touch any other button.\n"
    "\n"
    "Step-by-step:\n"
    "1. Navigate to the listing URL (the operator's existing Wallapop session "
    "   is loaded).\n"
    '2. Look for the "Hacer oferta" button on the listing page (next to the '
    "   buy button). If it is absent (PRO seller, refurbished product, or an "
    "   excluded category), return "
    '{"outcome": "offer_unavailable", "detail": "no offer button"}.\n'
    '3. Click "Hacer oferta". The offer form shows the listing, an amount '
    '   field ("Tu oferta"), and a counter like "N ofertas restantes para '
    '   hoy". Record the counter value as `platform_remaining` when visible.\n'
    "4. If the counter shows 0 offers remaining, or the form says the daily "
    "   offer limit is reached, ABORT without submitting — return "
    '{"outcome": "daily_limit_reached", "platform_remaining": 0}.\n'
    "5. Enter the offer amount in the amount field. The field uses a COMMA "
    '   as the decimal separator (e.g. "20,22"); our amounts are whole '
    '   euros, so enter just the integer (e.g. "61"). Verify the field '
    "   shows EXACTLY that amount before continuing.\n"
    '6. If the form rejects the amount (e.g. "Tu oferta debe ser de al menos '
    '   X €"), ABORT without submitting — return '
    '{"outcome": "amount_rejected", "detail": "<the form\'s message>"}.\n'
    '7. Tap "Enviar" and await the sent state: the form closes and the offer '
    "   appears as sent (in the form's confirmation or the listing's chat). "
    "   Budget: 30 s.\n"
    "8. If you submitted but CANNOT verify the sent state, return "
    '{"outcome": "screenshot_missing", "detail": "submitted, sent state '
    'unverified"}.\n'
    "9. Capture a screenshot of the sent confirmation and record its URL in "
    "   `screenshot_url` (when the capture fails but the sent state IS "
    "   verified, leave `screenshot_url` null). Return "
    '{"outcome": "success", "offered_eur": "<amount>", '
    '"screenshot_url": <url or null>, "platform_remaining": <n or null>}.\n'
    "\n"
    "If the marketplace shows an error page, returns a 4xx/5xx, or the "
    "listing no longer exists, return "
    '{"outcome": "marketplace_error", "detail": "<one-line summary>"}.\n'
    "If any expected element is missing mid-flow, return "
    '{"outcome": "missing_element", "missing": [list of the missing names]}.\n'
    "\n"
    "Recovery hint: the offer form is also directly addressable at "
    "https://es.wallapop.com/app/chat/offer?itemId=<the listing's internal "
    "id> — if the listing page's button is present but unresponsive, you "
    "may navigate there instead. Never use that URL to bypass step 2's "
    "missing-button check.\n"
)

_AGENT_OUTCOME_TO_REASON: Final[dict[str, OfferFailureReason]] = {
    "offer_unavailable": OfferFailureReason.offer_unavailable,
    "amount_rejected": OfferFailureReason.amount_rejected,
    "daily_limit_reached": OfferFailureReason.daily_limit_reached,
    "missing_element": OfferFailureReason.missing_element,
    "screenshot_missing": OfferFailureReason.screenshot_missing,
    "marketplace_error": OfferFailureReason.marketplace_error,
    "timeout": OfferFailureReason.timeout,
    "ui_check_failed": OfferFailureReason.ui_check_failed,
}


class OfferAgentResponse(BaseModel):
    """The shape the offer agent must return — parsed at the adapter
    boundary so the orchestrator never sees raw JSON. A payload that
    fails validation surfaces as ``ui_check_failed``."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal[
        "success",
        "offer_unavailable",
        "amount_rejected",
        "daily_limit_reached",
        "missing_element",
        "screenshot_missing",
        "marketplace_error",
        "timeout",
        "ui_check_failed",
    ]
    offered_eur: str | None = None
    screenshot_url: str | None = None
    platform_remaining: int | None = None
    missing: list[str] = Field(default_factory=list)
    detail: str | None = None


def render_offer_goal(amount_eur: Decimal) -> str:
    """Compose the offer goal with the exact-amount hard constraint and
    the JSON contract. Every send passes through here so the amount
    guard is uniform."""
    amount_clause = (
        f"Hard constraint: the offer amount is EXACTLY {amount_eur} EUR. Never "
        "enter any other value; if the form will not accept exactly this "
        'amount, ABORT without submitting and return {"outcome": '
        '"amount_rejected"}.\n\n'
    )
    return f"{_WALLAPOP_OFFER_GOAL.rstrip()}\n\n{amount_clause}{OFFER_OUTPUT_CONTRACT}"


class WallapopOfferFlow(OfferSession):
    """Concrete :class:`OfferSession` for Wallapop's native offer form."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        client: AsyncTinyFish | None = None,
        max_duration_s: int = DEFAULT_MAX_DURATION_S,
    ) -> None:
        self._owned_client = client is None
        if client is None:
            client = build_client(api_key)
        self._client = client
        self._max_duration_s = max_duration_s
        self._log = get_logger("adapter.tinyfish_browser.wallapop_offer")

    async def close(self) -> None:
        """Close the underlying TinyFish client. Idempotent."""
        if self._owned_client:
            await self._client.close()

    async def execute_offer(self, listing: Listing, amount_eur: Decimal) -> OfferResult:
        if listing.marketplace != "wallapop":
            return OfferSendFailure(
                reason=OfferFailureReason.marketplace_error,
                ctx={
                    "detail": (
                        f"WallapopOfferFlow refuses {listing.marketplace} listings — "
                        "wrong marketplace"
                    ),
                    "marketplace": listing.marketplace,
                },
            )
        goal = render_offer_goal(amount_eur)
        return await _execute_offer_via_tinyfish(
            self._client,
            goal=goal,
            url=str(listing.url),
            amount_eur=amount_eur,
            max_duration_s=self._max_duration_s,
            log=self._log,
        )


async def _execute_offer_via_tinyfish(
    client: AsyncTinyFish,
    *,
    goal: str,
    url: str,
    amount_eur: Decimal,
    max_duration_s: int,
    log: logging.Logger,
) -> OfferResult:
    """Drive one offer through the TinyFish agent. Never raises — every
    failure path produces an :class:`OfferSendFailure`."""
    started = time.perf_counter()

    try:
        response = await asyncio.wait_for(
            client.agent.run(goal=goal, url=url),
            timeout=max_duration_s,
        )
    except TimeoutError:
        log.warning("tinyfish_offer_timeout", extra={"url": url, "budget_s": max_duration_s})
        return OfferSendFailure(
            reason=OfferFailureReason.timeout,
            ctx={"budget_s": max_duration_s, "url": url},
        )
    except (AuthenticationError, PermissionDeniedError) as exc:
        log.exception("tinyfish_offer_auth_failed", extra={"error_class": exc.__class__.__name__})
        return OfferSendFailure(
            reason=OfferFailureReason.marketplace_error,
            ctx={"error_class": exc.__class__.__name__, "detail": "tinyfish_auth_failed"},
        )
    except RateLimitError as exc:
        log.warning("tinyfish_offer_rate_limited", extra={"error_class": exc.__class__.__name__})
        return OfferSendFailure(
            reason=OfferFailureReason.marketplace_error,
            ctx={"error_class": exc.__class__.__name__, "detail": "tinyfish_rate_limited"},
        )
    except SDKError as exc:
        log.exception("tinyfish_offer_sdk_error", extra={"error_class": exc.__class__.__name__})
        return OfferSendFailure(
            reason=OfferFailureReason.marketplace_error,
            ctx={"error_class": exc.__class__.__name__, "detail": str(exc)},
        )

    elapsed = int(time.perf_counter() - started)

    if response.status != RunStatus.COMPLETED:
        log.error(
            "tinyfish_offer_run_not_completed",
            extra={"run_id": response.run_id, "status": str(response.status)},
        )
        return OfferSendFailure(
            reason=OfferFailureReason.marketplace_error,
            ctx={
                "run_id": response.run_id,
                "status": str(response.status),
                "detail": "tinyfish run did not complete",
            },
        )

    parsed = _parse_offer_response(response, log=log)
    if isinstance(parsed, OfferSendFailure):
        return parsed

    return _payload_to_offer_result(parsed, amount_eur=amount_eur, total_seconds=elapsed, log=log)


def _parse_offer_response(
    response: AgentRunResponse,
    *,
    log: logging.Logger,
) -> OfferAgentResponse | OfferSendFailure:
    """Walk the TinyFish ``response.result`` into :class:`OfferAgentResponse`."""
    if response.result is None:
        log.error("tinyfish_offer_empty_result", extra={"run_id": response.run_id})
        return OfferSendFailure(
            reason=OfferFailureReason.ui_check_failed,
            ctx={"run_id": response.run_id, "detail": "agent returned no result"},
        )
    try:
        return OfferAgentResponse.model_validate(response.result)
    except ValidationError as exc:
        log.exception(
            "tinyfish_offer_response_schema_drift",
            extra={"run_id": response.run_id, "errors": str(exc)},
        )
        return OfferSendFailure(
            reason=OfferFailureReason.marketplace_error,
            ctx={
                "run_id": response.run_id,
                "detail": "agent response failed schema validation",
            },
        )


def _payload_to_offer_result(
    payload: OfferAgentResponse,
    *,
    amount_eur: Decimal,
    total_seconds: int,
    log: logging.Logger,
) -> OfferResult:
    """Translate the parsed agent payload into an :class:`OfferResult`.

    The exact-amount invariant is re-checked at the boundary: a success
    whose echoed amount differs from the bounded amount is a UI failure,
    never a success — the agent sent (or claims to have sent) a number
    we did not authorise.
    """
    if payload.outcome != "success":
        reason = _AGENT_OUTCOME_TO_REASON[payload.outcome]
        ctx: dict[str, Any] = {}
        if payload.detail:
            ctx["detail"] = payload.detail
        if payload.missing:
            ctx["missing"] = list(payload.missing)
        if payload.platform_remaining is not None:
            ctx["platform_remaining"] = payload.platform_remaining
        return OfferSendFailure(reason=reason, ctx=ctx)

    echoed = _coerce_amount(payload.offered_eur)
    if echoed is None or echoed != amount_eur:
        log.error(
            "tinyfish_offer_amount_echo_mismatch",
            extra={"expected": str(amount_eur), "echoed": payload.offered_eur},
        )
        return OfferSendFailure(
            reason=OfferFailureReason.ui_check_failed,
            ctx={
                "detail": "agent claimed success with a different amount than authorised",
                "expected": str(amount_eur),
                "echoed": payload.offered_eur,
            },
        )

    log.info(
        "tinyfish_offer_sent",
        extra={
            "offered_eur": str(amount_eur),
            "platform_remaining": payload.platform_remaining,
            "total_seconds": total_seconds,
        },
    )
    return OfferSuccess(
        offered_eur=amount_eur,
        screenshot_url=payload.screenshot_url,
        platform_remaining=payload.platform_remaining,
        total_seconds=total_seconds,
    )


def _coerce_amount(raw: str | None) -> Decimal | None:
    """Parse the echoed amount; None on garbage."""
    if not raw:
        return None
    cleaned = raw.strip().replace(",", ".").removesuffix(".EUR").removesuffix("€").strip()
    try:
        value = Decimal(cleaned)
    except (ArithmeticError, ValueError):
        return None
    if value <= 0:
        return None
    return value


__all__ = ["OFFER_OUTPUT_CONTRACT", "OfferAgentResponse", "WallapopOfferFlow", "render_offer_goal"]
