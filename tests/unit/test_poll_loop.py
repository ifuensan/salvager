"""Unit tests for the per-cycle pipeline — Story 3.14.

Every port (PageFetcher, ListingEvaluator, Store, TelegramSurface) is
mocked via a small fake that records calls + returns / raises preloaded
values. The tests focus on the orchestration logic, not the adapters.
The end-to-end test in :mod:`test_poll_loop_e2e` drives the same
pipeline against a representative listing stream.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from salvager.domain.alert import AlertSnapshot, InlineButton, RenderedAlert
from salvager.domain.audit import CallbackAudit, TapEventAudit, TransactionAudit
from salvager.domain.errors import (
    LlmEvaluationError,
    LlmRateLimited,
    TelegramDeliveryFailed,
)
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing, SearchQuery
from salvager.domain.wishlist import Wishlist, WishlistEntry
from salvager.interfaces.listing_evaluator import ListingEvaluator
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.interfaces.store import EntryKey, Store
from salvager.interfaces.telegram_surface import (
    CallbackHandler,
    TelegramSurface,
)
from salvager.orchestration.poll_loop import (
    PollCycleSummary,
    run_poll_cycle,
)

# ─────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────

_T0 = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
_FIXED_UUID = UUID("12345678-1234-1234-1234-123456789abc")


def _utc_t0() -> datetime:
    return _T0


def _fixed_uuid() -> UUID:
    return _FIXED_UUID


def _entry(
    *,
    manufacturer: str = "Western Digital",
    model: str = "WD Red Plus 4TB",
    ref: str = "WD40EFPX",
    confidence_threshold: str = "medium",
    keywords: list[str] | None = None,
    max_price_solo: Decimal = Decimal("70.00"),
) -> WishlistEntry:
    return WishlistEntry.model_validate(
        {
            "manufacturer": manufacturer,
            "model": model,
            "ref": ref,
            "type": "hdd",
            "keywords": keywords if keywords is not None else ["wd red plus 4tb"],
            "container_keywords": [],
            "max_price_solo": max_price_solo,
            "confidence_threshold": confidence_threshold,
        }
    )


def _wishlist(*entries: WishlistEntry) -> Wishlist:
    return Wishlist(entries=list(entries))


def _listing(
    listing_id: str = "abc123",
    *,
    title: str = "WD Red Plus 4TB",
    price: Decimal = Decimal("55.00"),
    is_reserved: bool = False,
) -> Listing:
    return Listing(
        listing_id=listing_id,
        marketplace="wallapop",
        url=f"https://es.wallapop.com/item/{listing_id}",
        title=title,
        description="ok",
        price_eur=price,
        location="Madrid",
        photo_urls=["https://cdn/photo.jpg"],
        fetched_at=_T0,
        is_reserved=is_reserved,
    )


def _evaluation(
    listing_id: str = "abc123",
    *,
    confidence: str = "high",
    is_container: bool = False,
) -> ListingEvaluation:
    return ListingEvaluation(
        listing_id=listing_id,
        entry_key=("Western Digital", "WD Red Plus 4TB", "WD40EFPX"),
        confidence=confidence,  # type: ignore[arg-type]
        one_line_take="Strong match.",
        is_container=is_container,
        evaluated_at=_T0,
    )


# ─────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────


class _FakeFetcher(PageFetcher):
    def __init__(self, *, response: list[Listing] | BaseException | None = None) -> None:
        if response is None:
            response = []
        self.search_calls: list[SearchQuery] = []
        self.response = response

    async def search(self, query: SearchQuery) -> list[Listing]:
        self.search_calls.append(query)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    async def fetch(self, listing_url: str) -> Listing:
        raise AssertionError("poll loop should not call fetch()")


class _FakeEvaluator(ListingEvaluator):
    def __init__(
        self,
        *,
        per_listing_response: dict[str, ListingEvaluation | BaseException] | None = None,
    ) -> None:
        self.calls: list[tuple[str, EntryKey]] = []
        self._per_listing = per_listing_response or {}

    async def evaluate(self, listing: Listing, entry: WishlistEntry) -> ListingEvaluation:
        self.calls.append((listing.listing_id, entry.entry_key))
        resp = self._per_listing.get(listing.listing_id)
        if resp is None:
            # Default: high-confidence match.
            return _evaluation(listing.listing_id, confidence="high")
        if isinstance(resp, BaseException):
            raise resp
        return resp


class _FakeStore(Store):
    def __init__(self) -> None:
        self.seen: set[tuple[str, EntryKey]] = set()
        self.snapshots: list[AlertSnapshot] = []
        self.snooze_until: dict[EntryKey, datetime] = {}
        self.record_seen_calls: list[tuple[str, EntryKey]] = []
        self.match_fired_calls: list[tuple[str, bool]] = []
        self.record_alert_calls: list[AlertSnapshot] = []
        self.meta: dict[str, str] = {}

    async def is_seen(self, listing_id: str, entry_key: EntryKey) -> bool:
        return (listing_id, entry_key) in self.seen

    async def record_seen(
        self,
        listing: Listing,
        entry_key: EntryKey,
        *,
        match_fired: bool = False,
    ) -> None:
        self.record_seen_calls.append((listing.listing_id, entry_key))
        self.seen.add((listing.listing_id, entry_key))
        self.match_fired_calls.append((listing.listing_id, match_fired))

    async def get_snooze_until(self, entry_key: EntryKey) -> datetime | None:
        return self.snooze_until.get(entry_key)

    async def set_snooze(self, entry_key: EntryKey, until: datetime) -> None:
        self.snooze_until[entry_key] = until

    async def record_alert_snapshot(self, snapshot: AlertSnapshot) -> int:
        self.record_alert_calls.append(snapshot)
        self.snapshots.append(snapshot)
        return len(self.snapshots)

    async def get_alert_snapshot(self, audit_id: int) -> AlertSnapshot | None:
        return None

    async def get_alert_snapshot_by_alert_id(self, alert_id: UUID) -> AlertSnapshot | None:
        for snapshot in self.snapshots:
            if snapshot.alert_id == alert_id:
                return snapshot
        return None

    async def record_callback(self, callback: CallbackAudit) -> None:
        return None

    async def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value

    async def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    async def get_all_meta(self) -> dict[str, str]:
        return dict(self.meta)

    async def record_tap_event(self, tap: TapEventAudit) -> None:
        raise AssertionError("Phase 2 audit should not run in Phase 1 cycle")

    async def record_transaction(self, transaction: TransactionAudit) -> None:
        raise AssertionError("Phase 2 audit should not run in Phase 1 cycle")


class _FakeTelegram(TelegramSurface):
    def __init__(
        self, *, send_response: int | BaseException = 1, send_responses: list[Any] | None = None
    ) -> None:
        self.sends: list[RenderedAlert] = []
        self._fixed_response = send_response
        self._stream = list(send_responses) if send_responses else None

    async def send(self, rendered: RenderedAlert) -> int:
        self.sends.append(rendered)
        if self._stream is not None:
            response = self._stream.pop(0)
            if isinstance(response, BaseException):
                raise response
            return int(response)
        if isinstance(self._fixed_response, BaseException):
            raise self._fixed_response
        return self._fixed_response

    async def edit_keyboard(
        self,
        message_id: int,
        keyboard: list[list[InlineButton]] | None,
    ) -> None:
        return None

    async def listen_callbacks(self, handler: CallbackHandler) -> None:
        _ = handler


def _records(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _make_kwargs(
    *,
    fetcher: _FakeFetcher,
    evaluator: _FakeEvaluator,
    store: _FakeStore,
    telegram: _FakeTelegram,
) -> dict[str, Any]:
    return {
        "fetcher": fetcher,
        "evaluator": evaluator,
        "store": store,
        "telegram": telegram,
        "clock": _utc_t0,
        "new_alert_id": _fixed_uuid,
    }


# ─────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────


async def test_single_entry_single_match_dispatches_one_alert() -> None:
    entry = _entry()
    listings = [_listing("abc123")]
    fetcher = _FakeFetcher(response=listings)
    evaluator = _FakeEvaluator()
    store = _FakeStore()
    telegram = _FakeTelegram()

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )

    assert summary.alerts_sent == 1
    assert summary.result_count == 1
    assert summary.new_count == 1
    assert summary.dropped_count == 0
    assert len(telegram.sends) == 1
    assert len(store.snapshots) == 1
    # Both alert_snapshot AND seen got recorded.
    assert store.snapshots[0].listing.listing_id == "abc123"
    assert ("abc123", entry.entry_key) in store.seen


async def test_below_threshold_listing_marked_seen_and_logged_dropped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    entry = _entry(confidence_threshold="high")
    fetcher = _FakeFetcher(response=[_listing("lo1")])
    evaluator = _FakeEvaluator(
        per_listing_response={"lo1": _evaluation("lo1", confidence="medium")}
    )
    store = _FakeStore()
    telegram = _FakeTelegram()

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )

    assert summary.alerts_sent == 0
    assert summary.dropped_count == 1
    # Marked seen so it doesn't re-evaluate next cycle.
    assert ("lo1", entry.entry_key) in store.seen
    # No alert sent.
    assert telegram.sends == []
    out = capsys.readouterr().out
    records = _records(out)
    dropped = [r for r in records if r["event"] == "listing_dropped_below_threshold"]
    assert dropped and dropped[0]["confidence"] == "medium"


async def test_reserved_listings_skip_eval_record_seen_and_emit_comp_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reserved listings are still useful comps, but they MUST NOT:
    - reach the LLM evaluator (eval cost on dead inventory)
    - trigger a Telegram alert (operator can't buy them)

    They MUST be marked seen so the next cycle doesn't process them
    again, and the cycle SHOULD emit a structured comp-observation
    log line so an operator (or a future renderer) can act on the
    price signal.
    """
    entry = _entry()
    fetcher = _FakeFetcher(
        response=[
            _listing("buyable1"),
            _listing("res1", price=Decimal("80.00"), is_reserved=True),
            _listing("res2", price=Decimal("230.00"), is_reserved=True),
        ]
    )
    evaluator = _FakeEvaluator()
    store = _FakeStore()
    telegram = _FakeTelegram()

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )

    assert summary.result_count == 3
    assert summary.new_count == 3
    assert summary.reserved_count == 2
    # Only the buyable listing reached the evaluator.
    assert [call[0] for call in evaluator.calls] == ["buyable1"]
    # Both reserved are recorded as seen so next cycle doesn't reprocess them.
    assert ("res1", entry.entry_key) in store.seen
    assert ("res2", entry.entry_key) in store.seen
    # Only the buyable triggered an alert (high-confidence default).
    assert summary.alerts_sent == 1
    assert len(telegram.sends) == 1

    out = capsys.readouterr().out
    records = _records(out)
    comp_logs = [r for r in records if r["event"] == "reserved_comps_observed"]
    assert len(comp_logs) == 1
    assert comp_logs[0]["reserved_count"] == 2
    # Set comparison — log emission preserves insertion order but the
    # test only cares which prices got captured, not their order.
    assert set(comp_logs[0]["comp_prices_eur"]) == {"80.00", "230.00"}


