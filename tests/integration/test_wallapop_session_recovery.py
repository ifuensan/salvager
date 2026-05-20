"""End-to-end Wallapop session-expiry recovery — Story 4.3 (FR12/NFR-R3/R4).

Drives the full Story 3.6 + 4.3 path with mocked adapters but the REAL
:class:`DegradationReporter` + :class:`WallapopFallbackFetcher` +
:class:`WallapopHealth`:

    cycle 1: API ok                      → listings delivered, no alerts
    cycle 2: API 401 → TinyFish serves   → info session_expired, listings still delivered
    cycle 3: operator re-ran login       → API re-attempted, succeeds
                                         → info session_renewed

The assertion matrix mirrors the Story 4.3 AC:

    - exactly two operational alerts dispatched, in order
      (session_expired, then session_renewed)
    - no listing alerts lost: every cycle returns a non-empty result set,
      including the fallback-window cycle
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from salvager.domain.alert import InlineButton, RenderedAlert
from salvager.domain.errors import WallapopSessionExpired
from salvager.domain.listing import Listing, SearchQuery
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.interfaces.telegram_surface import CallbackHandler, TelegramSurface
from salvager.orchestration.degradation_reporter import DegradationReporter
from salvager.orchestration.health_state import HealthState
from salvager.orchestration.wallapop_fallback import WallapopFallbackFetcher

_T0 = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)


def _listing(listing_id: str) -> Listing:
    return Listing(
        listing_id=listing_id,
        marketplace="wallapop",
        url=f"https://es.wallapop.com/item/{listing_id}",
        title="WD Red Plus 4TB",
        description="ok",
        price_eur=Decimal("55.00"),
        location="Madrid",
        fetched_at=_T0,
    )


def _query() -> SearchQuery:
    return SearchQuery(
        keyword="wd red plus 4tb",
        marketplace="wallapop",
        max_price_eur=Decimal("70"),
    )


class _ScriptedFetcher(PageFetcher):
    """Returns / raises whatever is loaded into ``response`` per call."""

    def __init__(self) -> None:
        self.response: list[Listing] | BaseException = []
        self.search_calls = 0

    async def search(self, query: SearchQuery) -> list[Listing]:
        self.search_calls += 1
        if isinstance(self.response, BaseException):
            raise self.response
        return list(self.response)

    async def fetch(self, listing_url: str) -> Listing:  # pragma: no cover
        raise AssertionError("integration test never calls fetch()")


class _RecordingTelegram(TelegramSurface):
    """Records every operational alert the reporter dispatches."""

    def __init__(self) -> None:
        self.sends: list[RenderedAlert] = []

    async def send(self, rendered: RenderedAlert) -> int:
        self.sends.append(rendered)
        return 1000 + len(self.sends)

    async def edit_keyboard(
        self,
        message_id: int,
        keyboard: list[list[InlineButton]] | None,
    ) -> None:  # pragma: no cover
        return None

    async def listen_callbacks(self, handler: CallbackHandler) -> None:  # pragma: no cover
        _ = handler


def _write_cookie(path: Path, mtime: float) -> None:
    path.write_text("# cookies", encoding="utf-8")
    os.utime(path, (mtime, mtime))


async def test_full_session_expiry_recovery_cycle(tmp_path: Path) -> None:
    cookies_path = tmp_path / "wallapop_cookies.txt"
    _write_cookie(cookies_path, mtime=1_000_000.0)

    api = _ScriptedFetcher()
    tinyfish = _ScriptedFetcher()
    telegram = _RecordingTelegram()
    health_state = HealthState()
    reporter = DegradationReporter(
        telegram_surface=telegram,
        health_state=health_state,
        # Disable dedup — distinct events anyway, but keep the test
        # independent of any wall-clock window.
        dedup_window_seconds=0,
    )
    fetcher = WallapopFallbackFetcher(
        api_fetcher=api,
        tinyfish_fetcher=tinyfish,
        reporter=reporter,
        cookies_path=cookies_path,
    )

    # ── Cycle 1: API healthy ─────────────────────────────────────────
    api.response = [_listing("a1"), _listing("a2")]
    cycle1 = await fetcher.search(_query())
    assert [listing.listing_id for listing in cycle1] == ["a1", "a2"]
    assert telegram.sends == []  # a clean cycle is silent

    # ── Cycle 2: API 401 → TinyFish serves the cycle ────────────────
    api.response = WallapopSessionExpired("401 unauthorized")
    tinyfish.response = [_listing("t1")]
    cycle2 = await fetcher.search(_query())
    # No listing alerts lost — the TinyFish path delivered this cycle.
    assert [listing.listing_id for listing in cycle2] == ["t1"]
    assert fetcher.health.api_attempt_enabled() is False
    # One operational alert so far: the info-severity "session expired".
    assert len(telegram.sends) == 1
    assert telegram.sends[0].text.startswith("ℹ️ ")  # noqa: RUF001
    assert "Sesión Wallapop expirada" in telegram.sends[0].text

    # ── Operator re-runs `login wallapop` → cookie file rewritten ───
    _write_cookie(cookies_path, mtime=2_000_000.0)

    # ── Cycle 3: fetcher detects the fresh cookie, API re-attempted ─
    api.response = [_listing("a3")]
    cycle3 = await fetcher.search(_query())
    assert [listing.listing_id for listing in cycle3] == ["a3"]
    assert fetcher.health.api_attempt_enabled() is True

    # ── Assertion matrix ─────────────────────────────────────────────
    # Exactly two operational alerts, in order: expired → renewed.
    assert len(telegram.sends) == 2
    assert "Sesión Wallapop expirada" in telegram.sends[0].text
    assert "Sesión Wallapop renovada" in telegram.sends[1].text

    # Health state reflects the recovery: the adapter is no longer degraded.
    assert "wallapop_api" not in health_state.degraded_adapters()
    assert not health_state.is_degraded()
