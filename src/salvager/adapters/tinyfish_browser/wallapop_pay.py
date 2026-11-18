"""Wallapop Pay buy flow — Story 5.3 (FR25 / FR30).

Drives Wallapop's in-app **Wallapop Pay** checkout through TinyFish.
Wallapop Pay is the *only* rail this flow ever touches; Story 5.14's
payment-rail lint enforces that structurally by walking every file in
this package.

The flow operates the marketplace's existing UI — the operator's
already-authenticated session navigates to the listing URL, asserts the
expected elements are present, clicks the Wallapop-Pay buy button,
awaits the confirmation page, captures a screenshot and the receipt
ID, and returns a typed :class:`BuyResult`. Every uncertainty
(missing button, missing confirmation, missing screenshot) maps to a
``BuyFailure`` — never a silent success.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from pydantic import SecretStr
from tinyfish import AsyncTinyFish

from salvager.adapters.tinyfish_browser._runtime import (
    DEFAULT_MAX_DURATION_S,
    DEFAULT_MAX_STEPS,
    build_client,
    execute_buy_via_tinyfish,
    render_buy_goal,
)
from salvager.domain.errors import BuyFailureReason
from salvager.domain.listing import Listing
from salvager.interfaces.browser_session import (
    BrowserSession,
    BuyFailure,
    BuyResult,
)
from salvager.observability.logging import get_logger

#: The Wallapop-side payment-method label. Wallapop Pay is the rail —
#: anything else surfaces as ``BuyFailure(reason=payment_rail_unavailable)``
#: from the agent goal's UI-check step.
_PAYMENT_METHOD: Final[str] = "wallapop_pay"

#: The buy-flow agent goal. The agent receives the goal text + the
#: per-listing URL and drives the 9-step contract end-to-end.
_WALLAPOP_BUY_GOAL: Final[str] = (
    "Open the Wallapop listing page and complete a purchase via the in-app "
    "Wallapop Pay checkout. Use ONLY the Wallapop Pay button shown on the "
    "listing — do NOT initiate any other payment method.\n"
    "\n"
    "Step-by-step:\n"
    "1. Navigate to the listing URL (the operator's existing Wallapop session "
    "   is loaded).\n"
    "2. Assert these elements are present on the listing page: the visible "
    '   "Buy" button (the Wallapop-Pay one — labeled "Comprar" with the '
    "   Wallapop-Pay logo), the price field, and the seller block.\n"
    "3. If ANY of those elements is missing, ABORT before clicking — return "
    '   {"outcome": "missing_element", "missing": [list of the missing names]}.\n'
    "4. Click the Wallapop Pay buy button.\n"
    "5. Await the confirmation page (it shows a receipt code and the final "
    "   price). Budget: 60 s.\n"
    "6. If the confirmation page does not load within the budget, return "
    '   {"outcome": "timeout", "detail": "confirmation page did not load"}.\n'
    "7. Capture a full-page screenshot of the confirmation. Persist it and "
    "   record its URL in `screenshot_url`.\n"
    "8. If the screenshot capture FAILED but the confirmation page is "
    '   visible, return {"outcome": "screenshot_missing", '
    '"receipt_id": "<the receipt code>"}.\n'
    "9. Extract the receipt code and the final paid price (parse as decimal "
    '   euros, e.g. "55.00"). Return '
    '{"outcome": "success", "price_paid_eur": "...", '
    '"receipt_id": "...", "screenshot_url": "..."}.\n'
    "\n"
    "If the marketplace shows an error page, returns a 4xx/5xx, or the "
    "listing has already been sold, return "
    '{"outcome": "marketplace_error", "detail": "<one-line summary>"}.\n'
)


class WallapopPayFlow(BrowserSession):
    """Concrete :class:`BrowserSession` for Wallapop Pay."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        client: AsyncTinyFish | None = None,
        max_duration_s: int = DEFAULT_MAX_DURATION_S,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        """Build the flow.

        ``client`` is dependency-injected for unit tests so the suite
        runs without an API key. Production leaves it ``None`` and we
        construct an :class:`AsyncTinyFish` from the operator's key
        (the unmask happens at construction and nowhere else).
        """
        self._owned_client = client is None
        if client is None:
            client = build_client(api_key)
        self._client = client
        self._max_duration_s = max_duration_s
        self._max_steps = max_steps
        self._log = get_logger("adapter.tinyfish_browser.wallapop_pay")

    async def close(self) -> None:
        """Close the underlying TinyFish client. Idempotent."""
        if self._owned_client:
            await self._client.close()

    async def execute_buy(self, listing: Listing, max_price_eur: Decimal) -> BuyResult:
        if listing.marketplace != "wallapop":
            return BuyFailure(
                reason=BuyFailureReason.marketplace_error,
                ctx={
                    "detail": (
                        f"WallapopPayFlow refuses {listing.marketplace} listings — "
                        "wrong marketplace"
                    ),
                    "marketplace": listing.marketplace,
                },
            )
        goal = render_buy_goal(_WALLAPOP_BUY_GOAL, max_price_eur=max_price_eur)
        return await execute_buy_via_tinyfish(
            self._client,
            goal=goal,
            url=str(listing.url),
            payment_method="wallapop_pay",
            max_duration_s=self._max_duration_s,
            max_steps=self._max_steps,
            log=self._log,
        )


__all__ = ["WallapopPayFlow"]
