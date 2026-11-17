"""Two-path Wallapop orchestrator — Story 3.6 + Story 4.3.

The poll loop's Wallapop branch tries the unofficial-API path first.
If it fails on a retryable shape (``WallapopApiError`` /
``WallapopSchemaDrift``) the orchestrator falls back to the TinyFish
path within the same cycle. A 401 cookie-expiry (``WallapopSessionExpired``)
is handled specially: the API path goes ``unhealthy`` and stays
disabled until the operator re-captures the cookie via
``salvager login wallapop`` (Story 2.9).

Story 4.3 — degradation reporting + recovery
--------------------------------------------
Every degraded condition is now reported through the injected
:class:`Reporter` (the single NFR-R3 fan-out), not logged ad-hoc:

- ``WallapopSessionExpired`` → ``warn`` ``wallapop_session_expired``;
  the API path latches off, the cycle continues on TinyFish.
- a non-401 ``WallapopError`` → ``info`` ``wallapop_api_degraded``.
- the first API success after an operator re-login →
  ``info`` ``wallapop_session_renewed``.
- the **second consecutive** fetch with both paths down →
  ``warn`` ``wallapop_both_paths_down`` (one blip is just a log line —
  a ⚠️ for a single transient failure would be a false alarm). It
  fires **once per streak**: every later failure of the same streak is
  a log line only. (Pre-v0.5.1 it fired on every failing fetch — ~2,900
  ⚠️/day during the 2026-11-03 session expiry.)

TinyFish pause
--------------
TinyFish is paid. After :data:`_TINYFISH_PAUSE_THRESHOLD` consecutive
TinyFish failures that reached the network (rate-limit refusals don't
count — they never left the process), the TinyFish path pauses: no
further TinyFish calls until the operator renews the Wallapop cookie or
restarts the daemon. Without it, an expired session meant ~475 failing
paid calls per day for two weeks.

Recovery is automatic: :class:`WallapopFallbackFetcher` watches the
cookie file's mtime. When the operator re-runs ``login wallapop`` the
file is rewritten; the next poll cycle sees the newer mtime and
re-enables the API path without a daemon restart.

When both paths fail, the helper returns an empty list and the poll
cycle continues regardless — eBay.es is independent (NFR-R1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from salvager.domain.alert import EventName
from salvager.domain.errors import (
    TinyFishError,
    TinyFishRateLimited,
    WallapopError,
    WallapopSessionExpired,
)
from salvager.domain.listing import Listing, SearchQuery
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.observability.logging import get_logger
from salvager.orchestration.degradation_reporter import Reporter

#: Source labels for the success-log line. Locked at v1 — these strings
#: surface in operator-facing health output (Epic 4 ``health`` command).
SOURCE_API: Final[str] = "wallapop_api"
SOURCE_TINYFISH: Final[str] = "wallapop_tinyfish"

#: Consecutive both-paths-down cycles before a ⚠️ alert fires. The first
#: failure is just a log line — a single transient blip is not worth a
#: high-priority operational alert (UX-DR13 "no false ⚠️").
_BOTH_DOWN_ALERT_THRESHOLD: Final[int] = 2

#: Consecutive TinyFish failures (excluding rate-limit refusals) before
#: the paid TinyFish path pauses until a cookie renewal or a restart.
_TINYFISH_PAUSE_THRESHOLD: Final[int] = 3


class WallapopHealth:
    """Cross-cycle health state for the Wallapop API path.

    The orchestrator queries :meth:`api_attempt_enabled` at the start
    of every poll to decide whether the cheap path is worth trying.
    :class:`WallapopFallbackFetcher` flips the path back on (via
    :meth:`mark_api_session_renewed_by_operator`) once it observes a
    fresh cookie file; the orchestrator then logs
    ``wallapop_session_renewed`` the next time the API actually
    succeeds — proving the renewal stuck.

    State lives in memory only. A daemon restart re-enables the API
    path optimistically (because there's no way to know whether the
    cookie is still valid without trying — and that's exactly what the
    cheap path is for).
    """

    def __init__(self) -> None:
        self._api_attempt_enabled = True
        self._pending_renewal_confirmation = False
        self._consecutive_both_down = 0
        self._both_down_alert_delivered = False
        self._consecutive_tinyfish_failures = 0

    def api_attempt_enabled(self) -> bool:
        """``True`` iff the unofficial-API path should be tried this cycle."""
        return self._api_attempt_enabled

    def mark_api_session_expired(self) -> None:
        """Latch the API path off until the operator re-captures cookies."""
        self._api_attempt_enabled = False

    def mark_api_session_renewed_by_operator(self) -> None:
        """Re-enable the API path and arm the renewal-confirmation flag.

        The next successful API call clears the flag and emits
        ``wallapop_session_renewed`` — so the renewal alert fires only
        when the renewal actually stuck, not on the bare login action.
        """
        self._api_attempt_enabled = True
        self._pending_renewal_confirmation = True
        # A fresh cookie is the operator's "try again" — unpause TinyFish too.
        self._consecutive_tinyfish_failures = 0

    def consume_pending_renewal(self) -> bool:
        """Atomically read-and-clear the renewal-confirmation flag.

        The orchestrator calls this on every API success; it only
        reports ``wallapop_session_renewed`` when this returns True.
        """
        was_pending = self._pending_renewal_confirmation
        self._pending_renewal_confirmation = False
        return was_pending

    def record_both_paths_down(self) -> int:
        """Increment the consecutive both-paths-down streak; return the new count."""
        self._consecutive_both_down += 1
        return self._consecutive_both_down

    def reset_failure_streak(self) -> None:
        """Clear the both-paths-down streak — called after any successful fetch."""
        self._consecutive_both_down = 0
        self._both_down_alert_delivered = False

    def both_down_alert_delivered(self) -> bool:
        """``True`` once the current streak's ⚠️ actually reached Telegram."""
        return self._both_down_alert_delivered

    def mark_both_down_alert_delivered(self) -> None:
        self._both_down_alert_delivered = True

    def tinyfish_attempt_enabled(self) -> bool:
        """``False`` once TinyFish has failed :data:`_TINYFISH_PAUSE_THRESHOLD` times in a row."""
        return self._consecutive_tinyfish_failures < _TINYFISH_PAUSE_THRESHOLD

    def record_tinyfish_failure(self) -> int:
        """Increment the consecutive TinyFish failure streak; return the new count."""
        self._consecutive_tinyfish_failures += 1
        return self._consecutive_tinyfish_failures

    def reset_tinyfish_failures(self) -> None:
        """Clear the TinyFish failure streak — called after a TinyFish success."""
        self._consecutive_tinyfish_failures = 0