async def test_only_reserved_listings_skips_eval_and_no_alerts() -> None:
    """When every candidate is reserved, the evaluator must never run
    and no alert is dispatched. ``new_count`` still counts everyone (it
    is a "fresh sighting" counter, not a "buyable" counter).
    """
    entry = _entry()
    fetcher = _FakeFetcher(
        response=[
            _listing("res1", is_reserved=True),
            _listing("res2", price=Decimal("70.00"), is_reserved=True),
        ]
    )
    evaluator = _FakeEvaluator()
    store = _FakeStore()
    telegram = _FakeTelegram()

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )

    assert evaluator.calls == []
    assert summary.alerts_sent == 0
    assert summary.reserved_count == 2
    assert summary.new_count == 2
    assert telegram.sends == []


async def test_already_seen_listings_are_filtered_before_eval() -> None:
    entry = _entry()
    fetcher = _FakeFetcher(response=[_listing("seen1"), _listing("new1")])
    store = _FakeStore()
    # Pre-populate dedup state for seen1.
    store.seen.add(("seen1", entry.entry_key))
    evaluator = _FakeEvaluator()
    telegram = _FakeTelegram()

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )

    # Eval only ran for the new listing.
    assert [call[0] for call in evaluator.calls] == ["new1"]
    assert summary.result_count == 2
    assert summary.new_count == 1
    assert summary.alerts_sent == 1


