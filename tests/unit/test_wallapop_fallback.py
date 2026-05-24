"""Tests for the Wallapop two-path orchestrator — Story 3.6 + Story 4.3.

Both fetchers are mocked via fake :class:`PageFetcher` implementations
that record calls and return / raise preloaded values. Degradation
reporting is captured by a fake :class:`Reporter` — the orchestrator
no longer logs operational events ad-hoc; it fans them through the
reporter (Story 4.3).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from salvager.domain.alert import EventName, Severity
from salvager.domain.errors import (
    TinyFishAuthFailed,
    TinyFishRateLimited,
    TinyFishUnavailable,
    WallapopApiError,
    WallapopSchemaDrift,
    WallapopSessionExpired,
)
from salvager.domain.listing import Listing, SearchQuery
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.orchestration.wallapop_fallback import (
    SOURCE_API,
    WallapopFallbackFetcher,
    WallapopHealth,
    wallapop_two_path_fetch,
)

# ─────────────────────────────────────────────────────────────────────────
# Fixtures + fakes
# ─────────────────────────────────────────────────────────────────────────


def _listing(listing_id: str = "abc") -> Listing:
    return Listing(
        listing_id=listing_id,
        marketplace="wallapop",
        url=f"https://es.wallapop.com/item/{listing_id}",
        title="WD Red Plus 4TB",
        description="ok",
        price_eur=Decimal("55.00"),
        location="Madrid",
        fetched_at=datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC),
    )


def _query() -> SearchQuery:
    return SearchQuery(
        keyword="wd red plus 4tb",
        marketplace="wallapop",
        max_price_eur=Decimal("70"),
    )


class _FakeFetcher(PageFetcher):
    """Records every call. Returns / raises preloaded values."""

    def __init__(self) -> None:
        self.search_calls: list[SearchQuery] = []
        self.search_response: list[Listing] | BaseException = []

    async def search(self, query: SearchQuery) -> list[Listing]:
        self.search_calls.append(query)
        if isinstance(self.search_response, BaseException):
            raise self.search_response
        return self.search_response

    async def fetch(self, listing_url: str) -> Listing:
        raise AssertionError("orchestrator should not call fetch()")


class _FakeReporter:
    """Records every ``report()`` call — the Story 4.3 fan-out seam."""

    def __init__(self) -> None:
        self.calls: list[tuple[Severity, EventName, dict[str, Any]]] = []

    async def report(
        self,
        severity: Severity,
        event: EventName,
        ctx: Mapping[str, Any],
    ) -> None:
        self.calls.append((severity, event, dict(ctx)))

    def events(self) -> list[EventName]:
        return [event for _, event, _ in self.calls]

    def ctx_for(self, event: EventName) -> dict[str, Any]:
        for _, ev, ctx in self.calls:
            if ev is event:
                return ctx
        raise AssertionError(f"no report() call for {event}")


def _records(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


# ─────────────────────────────────────────────────────────────────────────
# Happy path — API succeeds, TinyFish never called, nothing reported
# ─────────────────────────────────────────────────────────────────────────


async def test_api_success_returns_results_and_skips_tinyfish(
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = _FakeFetcher()
    api.search_response = [_listing("a"), _listing("b")]
    tinyfish = _FakeFetcher()
    health = WallapopHealth()
    reporter = _FakeReporter()

    listings = await wallapop_two_path_fetch(
        _query(),
        api_fetcher=api,
        tinyfish_fetcher=tinyfish,
        health=health,
        reporter=reporter,
    )

    assert [listing.listing_id for listing in listings] == ["a", "b"]
    assert len(api.search_calls) == 1
    assert tinyfish.search_calls == []
    # A clean success reports nothing.
    assert reporter.calls == []

    success = [
        r for r in _records(capsys.readouterr().out) if r["event"] == "wallapop_path_success"
    ]
    assert success and success[0]["source"] == SOURCE_API
    assert success[0]["result_count"] == 2


# ─────────────────────────────────────────────────────────────────────────
# API non-session failure → TinyFish takes over, api_degraded reported
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "api_exception",
    [
        WallapopApiError(503, "service unavailable"),
        WallapopSchemaDrift("search_objects[0].price.amount", "missing"),
    ],
)
async def test_api_degrades_falls_back_to_tinyfish(
    api_exception: Exception,
) -> None:
    api = _FakeFetcher()
    api.search_response = api_exception
    tinyfish = _FakeFetcher()
    tinyfish.search_response = [_listing("t1")]
    health = WallapopHealth()
    reporter = _FakeReporter()

    listings = await wallapop_two_path_fetch(
        _query(),
        api_fetcher=api,
        tinyfish_fetcher=tinyfish,
        health=health,
        reporter=reporter,
    )

    assert [listing.listing_id for listing in listings] == ["t1"]
    assert len(tinyfish.search_calls) == 1
    # API path remains attempted on the NEXT cycle — only SessionExpired latches it off.
    assert health.api_attempt_enabled() is True

    # api_degraded reported at info severity with the originating error class.
    assert reporter.events() == [EventName.wallapop_api_degraded]
    severity, _, ctx = reporter.calls[0]
    assert severity == "info"
    assert ctx["error_class"] == api_exception.__class__.__name__
    assert ctx["adapter"] == SOURCE_API


# ─────────────────────────────────────────────────────────────────────────
# Session expiry — latch the API path off, fall back this cycle, skip next
# ─────────────────────────────────────────────────────────────────────────


async def test_session_expired_latches_path_off_and_falls_back() -> None:
    api = _FakeFetcher()
    api.search_response = WallapopSessionExpired("401")
    tinyfish = _FakeFetcher()
    tinyfish.search_response = [_listing("t")]
    health = WallapopHealth()
    reporter = _FakeReporter()

    listings = await wallapop_two_path_fetch(
        _query(),
        api_fetcher=api,
        tinyfish_fetcher=tinyfish,
        health=health,
        reporter=reporter,
    )

    assert listings == [_listing("t")]
    assert health.api_attempt_enabled() is False

    assert reporter.events() == [EventName.wallapop_session_expired]
    severity, _, ctx = reporter.calls[0]
    assert severity == "info"
    assert ctx["adapter"] == SOURCE_API
    assert ctx["fallback_path_status"] == "active"


async def test_unhealthy_api_skipped_entirely_on_next_cycle() -> None:
    """After SessionExpired latches the API off, subsequent cycles MUST
    not call api_fetcher.search at all until the operator runs login."""
    api = _FakeFetcher()
    tinyfish = _FakeFetcher()
    tinyfish.search_response = [_listing("t")]
    health = WallapopHealth()
    health.mark_api_session_expired()  # simulate prior cycle's expiry
    reporter = _FakeReporter()

    await wallapop_two_path_fetch(
        _query(),
        api_fetcher=api,
        tinyfish_fetcher=tinyfish,
        health=health,
        reporter=reporter,
    )

    # The cheap path is NOT called — every cycle would otherwise burn an
    # API call (and possibly more bot-detection signal) for nothing.
    assert api.search_calls == []
    assert len(tinyfish.search_calls) == 1


# ─────────────────────────────────────────────────────────────────────────
# Session renewal — only fires AFTER login + a successful API call
# ─────────────────────────────────────────────────────────────────────────


async def test_session_renewed_reports_after_login_and_first_api_success() -> None:
    api = _FakeFetcher()
    tinyfish = _FakeFetcher()
    health = WallapopHealth()
    # Simulate: prior cycle saw 401, login was just run, API will now succeed.
    health.mark_api_session_expired()
    health.mark_api_session_renewed_by_operator()
    api.search_response = [_listing("renewed")]
    reporter = _FakeReporter()

    listings = await wallapop_two_path_fetch(
        _query(),
        api_fetcher=api,
        tinyfish_fetcher=tinyfish,
        health=health,
        reporter=reporter,
    )

    assert [listing.listing_id for listing in listings] == ["renewed"]
    assert reporter.events() == [EventName.wallapop_session_renewed]
    severity, _, ctx = reporter.calls[0]
    assert severity == "info"
    assert ctx["adapter"] == SOURCE_API


async def test_session_renewal_report_is_one_shot() -> None:
    """The renewed report fires exactly once per renewal — not on every
    subsequent successful poll."""
    api = _FakeFetcher()
    tinyfish = _FakeFetcher()
    health = WallapopHealth()
    health.mark_api_session_expired()
    health.mark_api_session_renewed_by_operator()
    api.search_response = [_listing("ok")]
    reporter = _FakeReporter()

    # First successful call consumes the pending-renewal latch + reports.
    await wallapop_two_path_fetch(
        _query(),
        api_fetcher=api,
        tinyfish_fetcher=tinyfish,
        health=health,
        reporter=reporter,
    )
    # Second successful call: latch already consumed → no second report.
    await wallapop_two_path_fetch(
        _query(),
        api_fetcher=api,
        tinyfish_fetcher=tinyfish,
        health=health,
        reporter=reporter,
    )
    assert reporter.events() == [EventName.wallapop_session_renewed]


# ─────────────────────────────────────────────────────────────────────────
# Both paths down — empty result; ⚠️ only from the SECOND consecutive cycle
# ─────────────────────────────────────────────────────────────────────────


async def test_first_both_paths_down_logs_but_does_not_alert(
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = _FakeFetcher()
    api.search_response = WallapopApiError(503, "down")
    tinyfish = _FakeFetcher()
    tinyfish.search_response = TinyFishUnavailable("timeout")
    health = WallapopHealth()
    reporter = _FakeReporter()

    listings = await wallapop_two_path_fetch(
        _query(),
        api_fetcher=api,
        tinyfish_fetcher=tinyfish,
        health=health,
        reporter=reporter,
    )

    assert listings == []
    # One blip: a log line, but NO ⚠️ alert (UX-DR13 "no false ⚠️").
    # The api_degraded fired (info), but NOT both_paths_down.
    assert EventName.wallapop_both_paths_down not in reporter.events()
    records = _records(capsys.readouterr().out)
    down = [r for r in records if r["event"] == "wallapop_both_paths_down"]
    assert down and down[0]["consecutive_failures"] == 1


@pytest.mark.parametrize(
    "tinyfish_exception",
    [
        TinyFishAuthFailed("invalid key"),
        TinyFishRateLimited(retry_after_s=12),
        TinyFishUnavailable("timeout"),
        WallapopSchemaDrift("listings", "bad shape"),
    ],
)
async def test_second_consecutive_both_paths_down_alerts(
    tinyfish_exception: Exception,
) -> None:
    api = _FakeFetcher()
    api.search_response = WallapopApiError(503, "down")
    tinyfish = _FakeFetcher()
    tinyfish.search_response = tinyfish_exception
    health = WallapopHealth()
    reporter = _FakeReporter()

    # Two consecutive cycles, both paths failing.
    for _ in range(2):
        listings = await wallapop_two_path_fetch(
            _query(),
            api_fetcher=api,
            tinyfish_fetcher=tinyfish,
            health=health,
            reporter=reporter,
        )
        assert listings == []

    both_down = [
        (sev, ctx) for sev, ev, ctx in reporter.calls if ev is EventName.wallapop_both_paths_down
    ]
    # Exactly one ⚠️ — fired on the 2nd consecutive failure.
    assert len(both_down) == 1
    severity, ctx = both_down[0]
    assert severity == "warn"
    assert ctx["consecutive_failures"] == 2
    assert ctx["last_error_class"] == tinyfish_exception.__class__.__name__


async def test_success_resets_the_both_paths_down_streak() -> None:
    api = _FakeFetcher()
    tinyfish = _FakeFetcher()
    health = WallapopHealth()
    reporter = _FakeReporter()

    # Cycle 1: both down (count → 1, log only).
    api.search_response = WallapopApiError(503, "down")
    tinyfish.search_response = TinyFishUnavailable("timeout")
    await wallapop_two_path_fetch(
        _query(), api_fetcher=api, tinyfish_fetcher=tinyfish, health=health, reporter=reporter
    )
    # Cycle 2: TinyFish recovers → streak resets.
    tinyfish.search_response = [_listing("ok")]
    await wallapop_two_path_fetch(
        _query(), api_fetcher=api, tinyfish_fetcher=tinyfish, health=health, reporter=reporter
    )
    # Cycle 3: both down again → count is back at 1, still no ⚠️.
    tinyfish.search_response = TinyFishUnavailable("timeout")
    await wallapop_two_path_fetch(
        _query(), api_fetcher=api, tinyfish_fetcher=tinyfish, health=health, reporter=reporter
    )

    assert EventName.wallapop_both_paths_down not in reporter.events()


# ─────────────────────────────────────────────────────────────────────────
# WallapopHealth state machine
# ─────────────────────────────────────────────────────────────────────────


def test_health_initial_state_is_attempt_enabled() -> None:
    assert WallapopHealth().api_attempt_enabled() is True


def test_health_session_expired_disables() -> None:
    health = WallapopHealth()
    health.mark_api_session_expired()
    assert health.api_attempt_enabled() is False


def test_health_renewal_by_operator_enables_and_arms_pending_flag() -> None:
    health = WallapopHealth()
    health.mark_api_session_expired()
    health.mark_api_session_renewed_by_operator()
    assert health.api_attempt_enabled() is True
    assert health.consume_pending_renewal() is True
    # Atomic clear: a second consume returns False.
    assert health.consume_pending_renewal() is False


def test_consume_pending_renewal_is_false_when_no_renewal_action() -> None:
    health = WallapopHealth()
    assert health.consume_pending_renewal() is False


def test_both_down_streak_counter_increments_and_resets() -> None:
    health = WallapopHealth()
    assert health.record_both_paths_down() == 1
    assert health.record_both_paths_down() == 2
    health.reset_failure_streak()
    assert health.record_both_paths_down() == 1


# ─────────────────────────────────────────────────────────────────────────
# WallapopFallbackFetcher — cookie-mtime recovery detection (Story 4.3)
# ─────────────────────────────────────────────────────────────────────────


def _write_cookie(path: Path, mtime: float) -> None:
    path.write_text("# cookies", encoding="utf-8")
    os.utime(path, (mtime, mtime))


async def test_fallback_fetcher_redetects_refreshed_cookie(tmp_path: Path) -> None:
    """After the API path latches off, a rewritten cookie file (operator
    re-ran `login wallapop`) re-enables the API path on the next cycle."""
    cookies_path = tmp_path / "wallapop_cookies.txt"
    _write_cookie(cookies_path, mtime=1_000_000.0)

    api = _FakeFetcher()
    tinyfish = _FakeFetcher()
    reporter = _FakeReporter()
    fetcher = WallapopFallbackFetcher(
        api_fetcher=api,
        tinyfish_fetcher=tinyfish,
        reporter=reporter,
        cookies_path=cookies_path,
    )

    # Cycle 1: API returns 401 → latches off; TinyFish serves the cycle.
    api.search_response = WallapopSessionExpired("401")
    tinyfish.search_response = [_listing("t1")]
    await fetcher.search(_query())
    assert fetcher.health.api_attempt_enabled() is False

    # Operator re-runs `login wallapop` → the cookie file is rewritten.
    _write_cookie(cookies_path, mtime=2_000_000.0)

    # Cycle 2: the fetcher sees the newer mtime, re-enables + re-attempts
    # the API path, and it succeeds → session_renewed reported.
    api.search_response = [_listing("renewed")]
    listings = await fetcher.search(_query())

    assert [listing.listing_id for listing in listings] == ["renewed"]
    assert fetcher.health.api_attempt_enabled() is True
    assert EventName.wallapop_session_expired in reporter.events()
    assert EventName.wallapop_session_renewed in reporter.events()


async def test_fallback_fetcher_does_not_redetect_unchanged_cookie(tmp_path: Path) -> None:
    """A stale cookie file (mtime unchanged) must NOT re-enable the API
    path — that would just burn a doomed API call every cycle."""
    cookies_path = tmp_path / "wallapop_cookies.txt"
    _write_cookie(cookies_path, mtime=1_000_000.0)

    api = _FakeFetcher()
    tinyfish = _FakeFetcher()
    reporter = _FakeReporter()
    fetcher = WallapopFallbackFetcher(
        api_fetcher=api,
        tinyfish_fetcher=tinyfish,
        reporter=reporter,
        cookies_path=cookies_path,
    )

    api.search_response = WallapopSessionExpired("401")
    tinyfish.search_response = [_listing("t1")]
    await fetcher.search(_query())
    assert fetcher.health.api_attempt_enabled() is False

    # Cycle 2: cookie file untouched → API path stays off, TinyFish only.
    api.search_calls.clear()
    tinyfish.search_response = [_listing("t2")]
    await fetcher.search(_query())

    assert fetcher.health.api_attempt_enabled() is False
    assert api.search_calls == []  # the doomed cheap path was not retried


async def test_fallback_fetcher_fetch_is_not_implemented() -> None:
    fetcher = WallapopFallbackFetcher(
        api_fetcher=_FakeFetcher(),
        tinyfish_fetcher=_FakeFetcher(),
        reporter=_FakeReporter(),
    )
    with pytest.raises(NotImplementedError, match="explain"):
        await fetcher.fetch("https://es.wallapop.com/item/abc")


# ─────────────────────────────────────────────────────────────────────────
# Adapter discipline — orchestration stays pure
# ─────────────────────────────────────────────────────────────────────────


def test_wallapop_fallback_imports_stay_within_orchestration_allowlist() -> None:
    """The orchestrator imports only stdlib + domain/interfaces/observability
    + sibling orchestration modules. No adapter package may be imported here —
    composition happens via the PageFetcher port, never via a concrete class."""
    import ast

    source_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "salvager"
        / "orchestration"
        / "wallapop_fallback.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("salvager.adapters"):
                    offenders.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("salvager.adapters"):
                offenders.append(f"from {module} import ...")
    assert not offenders, "orchestration.wallapop_fallback imported an adapter:\n  " + "\n  ".join(
        offenders
    )
