"""TinyFish-driven buy flows for Phase 2 — Story 5.3.

The package collects the protected-payment-rail buy adapters. Two
flows ship at v1.0:

  - :class:`WallapopPayFlow` — drives Wallapop's in-app Wallapop Pay
    checkout.
  - :class:`EbayCheckoutFlow` — drives eBay.es' official checkout.

The package is the *only* location in the codebase allowed to talk to
TinyFish for buy-driving. Story 5.14's CI lint walks this directory
and fails the build if any file mentions a non-protected payment rail
without the explicit ``# verified by payment_rail_lint`` escape marker.
"""

from salvager.adapters.tinyfish_browser.ebay_checkout import EbayCheckoutFlow
from salvager.adapters.tinyfish_browser.wallapop_offer import WallapopOfferFlow
from salvager.adapters.tinyfish_browser.wallapop_pay import WallapopPayFlow

__all__ = ["EbayCheckoutFlow", "WallapopOfferFlow", "WallapopPayFlow"]