# ─────────────────────────────────────────────────────────────────────────
# Snooze gate
# ─────────────────────────────────────────────────────────────────────────


async def test_snoozed_entry_skips_fetch_entirely() -> None:
    entry = _entry()
    fetcher = _FakeFetcher(response=[_listing()])
    evaluator = _FakeEvaluator()
    store = _FakeStore()
    store.snooze_until[entry.entry_key] = _T0 + timedelta(hours=2)
    telegram = _FakeTelegram()

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )

    assert summary.snoozed_entries == 1
    assert summary.result_count == 0
    assert fetcher.search_calls == [], "snoozed entry must not call fetcher.search"


async def test_expired_snooze_does_not_block() -> None:
    entry = _entry()
    fetcher = _FakeFetcher(response=[_listing()])
    evaluator = _FakeEvaluator()
    store = _FakeStore()
    store.snooze_until[entry.entry_key] = _T0 - timedelta(hours=1)  # past
    telegram = _FakeTelegram()

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )

    assert summary.snoozed_entries == 0
    assert summary.alerts_sent == 1


# ─────────────────────────────────────────────────────────────────────────
# Exception isolation
# ─────────────────────────────────────────────────────────────────────────


async def test_fetch_failure_for_one_entry_does_not_kill_cycle() -> None:
    failing = _entry(
        model="Failing 8TB",
        ref="FAIL8",
        keywords=["FAIL-FETCH-MARKER"],
    )
    good = _entry()

    class _FetcherSwitcher(PageFetcher):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def search(self, query: SearchQuery) -> list[Listing]:
            self.calls.append(query.keyword)
            if "FAIL-FETCH-MARKER" in query.keyword:
                raise RuntimeError("transient network error")
            return [_listing()]

        async def fetch(self, listing_url: str) -> Listing:
            raise AssertionError

    fetcher = _FetcherSwitcher()
    evaluator = _FakeEvaluator()
    store = _FakeStore()
    telegram = _FakeTelegram()

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(failing, good),
        fetcher=fetcher,
        evaluator=evaluator,
        store=store,
        telegram=telegram,
        clock=_utc_t0,
        new_alert_id=_fixed_uuid,
    )

    assert summary.errors == 1
    assert summary.alerts_sent == 1
    assert "Western Digital Failing 8TB (FAIL8)" in summary.failed_entries
    # Both entries were attempted.
    assert len(fetcher.calls) == 2


