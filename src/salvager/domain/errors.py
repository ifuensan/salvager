"""Marketplace-shaped errors surfaced by adapters — Story 3.4 (NFR-I4).

These exception classes live in :mod:`domain` so the orchestration layer
(``orchestration/``) can catch them without importing any adapter
package. Adapters raise; the two-path Wallapop fallback (Story 3.6) and
the eBay daily-quota guard (Story 3.7) catch and decide.

The names are marketplace-specific because the *reactions* are
marketplace-specific (Wallapop session-expiry triggers a Telegram
operational alert + falls back to the TinyFish path; eBay 4xx triggers
a cadence backoff). Putting them in ``domain/`` keeps adapter imports
out of orchestration, which is the whole point.
"""

from __future__ import annotations

import enum


class MarketplaceError(RuntimeError):
    """Common base for any marketplace-shaped adapter failure."""


# ─────────────────────────────────────────────────────────────────────────
# Wallapop
# ─────────────────────────────────────────────────────────────────────────


class WallapopError(MarketplaceError):
    """Base class for any Wallapop adapter failure."""


class WallapopSessionExpired(WallapopError):
    """The session cookie is no longer valid (HTTP 401 from api.wallapop).

    The orchestration layer reacts by emitting an operational alert
    (``wallapop_session_expired``) and falling back to the TinyFish
    path for the rest of the poll cycle (NFR-R2).
    """


class WallapopApiError(WallapopError):
    """A non-401 4xx or 5xx response from the unofficial API."""

    def __init__(self, status_code: int, body_excerpt: str | None = None) -> None:
        self.status_code = status_code
        self.body_excerpt = body_excerpt
        suffix = f": {body_excerpt}" if body_excerpt else ""
        super().__init__(f"Wallapop API returned HTTP {status_code}{suffix}")


class WallapopSchemaDrift(WallapopError):
    """A 200 response was missing a field the adapter schema declares.

    The path identifies the offending location for the operational log
    (e.g. ``"search_objects[0].price.amount"``); operators see this in
    the structured-log line and know which selector to patch.
    """

    def __init__(self, field_path: str, detail: str | None = None) -> None:
        self.field_path = field_path
        self.detail = detail
        super().__init__(f"Wallapop schema drift at {field_path}{f': {detail}' if detail else ''}")


# ─────────────────────────────────────────────────────────────────────────
# TinyFish (browser-as-a-service used as Wallapop fallback path)
# ─────────────────────────────────────────────────────────────────────────


class TinyFishError(MarketplaceError):
    """Base class for any TinyFish-adapter failure.

    TinyFish errors live in their own hierarchy (not under
    :class:`WallapopError`) because their *causes* and *operator
    actions* differ. A TinyFish-side outage doesn't mean Wallapop is
    down — it means our fallback path is degraded; the operator should
    check the TinyFish dashboard rather than re-logging into Wallapop.
    """


class TinyFishAuthFailed(TinyFishError):
    """The TinyFish API key is invalid or revoked (HTTP 401).

    The operator must replace ``TINYFISH_API_KEY`` in ``.env``; the
    daemon stops attempting the TinyFish path until that's done.
    """


class TinyFishRateLimited(TinyFishError):
    """TinyFish rate limit hit, or our own client-side window guard
    refused the call to stay under the published 5 req/min Search limit.

    The poll loop reacts by deferring the fallback to the next cycle
    (graceful degradation) and emitting ``tinyfish_rate_limited`` on
    the operational log.
    """

    def __init__(self, retry_after_s: float | None = None) -> None:
        self.retry_after_s = retry_after_s
        suffix = f" (retry after {retry_after_s}s)" if retry_after_s else ""
        super().__init__(f"TinyFish rate limit exceeded{suffix}")


class TinyFishUnavailable(TinyFishError):
    """TinyFish returned a non-auth, non-rate-limit failure — network
    timeout, 5xx, or a run that finished with ``status != COMPLETED``.

    The poll loop's two-path orchestrator (Story 3.6) catches this and
    falls back to the unofficial-API path if it succeeded, OR fires the
    ``wallapop_both_paths_down`` operational alert if both paths failed
    in the same cycle.
    """


# ─────────────────────────────────────────────────────────────────────────
# eBay
# ─────────────────────────────────────────────────────────────────────────


class EbayError(MarketplaceError):
    """Base class for any eBay adapter failure."""


class EbayAuthFailed(EbayError):
    """OAuth refresh-token endpoint rejected the refresh token (HTTP 401).

    The operator must re-run ``salvager login ebay`` to capture
    fresh tokens; the daemon stops polling eBay until then.
    """