# ─────────────────────────────────────────────────────────────────────────
# The orchestrator
# ─────────────────────────────────────────────────────────────────────────


async def wallapop_two_path_fetch(
    query: SearchQuery,
    *,
    api_fetcher: PageFetcher,
    tinyfish_fetcher: PageFetcher | None,
    health: WallapopHealth,
    reporter: Reporter,
) -> list[Listing]:
    """Fetch Wallapop listings via the API path first, TinyFish on failure.

    Returns:
        The listings from whichever path succeeded. Empty list when
        both paths fail — the cycle continues regardless.

    The function never raises: every error path is converted into an
    empty result plus a degradation report. The poll loop owns the
    cycle-level error handling (Story 3.14); making this helper raise
    would force every caller to re-implement the same fallback.
    """
    log = get_logger("orchestration.wallapop_fallback")

    if health.api_attempt_enabled():
        try:
            results = await api_fetcher.search(query)
        except WallapopSessionExpired as exc:
            health.mark_api_session_expired()
            await reporter.report(
                "warn",
                EventName.wallapop_session_expired,
                {
                    "adapter": SOURCE_API,
                    "fallback_path_status": "active",
                    "error_class": exc.__class__.__name__,
                },
            )
            # Fall through to TinyFish for the current cycle.
        except WallapopError as exc:
            await reporter.report(
                "info",
                EventName.wallapop_api_degraded,
                {"adapter": SOURCE_API, "error_class": exc.__class__.__name__},
            )
            # Fall through to TinyFish for the current cycle.
        else:
            health.reset_failure_streak()
            if health.consume_pending_renewal():
                await reporter.report(
                    "info",
                    EventName.wallapop_session_renewed,
                    {"adapter": SOURCE_API},
                )
            log.info(
                "wallapop_path_success",
                extra={"source": SOURCE_API, "result_count": len(results)},
            )
            return results

    if tinyfish_fetcher is None:
        # TinyFish search disabled by config (the v0.5.2 default): the API
        # path already retried anonymously, so there is nothing left to try.
        await _record_both_paths_down(health, reporter, "TinyFishDisabled")
        return []

    if not health.tinyfish_attempt_enabled():
        await _record_both_paths_down(health, reporter, "TinyFishPaused")
        return []

    try:
        results = await tinyfish_fetcher.search(query)
    except (TinyFishError, WallapopError) as exc:
        # A rate-limit refusal never reached the network (or was the
        # remote saying "slow down") — it says nothing about whether
        # TinyFish works, so it does not count toward the pause.
        if not isinstance(exc, TinyFishRateLimited):
            tinyfish_failures = health.record_tinyfish_failure()
            if tinyfish_failures == _TINYFISH_PAUSE_THRESHOLD:
                log.warning(
                    "wallapop_tinyfish_paused",
                    extra={
                        "consecutive_tinyfish_failures": tinyfish_failures,
                        "last_error_class": exc.__class__.__name__,
                    },
                )
        await _record_both_paths_down(health, reporter, exc.__class__.__name__)
        return []

    health.reset_failure_streak()
    health.reset_tinyfish_failures()
    log.info(
        "wallapop_path_success",
        extra={"source": SOURCE_TINYFISH, "result_count": len(results)},
    )
    return results