async def test_llm_eval_failure_leaves_listing_unmarked_for_retry() -> None:
    entry = _entry()
    fetcher = _FakeFetcher(response=[_listing("bad"), _listing("good")])
    evaluator = _FakeEvaluator(
        per_listing_response={
            "bad": LlmEvaluationError("malformed"),
            "good": _evaluation("good", confidence="high"),
        }
    )
    store = _FakeStore()
    telegram = _FakeTelegram()

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )

    assert summary.alerts_sent == 1
    # The failed listing is NOT in seen → will retry next cycle.
    assert ("bad", entry.entry_key) not in store.seen
    assert ("good", entry.entry_key) in store.seen


async def test_llm_rate_limited_does_not_propagate() -> None:
    """A rate-limit on one listing's eval is logged like any other
    failure and the loop continues with the other listings."""
    entry = _entry()
    fetcher = _FakeFetcher(response=[_listing("rate"), _listing("ok")])
    evaluator = _FakeEvaluator(
        per_listing_response={
            "rate": LlmRateLimited("429"),
            "ok": _evaluation("ok", confidence="high"),
        }
    )
    store = _FakeStore()
    telegram = _FakeTelegram()

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )

    assert summary.alerts_sent == 1  # 'ok' still went through
    assert ("rate", entry.entry_key) not in store.seen
    assert ("ok", entry.entry_key) in store.seen


async def test_telegram_delivery_failure_does_not_mark_seen() -> None:
    """A TelegramDeliveryFailed leaves the listing un-marked so the
    next cycle retries. The cache (Story 3.10) absorbs the re-eval cost."""
    entry = _entry()
    fetcher = _FakeFetcher(response=[_listing("undeliverable")])
    evaluator = _FakeEvaluator()
    store = _FakeStore()
    telegram = _FakeTelegram(send_response=TelegramDeliveryFailed("network"))

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )

    assert summary.alerts_sent == 0
    assert ("undeliverable", entry.entry_key) not in store.seen
    assert store.snapshots == []