class EbayOAuthExchangeFailed(EbayError):
    """The authorization-code → token exchange was rejected (HTTP 4xx).

    Raised by ``salvager login ebay`` when eBay refuses to swap
    the operator-pasted authorization code for tokens — usually a stale
    or mistyped code. ``ebay_message`` carries eBay's own error text so
    the CLI can surface it verbatim.
    """

    def __init__(self, status_code: int, ebay_message: str) -> None:
        self.status_code = status_code
        self.ebay_message = ebay_message
        super().__init__(f"eBay OAuth exchange failed (HTTP {status_code}): {ebay_message}")


class EbayQuotaExceeded(EbayError):
    """Daily request budget would be exceeded by the next call.

    The poll loop reacts by halving the eBay cadence (2x backoff) until
    the next UTC-midnight quota reset. Operators see the
    ``ebay_quota_breach`` operational alert.
    """

    def __init__(self, used: int, budget: int) -> None:
        self.used = used
        self.budget = budget
        super().__init__(f"eBay daily quota exceeded: {used}/{budget} requests used")


class EbayApiError(EbayError):
    """A non-401 4xx or 5xx response from the eBay API."""

    def __init__(self, status_code: int, body_excerpt: str | None = None) -> None:
        self.status_code = status_code
        self.body_excerpt = body_excerpt
        suffix = f": {body_excerpt}" if body_excerpt else ""
        super().__init__(f"eBay API returned HTTP {status_code}{suffix}")


class EbaySchemaDrift(EbayError):
    """A 200 response was missing a field the adapter schema declares."""

    def __init__(self, field_path: str, detail: str | None = None) -> None:
        self.field_path = field_path
        self.detail = detail
        super().__init__(f"eBay schema drift at {field_path}{f': {detail}' if detail else ''}")


# ─────────────────────────────────────────────────────────────────────────
# LLM provider (Gemini Flash / GPT-4o-mini / Claude Haiku)
# ─────────────────────────────────────────────────────────────────────────


class LlmError(RuntimeError):
    """Base class for any LLM-adapter failure."""


class LlmEvaluationError(LlmError):
    """The LLM returned a malformed or unparseable response.

    The poll loop catches and skips the listing — crucially, the
    listing is NOT marked as seen, so it will be retried on the next
    poll cycle.
    """


class LlmRateLimited(LlmError):
    """The LLM provider returned a rate-limit error (HTTP 429 or equivalent).

    The poll loop reacts by deferring remaining listings to the next
    cycle (graceful degradation) and emitting an operational event
    ``llm_provider_rate_limited``.
    """


# ─────────────────────────────────────────────────────────────────────────
# Telegram bot adapter
# ─────────────────────────────────────────────────────────────────────────


class TelegramError(RuntimeError):
    """Base class for any Telegram adapter failure."""


class TelegramDeliveryFailed(TelegramError):
    """The bot exhausted its retry budget on a transient failure.

    The poll loop catches and continues (NFR-I6 — delivery failure
    must not block polling). The operator sees the
    ``telegram_send_failed`` structured-log line and decides whether
    the issue warrants intervention.
    """


class TelegramConfigError(TelegramError):
    """Telegram returned a non-retryable 4xx — token invalid, chat ID
    wrong, bot kicked from chat, etc. The daemon stops attempting
    deliveries until the operator fixes the configuration."""


class TelegramMessageGone(TelegramError):
    """The edit target no longer exists ("message to edit not found") —
    the operator deleted the alert. Terminal for that alert's watch:
    the caller closes the watch silently and never sends a replacement
    (edit-alerts-on-state-change, design.md Resolved Question 7)."""


# ─────────────────────────────────────────────────────────────────────────
# Phase 2 buy outcomes — Story 5.3 / Story 5.9
# ─────────────────────────────────────────────────────────────────────────


@enum.unique
class BuyFailureReason(enum.Enum):
    """Closed set of reasons a Phase 2 buy can abort or fail.

    Final per Story 5.3 AC. Every variant is renderable by
    :func:`salvager.domain.alert.render_phase2_buy_failure` —
    adding a new variant requires a PRD amendment AND a render-table
    entry, never a silent fall-through.
    """

    listing_gone = "listing_gone"
    reconciliation_tripped = "reconciliation_tripped"
    ui_check_failed = "ui_check_failed"
    circuit_open = "circuit_open"
    missing_element = "missing_element"
    marketplace_error = "marketplace_error"
    timeout = "timeout"
    screenshot_missing = "screenshot_missing"
    payment_rail_unavailable = "payment_rail_unavailable"