async def _record_both_paths_down(
    health: WallapopHealth,
    reporter: Reporter,
    last_error_class: str,
) -> None:
    """Advance the both-paths-down streak; ⚠️ once per streak, retried until delivered.

    A streak is "alerted" only once the reporter confirms Telegram
    delivery: a failed send, or a dedup against a previous streak's ⚠️,
    leaves it un-alerted and the next failure of the streak tries again.
    """
    count = health.record_both_paths_down()
    ctx = {
        "consecutive_failures": count,
        "last_error_class": last_error_class,
        "api_attempt_enabled": health.api_attempt_enabled(),
    }
    if count >= _BOTH_DOWN_ALERT_THRESHOLD and not health.both_down_alert_delivered():
        if await reporter.report("warn", EventName.wallapop_both_paths_down, ctx):
            health.mark_both_down_alert_delivered()
    else:
        # First blip, or a streak already alerted on: log line only — a
        # single transient failure is not worth a ⚠️, and one delivered
        # ⚠️ per streak is enough to get the operator's attention.
        get_logger("orchestration.wallapop_fallback").warning("wallapop_both_paths_down", extra=ctx)


class WallapopFallbackFetcher(PageFetcher):
    """:class:`PageFetcher` adaptor over :func:`wallapop_two_path_fetch`.

    The poll loop (Story 3.14) takes a single ``PageFetcher`` per
    marketplace. For Wallapop we need the API → TinyFish fallback
    behaviour — and the cookie-expiry / recovery lifecycle — to be
    invisible from the loop's perspective, so we wrap the two-path
    helper in a ``PageFetcher`` and pass that single object down.

    Cross-cycle state (:class:`WallapopHealth`) is owned by this
    instance so it survives across cycles. When ``cookies_path`` is
    given, the fetcher also watches that file's mtime: after the API
    path latches off, a rewrite of the cookie file (an operator
    re-running ``login wallapop``) is detected on the next cycle and
    re-enables the API path — no daemon restart needed.
    """

    def __init__(
        self,
        *,
        api_fetcher: PageFetcher,
        tinyfish_fetcher: PageFetcher | None,
        reporter: Reporter,
        cookies_path: str | Path | None = None,
        health: WallapopHealth | None = None,
    ) -> None:
        self._api_fetcher = api_fetcher
        self._tinyfish_fetcher = tinyfish_fetcher
        self._reporter = reporter
        self._cookies_path = Path(cookies_path) if cookies_path is not None else None
        self._health = health if health is not None else WallapopHealth()
        #: The cookie file's mtime captured at the moment the API path
        #: latched off. A later mtime means the operator re-logged in.
        self._cookie_mtime_at_expiry: float | None = None
        self._log = get_logger("orchestration.wallapop_fallback")

    @property
    def health(self) -> WallapopHealth:
        return self._health

    async def search(self, query: SearchQuery) -> list[Listing]:
        if not self._health.api_attempt_enabled():
            self._maybe_redetect_cookie()
        # Read AFTER redetection: a renewed cookie re-enables the API, and
        # if that renewed cookie is rejected too, this cycle must snapshot
        # it as the new expired baseline so the next renewal is detected.
        api_was_enabled = self._health.api_attempt_enabled()
        # Read the cookie mtime BEFORE the fetch: the expiry report and the
        # TinyFish fallback can take minutes, and a cookie the operator
        # renews meanwhile must not be mistaken for the expired one.
        mtime_before_fetch = self._current_cookie_mtime()

        results = await wallapop_two_path_fetch(
            query,
            api_fetcher=self._api_fetcher,
            tinyfish_fetcher=self._tinyfish_fetcher,
            health=self._health,
            reporter=self._reporter,
        )

        # The API path latched off *this* cycle — snapshot the stale
        # cookie's mtime so a later rewrite (operator re-login) is
        # detectable on the next cycle.
        if api_was_enabled and not self._health.api_attempt_enabled():
            self._cookie_mtime_at_expiry = mtime_before_fetch

        return results

    async def fetch(self, listing_url: str) -> Listing:
        # `explain <url>` (Epic 4) is the only consumer; v0.x defers it.
        raise NotImplementedError(
            "WallapopFallbackFetcher.fetch is not implemented at v0.x — "
            "per-listing fetch lands with the `explain` command in Epic 4."
        )

    def _current_cookie_mtime(self) -> float | None:
        if self._cookies_path is None or not self._cookies_path.exists():
            return None
        return self._cookies_path.stat().st_mtime

    def _maybe_redetect_cookie(self) -> None:
        """Re-enable the API path if the cookie file was rewritten.

        Called at the start of a cycle while the API path is latched
        off. A cookie-file mtime newer than the one captured at expiry
        means the operator re-ran ``login wallapop`` — so we arm the
        renewal and let the next API call confirm it stuck.
        """
        current = self._current_cookie_mtime()
        if current is None:
            return
        if self._cookie_mtime_at_expiry is None:
            # No baseline (e.g. health was pre-disabled out of band) —
            # adopt the current mtime so a *future* rewrite is detected.
            self._cookie_mtime_at_expiry = current
            return
        if current > self._cookie_mtime_at_expiry:
            self._log.info(
                "wallapop_cookie_refresh_detected",
                extra={"cookies_path": str(self._cookies_path)},
            )
            self._health.mark_api_session_renewed_by_operator()
            self._cookie_mtime_at_expiry = None


__all__ = [
    "SOURCE_API",
    "SOURCE_TINYFISH",
    "WallapopFallbackFetcher",
    "WallapopHealth",
    "wallapop_two_path_fetch",
]