# ─────────────────────────────────────────────────────────────────────────
# Confidence threshold semantics
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "threshold,eval_confidence,expect_alert",
    [
        ("low", "low", True),
        ("low", "medium", True),
        ("low", "high", True),
        ("medium", "low", False),
        ("medium", "medium", True),
        ("medium", "high", True),
        ("high", "low", False),
        ("high", "medium", False),
        ("high", "high", True),
    ],
)
async def test_threshold_matrix(threshold: str, eval_confidence: str, expect_alert: bool) -> None:
    entry = _entry(confidence_threshold=threshold)
    fetcher = _FakeFetcher(response=[_listing("x")])
    evaluator = _FakeEvaluator(
        per_listing_response={"x": _evaluation("x", confidence=eval_confidence)}
    )
    store = _FakeStore()
    telegram = _FakeTelegram()

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )

    assert (summary.alerts_sent == 1) is expect_alert
    # Either alerted (and seen) OR dropped (and seen). The listing is
    # always marked seen exactly once after a successful eval.
    assert ("x", entry.entry_key) in store.seen


# ─────────────────────────────────────────────────────────────────────────
# Structured logging — poll_cycle_complete carries every counter
# ─────────────────────────────────────────────────────────────────────────


async def test_poll_cycle_complete_log_carries_metrics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    entry = _entry(confidence_threshold="high")
    fetcher = _FakeFetcher(
        response=[
            _listing("alert"),
            _listing("drop"),
            _listing("seen"),
        ]
    )
    evaluator = _FakeEvaluator(
        per_listing_response={
            "alert": _evaluation("alert", confidence="high"),
            "drop": _evaluation("drop", confidence="medium"),
        }
    )
    store = _FakeStore()
    store.seen.add(("seen", entry.entry_key))
    telegram = _FakeTelegram()

    await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )
    out = capsys.readouterr().out
    records = _records(out)
    complete = [r for r in records if r["event"] == "poll_cycle_complete"]
    assert complete
    record = complete[0]
    assert record["marketplace"] == "wallapop"
    assert record["result_count"] == 3
    assert record["new_count"] == 2
    assert record["alerts_sent"] == 1
    assert record["dropped_count"] == 1
    assert isinstance(record["latency_ms"], int)


# ─────────────────────────────────────────────────────────────────────────
# Concurrency — semaphore bounds in-flight evaluations
# ─────────────────────────────────────────────────────────────────────────


async def test_concurrent_evaluations_capped_by_semaphore() -> None:
    """Inject a custom evaluator that asserts max in-flight count never
    exceeds the configured ceiling."""
    import asyncio as _asyncio

    cap = 3
    in_flight = 0
    high_water = 0
    lock = _asyncio.Lock()

    class _ConcurrencyProbe(ListingEvaluator):
        async def evaluate(self, listing: Listing, entry: WishlistEntry) -> ListingEvaluation:
            nonlocal in_flight, high_water
            async with lock:
                in_flight += 1
                high_water = max(high_water, in_flight)
            try:
                await _asyncio.sleep(0)  # yield so others can enter
            finally:
                async with lock:
                    in_flight -= 1
            return _evaluation(listing.listing_id, confidence="high")

    entry = _entry()
    listings = [_listing(f"id{n}") for n in range(20)]
    fetcher = _FakeFetcher(response=listings)
    store = _FakeStore()
    telegram = _FakeTelegram(send_responses=list(range(1, 21)))

    await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        fetcher=fetcher,
        evaluator=_ConcurrencyProbe(),
        store=store,
        telegram=telegram,
        max_concurrent_evaluations=cap,
        clock=_utc_t0,
        new_alert_id=_fixed_uuid,
    )

    assert high_water <= cap, f"saw {high_water} in flight; cap was {cap}"
    assert high_water > 0  # actually exercised the fan-out


# ─────────────────────────────────────────────────────────────────────────
# Summary type is a dataclass — operators can inspect / serialize
# ─────────────────────────────────────────────────────────────────────────


def test_summary_initializes_with_marketplace_only() -> None:
    summary = PollCycleSummary(marketplace="ebay")
    assert summary.marketplace == "ebay"
    assert summary.result_count == 0
    assert summary.alerts_sent == 0


# ─────────────────────────────────────────────────────────────────────────
# Phase 2 dispatch — pre-flight gate decides which renderer fires
# ─────────────────────────────────────────────────────────────────────────


