"""eBay.es checkout buy flow — Story 5.3 (FR25 / FR30).

Drives eBay.es' official checkout through TinyFish. The flow uses
eBay's *checkout* page exclusively — Story 5.14's payment-rail lint
guarantees structurally that no off-platform rail ever lands in this
package, and the goal text below names the checkout URL pattern
explicitly so a goal-template drift is auditable.

The buy contract mirrors :mod:`wallapop_pay`: 9 explicit steps, every
uncertainty turns into a :class:`BuyFailure`, never a silent success.
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

#: The eBay-side payment-method label. eBay checkout aggregates the
#: card / wallet rails the operator has registered with their account;
#: from this adapter's point of view it's one rail with one button.
_PAYMENT_METHOD: Final[str] = "ebay_checkout"

#: The buy-flow agent goal. The 9-step structure mirrors the Wallapop
#: flow so the operator can reason about both surfaces uniformly.
_EBAY_BUY_GOAL: Final[str] = (
    "Open the eBay.es listing page and complete a purchase via the official "
    "eBay checkout flow. Use ONLY the standard eBay checkout — do NOT "
    "initiate any off-platform payment.\n"
    "\n"
    "Step-by-step:\n"
    "1. Navigate to the listing URL (the operator's existing eBay session "
    "   is loaded).\n"
    '2. Assert these elements are present: the "Buy It Now" button, the '
    "   price field, and the seller block.\n"
    "3. If ANY of those elements is missing, ABORT before clicking — return "
    '   {"outcome": "missing_element", "missing": [list of the missing names]}.\n'
    '4. Click "Buy It Now". The page transitions to the eBay checkout URL '
    "   under https://www.ebay.es/checkout/ — verify that prefix before "
    "   proceeding (if it is anywhere else, abort with marketplace_error).\n"
    "5. Confirm the order on the checkout page (do not edit shipping or "
    "   payment — the operator has them set up; we only press the final "
    '   "Confirm and pay" button).\n'
    "6. Await the order-confirmation page. Budget: 60 s. If it does not "
    '   load, return {"outcome": "timeout", "detail": "confirmation page '
    'did not load"}.\n'
    "7. Capture a full-page screenshot of the confirmation. Persist it and "
    "   record its URL in `screenshot_url`.\n"
    "8. If the screenshot capture FAILED but the confirmation page is "
    '   visible, return {"outcome": "screenshot_missing", '
    '"receipt_id": "<the order number>"}.\n'
    "9. Extract the order number (eBay calls it the order confirmation "
    "   number) and the final paid price (parse as decimal euros). Return "
    '   {"outcome": "success", "price_paid_eur": "...", '
    '"receipt_id": "...", "screenshot_url": "..."}.\n'
    "\n"
    "If the marketplace returns a 4xx/5xx or the listing has ended, return "
    '{"outcome": "marketplace_error", "detail": "<one-line summary>"}.\n'
)


class EbayCheckoutFlow(BrowserSession):
    """Concrete :class:`BrowserSession` for eBay.es checkout."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        client: AsyncTinyFish | None = None,
        max_duration_s: int = DEFAULT_MAX_DURATION_S,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        self._owned_client = client is None
        if client is None:
            client = build_client(api_key)
        self._client = client
        self._max_duration_s = max_duration_s
        self._max_steps = max_steps
        self._log = get_logger("adapter.tinyfish_browser.ebay_checkout")

    async def close(self) -> None:
        """Close the underlying TinyFish client. Idempotent."""
        if self._owned_client:
            await self._client.close()

    async def execute_buy(self, listing: Listing, max_price_eur: Decimal) -> BuyResult:
        if listing.marketplace != "ebay":
            return BuyFailure(
                reason=BuyFailureReason.marketplace_error,
                ctx={
                    "detail": (
                        f"EbayCheckoutFlow refuses {listing.marketplace} listings — "
                        "wrong marketplace"
                    ),
                    "marketplace": listing.marketplace,
                },
            )
        goal = render_buy_goal(_EBAY_BUY_GOAL, max_price_eur=max_price_eur)
        return await execute_buy_via_tinyfish(
            self._client,
            goal=goal,
            url=str(listing.url),
            payment_method="ebay_checkout",
            max_duration_s=self._max_duration_s,
            max_steps=self._max_steps,
            log=self._log,
        )


__all__ = ["EbayCheckoutFlow"]
