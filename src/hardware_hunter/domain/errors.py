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
# eBay
# ─────────────────────────────────────────────────────────────────────────


class EbayError(MarketplaceError):
    """Base class for any eBay adapter failure."""


class EbayAuthFailed(EbayError):
    """OAuth refresh-token endpoint rejected the refresh token (HTTP 401).

    The operator must re-run ``hardware-hunter login ebay`` to capture
    fresh tokens; the daemon stops polling eBay until then.
    """


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


# ─────────────────────────────────────────────────────────────────────────
# Hermes MCP client adapter
# ─────────────────────────────────────────────────────────────────────────


class HermesError(RuntimeError):
    """Base class for any Hermes-MCP adapter failure."""


class HermesUnavailable(HermesError):
    """The Hermes subprocess could not be spawned, the stdio transport
    broke, or initialization failed.

    Callers (cache, TinyFish path, subagent dispatcher) treat this as
    a degraded-Hermes signal: the poll cycle drops to its fallback
    (TinyFish path falls back to API; cache misses revert to direct
    LLM calls; subagent fan-out collapses to sequential evaluation).
    """


class HermesToolError(HermesError):
    """A Hermes MCP tool call returned ``isError=True``.

    The MCP spec returns tool errors in-band so an orchestrating LLM
    can recover; for our deterministic-daemon use case we raise so the
    caller doesn't silently consume a malformed result. ``tool_name``
    + the rendered error text are preserved on the instance for the
    operational log line.
    """

    def __init__(self, tool_name: str, message: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"Hermes tool {tool_name!r} returned an error: {message}")