from salvager.domain.phase2_audit import Phase2StateSnapshot  # noqa: E402
from salvager.orchestration.phase2_preflight import Phase2Preflight  # noqa: E402


class _StubStateReader:
    def __init__(self, snapshot: Phase2StateSnapshot) -> None:
        self._snapshot = snapshot

    async def read(self) -> Phase2StateSnapshot:
        return self._snapshot


def _phase2_entry() -> WishlistEntry:
    return WishlistEntry.model_validate(
        {
            "manufacturer": "Western Digital",
            "model": "WD Red Plus 4TB",
            "ref": "WD40EFPX",
            "type": "hdd",
            "keywords": ["wd red plus 4tb"],
            "max_price_solo": Decimal("70.00"),
            "confidence_threshold": "medium",
            "phase2": {"enabled": True, "max_price_eur": "60.00"},
        }
    )


def _healthy_state() -> Phase2StateSnapshot:
    return Phase2StateSnapshot(
        globally_disabled=False,
        consecutive_failures=0,
        last_smoke_result="pass",
        last_smoke_at=_T0 - timedelta(hours=2),
    )


async def test_phase2_alert_dispatched_when_preflight_passes() -> None:
    entry = _phase2_entry()
    fetcher = _FakeFetcher(response=[_listing("abc123", price=Decimal("55.00"))])
    evaluator = _FakeEvaluator()
    store = _FakeStore()
    telegram = _FakeTelegram()
    preflight = Phase2Preflight(
        state_reader=_StubStateReader(_healthy_state()),
        circuit_breaker_threshold=3,
        clock=_utc_t0,
    )

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        phase2_preflight=preflight,
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )

    assert summary.alerts_sent == 1
    assert len(store.snapshots) == 1
    persisted = store.snapshots[0]
    assert persisted.phase == "phase2"
    assert persisted.phase2_max_price_eur == Decimal("60.00")
    # The dispatched Telegram message carries the Phase 2 keyboard.
    rendered = telegram.sends[0]
    assert rendered.inline_keyboard is not None
    assert [b.text for b in rendered.inline_keyboard[0]] == [
        "✅ Comprar",
        "❌ Saltar",
        "👁 Ver",
    ]


async def test_phase2_downgrades_silently_when_preflight_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    entry = _phase2_entry()
    fetcher = _FakeFetcher(response=[_listing("abc123", price=Decimal("55.00"))])
    evaluator = _FakeEvaluator()
    store = _FakeStore()
    telegram = _FakeTelegram()
    # Circuit open: gate must downgrade.
    preflight = Phase2Preflight(
        state_reader=_StubStateReader(
            Phase2StateSnapshot(
                globally_disabled=False,
                consecutive_failures=3,
                last_smoke_result="pass",
                last_smoke_at=_T0 - timedelta(hours=2),
            )
        ),
        circuit_breaker_threshold=3,
        clock=_utc_t0,
    )

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        phase2_preflight=preflight,
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )

    assert summary.alerts_sent == 1
    persisted = store.snapshots[0]
    # Silent downgrade — the persisted snapshot is Phase 1, not Phase 2.
    assert persisted.phase == "phase1"
    assert persisted.phase2_max_price_eur is None

    # The downgrade reason is in the structured log.
    records = _records(capsys.readouterr().out)
    downgrades = [r for r in records if r.get("event") == "phase2_alert_downgraded"]
    assert downgrades
    assert downgrades[0]["reason"] == "circuit_breaker_open"


async def test_phase1_path_unchanged_when_no_preflight_supplied() -> None:
    """No preflight passed → Phase 1 path runs even for opted-in entries."""
    entry = _phase2_entry()
    fetcher = _FakeFetcher(response=[_listing("abc123")])
    evaluator = _FakeEvaluator()
    store = _FakeStore()
    telegram = _FakeTelegram()

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        **_make_kwargs(fetcher=fetcher, evaluator=evaluator, store=store, telegram=telegram),
    )

    assert summary.alerts_sent == 1
    persisted = store.snapshots[0]
    assert persisted.phase == "phase1"
    assert persisted.phase2_max_price_eur is None


# ─────────────────────────────────────────────────────────────────────────
# Adapter discipline — poll_loop stays pure
# ─────────────────────────────────────────────────────────────────────────


