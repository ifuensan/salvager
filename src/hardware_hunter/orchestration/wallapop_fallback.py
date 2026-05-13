"""Two-path Wallapop orchestrator — Story 3.6 (NFR-R2 + FR12).

The poll loop's Wallapop branch tries the unofficial-API path first.
If it fails on a retryable shape (``WallapopApiError`` /
``WallapopSchemaDrift``) the orchestrator falls back to the TinyFish
path within the same cycle. A 401 cookie-expiry (``WallapopSessionExpired``)
is handled specially: the API path goes ``unhealthy`` and stays
disabled until the operator re-captures the cookie via
``hardware-hunter login wallapop`` (Story 2.9).

When both paths fail, the helper returns an empty list and the
orchestrator emits a ``wallapop_both_paths_down`` operational event.
The poll cycle continues regardless — eBay.es is independent (NFR-R1).

Operational log events emitted
------------------------------
- ``wallapop_path_success`` — every successful fetch with
  ``source: "wallapop_api" | "wallapop_tinyfish"`` and ``result_count``
- ``wallapop_api_degraded`` — API path failed with a non-401 4xx/5xx or
  a schema-drift; TinyFish was used as fallback this cycle
- ``wallapop_session_expired`` — 401 cookie expiry; API path latched
  unhealthy until the operator re-runs login
- ``wallapop_session_renewed`` — first successful API call after the
  operator re-ran login
- ``wallapop_both_paths_down`` — both paths failed; cycle returns []
"""

from __future__ import annotations

from typing import Final

from hardware_hunter.domain.errors import (
    TinyFishError,
    WallapopError,
    WallapopSessionExpired,
)
from hardware_hunter.domain.listing import Listing, SearchQuery
from hardware_hunter.interfaces.page_fetcher import PageFetcher
from hardware_hunter.observability.logging import get_logger

#: Source labels for the success-log line. Locked at v1 — these strings
#: surface in operator-facing health output (Epic 4 ``health`` command).
SOURCE_API: Final[str] = "wallapop_api"
SOURCE_TINYFISH: Final[str] = "wallapop_tinyfish"


class WallapopHealth:
    """Cross-cycle health state for the Wallapop API path.

    The orchestrator queries :meth:`api_attempt_enabled` at the start
    of every poll to decide whether the cheap path is worth trying.
    The login CLI command (Story 2.9) flips the path back on via
    :meth:`mark_api_session_renewed_by_operator`; the orchestrator
    then logs ``wallapop_session_renewed`` the next time the API
    actually succeeds — proving the renewal stuck.

    State lives in memory only. A daemon restart re-enables the API
    path optimistically (because there's no way to know whether the
    cookie is still valid without trying — and that's exactly what the
    cheap path is for).
    """

    def __init__(self) -> None:
        self._api_attempt_enabled = True
        self._pending_renewal_confirmation = False

    def api_attempt_enabled(self) -> bool:
        """``True`` iff the unofficial-API path should be tried this cycle."""
        return self._api_attempt_enabled

    def mark_api_session_expired(self) -> None:
        """Latch the API path off until the operator re-captures cookies."""
        self._api_attempt_enabled = False

    def mark_api_session_renewed_by_operator(self) -> None:
        """Re-enable the API path and arm the renewal-confirmation flag.

        The next successful API call clears the flag and emits
        ``wallapop_session_renewed`` — so the renewal log fires only
        when the renewal actually stuck, not on the bare login action.
        """
        self._api_attempt_enabled = True
        self._pending_renewal_confirmation = True

    def consume_pending_renewal(self) -> bool:
        """Atomically read-and-clear the renewal-confirmation flag.

        The orchestrator calls this on every API success; the orchestrator
        only logs ``wallapop_session_renewed`` when this returns True.
        """
        was_pending = self._pending_renewal_confirmation
        self._pending_renewal_confirmation = False
        return was_pending


# ─────────────────────────────────────────────────────────────────────────
# The orchestrator
# ─────────────────────────────────────────────────────────────────────────


async def wallapop_two_path_fetch(
    query: SearchQuery,
    *,
    api_fetcher: PageFetcher,
    tinyfish_fetcher: PageFetcher,
    health: WallapopHealth,
) -> list[Listing]:
    """Fetch Wallapop listings via the API path first, TinyFish on failure.

    Returns:
        The listings from whichever path succeeded. Empty list when
        both paths fail — the cycle continues regardless.

    The function never raises: every error path is converted into an
    empty result + a structured log entry. The poll loop owns the
    cycle-level error handling (Story 3.14); making this helper raise
    would force every caller to re-implement the same fallback.
    """
    log = get_logger("orchestration.wallapop_fallback")

    if health.api_attempt_enabled():
        try:
            results = await api_fetcher.search(query)
        except WallapopSessionExpired as exc:
            health.mark_api_session_expired()
            log.error(
                "wallapop_session_expired",
                extra={"error_class": exc.__class__.__name__},
            )
            # Fall through to TinyFish for the current cycle.
        except WallapopError as exc:
            log.warning(
                "wallapop_api_degraded",
                extra={"error_class": exc.__class__.__name__},
            )
            # Fall through to TinyFish for the current cycle.
        else:
            if health.consume_pending_renewal():
                log.info("wallapop_session_renewed", extra={})
            log.info(
                "wallapop_path_success",
                extra={"source": SOURCE_API, "result_count": len(results)},
            )
            return results

    try:
        results = await tinyfish_fetcher.search(query)
    except (TinyFishError, WallapopError) as exc:
        log.error(
            "wallapop_both_paths_down",
            extra={
                "tinyfish_error_class": exc.__class__.__name__,
                "api_attempt_enabled": health.api_attempt_enabled(),
            },
        )
        return []

    log.info(
        "wallapop_path_success",
        extra={"source": SOURCE_TINYFISH, "result_count": len(results)},
    )
    return results


class WallapopFallbackFetcher(PageFetcher):
    """:class:`PageFetcher` adaptor over :func:`wallapop_two_path_fetch`.

    The poll loop (Story 3.14) takes a single ``PageFetcher`` per
    marketplace. For Wallapop we need the API → TinyFish fallback
    behaviour to be invisible from the loop's perspective, so we wrap
    the two-path helper in a ``PageFetcher`` and pass that single
    object down. State (``WallapopHealth``) is owned by this instance
    so it survives across cycles.
    """

    def __init__(
        self,
        *,
        api_fetcher: PageFetcher,
        tinyfish_fetcher: PageFetcher,
        health: WallapopHealth | None = None,
    ) -> None:
        self._api_fetcher = api_fetcher
        self._tinyfish_fetcher = tinyfish_fetcher
        self._health = health if health is not None else WallapopHealth()

    @property
    def health(self) -> WallapopHealth:
        return self._health

    async def search(self, query: SearchQuery) -> list[Listing]:
        return await wallapop_two_path_fetch(
            query,
            api_fetcher=self._api_fetcher,
            tinyfish_fetcher=self._tinyfish_fetcher,
            health=self._health,
        )

    async def fetch(self, listing_url: str) -> Listing:
        # `explain <url>` (Epic 4) is the only consumer; v0.x defers it.
        raise NotImplementedError(
            "WallapopFallbackFetcher.fetch is not implemented at v0.x — "
            "per-listing fetch lands with the `explain` command in Epic 4."
        )


__all__ = [
    "SOURCE_API",
    "SOURCE_TINYFISH",
    "WallapopFallbackFetcher",
    "WallapopHealth",
    "wallapop_two_path_fetch",
]