def test_poll_loop_does_not_import_adapters() -> None:
    """Orchestration composes ports, never concrete adapters."""
    import ast
    from pathlib import Path

    source_path = (
        Path(__file__).resolve().parents[2] / "src" / "salvager" / "orchestration" / "poll_loop.py"
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
    assert not offenders, "poll_loop imported an adapter:\n  " + "\n  ".join(offenders)


# ─────────────────────────────────────────────────────────────────────────
# Keyword fan-out — each wishlist keyword fires its own search; results
# are unioned + de-duped by listing_id. Regression for the prior bug
# where `keywords=["A", "B"]` was joined into a single "A B" query that
# tokenized at the marketplace as AND and matched nothing.
# ─────────────────────────────────────────────────────────────────────────


async def test_fan_out_runs_one_search_per_keyword() -> None:
    entry = _entry(keywords=["Ultrastar 14TB", "WUH721414", "WD 14TB HC530"])
    fetcher = _FakeFetcher(response=[_listing()])
    evaluator = _FakeEvaluator()
    store = _FakeStore()
    telegram = _FakeTelegram()

    await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        fetcher=fetcher,
        evaluator=evaluator,
        store=store,
        telegram=telegram,
        clock=_utc_t0,
        new_alert_id=_fixed_uuid,
    )

    issued = [q.keyword for q in fetcher.search_calls]
    assert issued == ["Ultrastar 14TB", "WUH721414", "WD 14TB HC530"]


async def test_fan_out_dedups_overlapping_listings_by_id() -> None:
    entry = _entry(keywords=["kw1", "kw2"])
    shared = _listing("shared", title="shared")
    unique_to_kw2 = _listing("unique2", title="unique2")

    class _SwitchingFetcher(PageFetcher):
        def __init__(self) -> None:
            self.search_calls: list[SearchQuery] = []

        async def search(self, query: SearchQuery) -> list[Listing]:
            self.search_calls.append(query)
            if query.keyword == "kw1":
                return [shared]
            return [shared, unique_to_kw2]  # `shared` overlaps, must be deduped

        async def fetch(self, listing_url: str) -> Listing:
            raise AssertionError

    fetcher = _SwitchingFetcher()
    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        fetcher=fetcher,
        evaluator=_FakeEvaluator(),
        store=_FakeStore(),
        telegram=_FakeTelegram(),
        clock=_utc_t0,
        new_alert_id=_fixed_uuid,
    )

    # Two fetcher calls, three raw hits, two unique listing_ids after dedup.
    assert len(fetcher.search_calls) == 2
    assert summary.result_count == 2


async def test_partial_keyword_failure_keeps_entry_alive() -> None:
    """One keyword erroring out must not lose the other keyword's hits."""
    entry = _entry(keywords=["good-kw", "bad-kw"])
    good_listing = _listing("good", title="good")

    class _FailingFetcher(PageFetcher):
        def __init__(self) -> None:
            self.search_calls: list[SearchQuery] = []

        async def search(self, query: SearchQuery) -> list[Listing]:
            self.search_calls.append(query)
            if query.keyword == "bad-kw":
                raise RuntimeError("transient")
            return [good_listing]

        async def fetch(self, listing_url: str) -> Listing:
            raise AssertionError

    fetcher = _FailingFetcher()
    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        fetcher=fetcher,
        evaluator=_FakeEvaluator(),
        store=_FakeStore(),
        telegram=_FakeTelegram(),
        clock=_utc_t0,
        new_alert_id=_fixed_uuid,
    )

    assert len(fetcher.search_calls) == 2
    # Entry not marked failed — partial success.
    assert summary.errors == 0
    assert summary.failed_entries == []
    assert summary.result_count == 1


async def test_all_keywords_failing_marks_entry_failed() -> None:
    entry = _entry(keywords=["kw1", "kw2"])
    fetcher = _FakeFetcher(response=RuntimeError("dead"))

    summary = await run_poll_cycle(
        "wallapop",
        wishlist=_wishlist(entry),
        fetcher=fetcher,
        evaluator=_FakeEvaluator(),
        store=_FakeStore(),
        telegram=_FakeTelegram(),
        clock=_utc_t0,
        new_alert_id=_fixed_uuid,
    )

    assert summary.errors == 1
    assert summary.failed_entries == [entry.display_name]
