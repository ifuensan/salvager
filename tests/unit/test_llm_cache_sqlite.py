"""Tests for the SQLite-backed LLM eval cache — Story 3.10.

The cache file is created in pytest's tmp_path so each test starts
fresh. The clock is dependency-injected via the constructor so TTL
behaviour is tested deterministically without sleep.
"""

from __future__ import annotations

from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from salvager.adapters.llm_cache_sqlite import (
    CachingListingEvaluator,
    SqliteLlmEvalCache,
)
from salvager.adapters.llm_cache_sqlite.cache import (
    DEFAULT_TTL_HOURS,
    DEFAULT_TTL_HOURS_LOW_CONFIDENCE,
)
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.domain.wishlist import WishlistEntry
from salvager.interfaces.listing_evaluator import ListingEvaluator

# ─────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────


_T0 = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


class _FrozenClock:
    """Mutable clock so tests can advance time inside one cache instance."""

    def __init__(self, start: datetime = _T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


def _listing(
    listing_id: str = "abc123",
    url: str = "https://wallapop.com/item/abc123",
) -> Listing:
    return Listing(
        listing_id=listing_id,
        marketplace="wallapop",
        url=url,
        title="WD Red Plus 4TB",
        description="Like new, in box.",
        price_eur=Decimal("55.00"),
        location="Madrid",
        fetched_at=_T0,
    )


def _entry() -> WishlistEntry:
    return WishlistEntry.model_validate(
        {
            "manufacturer": "Western Digital",
            "model": "WD Red Plus 4TB",
            "ref": "WD40EFPX",
            "type": "hdd",
            "keywords": ["wd red plus 4tb"],
            "container_keywords": ["nas synology", "qnap"],
            "max_price_solo": Decimal("70.00"),
            "confidence_threshold": "medium",
        }
    )


def _evaluation(
    *,
    confidence: str = "high",
    one_line_take: str = "Strong match at €55.",
    is_container: bool = False,
) -> ListingEvaluation:
    return ListingEvaluation(
        listing_id="abc123",
        entry_key=("Western Digital", "WD Red Plus 4TB", "WD40EFPX"),
        confidence=confidence,  # type: ignore[arg-type]
        one_line_take=one_line_take,
        is_container=is_container,
        evaluated_at=_T0,
    )


def _make_cache(
    tmp_path: Path,
    *,
    clock: _FrozenClock,
    ttl_normal: timedelta = timedelta(hours=DEFAULT_TTL_HOURS),
    ttl_low: timedelta = timedelta(hours=DEFAULT_TTL_HOURS_LOW_CONFIDENCE),
) -> SqliteLlmEvalCache:
    return SqliteLlmEvalCache(
        tmp_path / "llm_eval_cache.db",
        ttl_normal=ttl_normal,
        ttl_low_confidence=ttl_low,
        clock=clock,
    )


# ─────────────────────────────────────────────────────────────────────────
# get / set — happy path round-trip
# ─────────────────────────────────────────────────────────────────────────


async def test_get_returns_none_for_unknown_key(tmp_path: Path) -> None:
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    try:
        assert await cache.get("https://example.com/unseen", "v1") is None
    finally:
        await cache.close()


async def test_set_then_get_round_trips(tmp_path: Path) -> None:
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    try:
        evaluation = _evaluation()
        url = "https://wallapop.com/item/abc123"
        await cache.set(url, "v1", prompt_text="dummy prompt", evaluation=evaluation)
        loaded = await cache.get(url, "v1")
        assert loaded is not None
        assert loaded.listing_id == evaluation.listing_id
        assert loaded.confidence == evaluation.confidence
        assert loaded.one_line_take == evaluation.one_line_take
    finally:
        await cache.close()


async def test_set_replaces_existing_entry(tmp_path: Path) -> None:
    """A second ``set`` against the same key overwrites the value and
    resets ``cached_at`` to the new clock value."""
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    try:
        url = "https://wallapop.com/item/x"
        await cache.set(url, "v1", "p", _evaluation(confidence="low"))

        clock.advance(timedelta(minutes=30))
        await cache.set(url, "v1", "p", _evaluation(confidence="high", one_line_take="Now high."))

        # The replacement also extends TTL: 30 min after the *second*
        # write we still hit (high-confidence default TTL is 24h).
        clock.advance(timedelta(hours=23))
        loaded = await cache.get(url, "v1")
        assert loaded is not None
        assert loaded.confidence == "high"
        assert loaded.one_line_take == "Now high."
    finally:
        await cache.close()


# ─────────────────────────────────────────────────────────────────────────
# Key includes prompt_version — bumping the version evicts everything
# ─────────────────────────────────────────────────────────────────────────


async def test_get_returns_none_when_prompt_version_differs(tmp_path: Path) -> None:
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    try:
        url = "https://wallapop.com/item/abc"
        await cache.set(url, "v1", "p", _evaluation())
        # Same URL, different prompt_version → miss.
        assert await cache.get(url, "v2") is None
        # The v1 entry is still there, just not for v2 callers.
        assert await cache.get(url, "v1") is not None
    finally:
        await cache.close()


# ─────────────────────────────────────────────────────────────────────────
# TTL semantics — low vs normal confidence
# ─────────────────────────────────────────────────────────────────────────


async def test_high_confidence_within_24h_returns_value(tmp_path: Path) -> None:
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    try:
        url = "https://wallapop.com/item/x"
        await cache.set(url, "v1", "p", _evaluation(confidence="high"))
        clock.advance(timedelta(hours=23, minutes=59))
        assert await cache.get(url, "v1") is not None
    finally:
        await cache.close()


async def test_high_confidence_past_24h_returns_none(tmp_path: Path) -> None:
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    try:
        url = "https://wallapop.com/item/x"
        await cache.set(url, "v1", "p", _evaluation(confidence="high"))
        clock.advance(timedelta(hours=24, minutes=1))
        assert await cache.get(url, "v1") is None
    finally:
        await cache.close()


async def test_low_confidence_past_1h_returns_none(tmp_path: Path) -> None:
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    try:
        url = "https://wallapop.com/item/x"
        await cache.set(url, "v1", "p", _evaluation(confidence="low"))
        clock.advance(timedelta(hours=1, minutes=1))
        assert await cache.get(url, "v1") is None
    finally:
        await cache.close()


async def test_low_confidence_within_1h_returns_value(tmp_path: Path) -> None:
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    try:
        url = "https://wallapop.com/item/x"
        await cache.set(url, "v1", "p", _evaluation(confidence="low"))
        clock.advance(timedelta(minutes=59))
        assert await cache.get(url, "v1") is not None
    finally:
        await cache.close()


async def test_medium_confidence_uses_normal_ttl(tmp_path: Path) -> None:
    """Medium is NOT 'low' — it follows the normal TTL."""
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    try:
        url = "https://wallapop.com/item/x"
        await cache.set(url, "v1", "p", _evaluation(confidence="medium"))
        clock.advance(timedelta(hours=23))
        assert await cache.get(url, "v1") is not None
    finally:
        await cache.close()


async def test_ttl_constructor_overrides_apply(tmp_path: Path) -> None:
    """Custom TTLs from config.yaml flow through the constructor."""
    clock = _FrozenClock()
    cache = _make_cache(
        tmp_path,
        clock=clock,
        ttl_normal=timedelta(hours=4),
        ttl_low=timedelta(minutes=15),
    )
    try:
        url = "https://wallapop.com/item/x"
        await cache.set(url, "v1", "p", _evaluation(confidence="medium"))
        clock.advance(timedelta(hours=3, minutes=59))
        assert await cache.get(url, "v1") is not None
        clock.advance(timedelta(minutes=2))
        assert await cache.get(url, "v1") is None
    finally:
        await cache.close()


# ─────────────────────────────────────────────────────────────────────────
# Persistence — prompt + value survive the row
# ─────────────────────────────────────────────────────────────────────────


async def test_stored_prompt_text_is_persisted(tmp_path: Path) -> None:
    """FR44 ('explain') wants the originating prompt back. The cache
    must store it verbatim alongside the evaluation."""
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    try:
        url = "https://wallapop.com/item/x"
        prompt = "The full prompt the LLM saw on 2026-05-13."
        await cache.set(url, "v1", prompt, _evaluation())

        # Reach in via the connection — no get_prompt() public API at
        # v0.x; ``explain`` will get its own method when it lands.
        cursor = cache._connection.execute(
            "SELECT prompt_text FROM llm_evaluation_cache WHERE listing_url = ?",
            (url,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["prompt_text"] == prompt
    finally:
        await cache.close()


# ─────────────────────────────────────────────────────────────────────────
# Structured logging — llm_cache_hit + llm_cache_expired
# ─────────────────────────────────────────────────────────────────────────


async def test_cache_hit_logs_event_with_age(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    try:
        url = "https://wallapop.com/item/x"
        await cache.set(url, "v1", "p", _evaluation(confidence="high"))
        clock.advance(timedelta(hours=2))
        capsys.readouterr()  # drain prior logs
        await cache.get(url, "v1")
        out = capsys.readouterr().out
    finally:
        await cache.close()

    records = [json.loads(line) for line in out.splitlines() if line.strip()]
    hits = [r for r in records if r["event"] == "llm_cache_hit"]
    assert hits, f"missing llm_cache_hit in {records!r}"
    assert hits[0]["listing_url"] == url
    assert hits[0]["age_seconds"] == 2 * 3600


async def test_expired_entry_logs_llm_cache_expired(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    try:
        url = "https://wallapop.com/item/x"
        await cache.set(url, "v1", "p", _evaluation(confidence="low"))
        clock.advance(timedelta(hours=2))  # past 1h low-conf TTL
        capsys.readouterr()
        result = await cache.get(url, "v1")
        out = capsys.readouterr().out
    finally:
        await cache.close()

    assert result is None
    records = [json.loads(line) for line in out.splitlines() if line.strip()]
    expired = [r for r in records if r["event"] == "llm_cache_expired"]
    assert expired, f"missing llm_cache_expired in {records!r}"
    assert expired[0]["confidence"] == "low"


# ─────────────────────────────────────────────────────────────────────────
# CachingListingEvaluator — decorator that consults the cache first
# ─────────────────────────────────────────────────────────────────────────


class _RecordingEvaluator(ListingEvaluator):
    """A fake :class:`ListingEvaluator` that records every call and
    returns a preloaded result."""

    def __init__(self, response: ListingEvaluation) -> None:
        self.calls: list[tuple[str, tuple[str, str, str]]] = []
        self.response = response

    async def evaluate(self, listing: Listing, entry: WishlistEntry) -> ListingEvaluation:
        self.calls.append((listing.url, (entry.manufacturer, entry.model, entry.ref)))
        return self.response


async def test_decorator_miss_calls_inner_and_stores(tmp_path: Path) -> None:
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    inner = _RecordingEvaluator(_evaluation())
    decorator = CachingListingEvaluator(
        inner, cache, "v1", prompt_builder=lambda _l, _e: "fake-prompt"
    )
    try:
        result = await decorator.evaluate(_listing(), _entry())
        # Inner was called; result is the inner's response.
        assert len(inner.calls) == 1
        assert result.cache_hit is False
        # And the cache now has a row for this URL.
        cached = await cache.get(_listing().url, "v1")
        assert cached is not None
    finally:
        await cache.close()


async def test_decorator_hit_skips_inner_and_marks_cache_hit(tmp_path: Path) -> None:
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    inner = _RecordingEvaluator(_evaluation())
    decorator = CachingListingEvaluator(
        inner, cache, "v1", prompt_builder=lambda _l, _e: "fake-prompt"
    )
    try:
        # First call populates the cache (one inner call).
        await decorator.evaluate(_listing(), _entry())
        assert len(inner.calls) == 1
        # Second call hits the cache; inner is NOT called again.
        second = await decorator.evaluate(_listing(), _entry())
        assert len(inner.calls) == 1
        # The returned evaluation is the cached one, with cache_hit=True.
        assert second.cache_hit is True
        assert second.one_line_take == _evaluation().one_line_take
    finally:
        await cache.close()


async def test_decorator_bumps_prompt_version_invalidates_cache(
    tmp_path: Path,
) -> None:
    """A PROMPT_VERSION bump forces every URL to miss exactly once."""
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    inner = _RecordingEvaluator(_evaluation())
    decorator_v1 = CachingListingEvaluator(
        inner, cache, "v1", prompt_builder=lambda _l, _e: "v1-prompt"
    )
    decorator_v2 = CachingListingEvaluator(
        inner, cache, "v2", prompt_builder=lambda _l, _e: "v2-prompt"
    )
    try:
        await decorator_v1.evaluate(_listing(), _entry())
        assert len(inner.calls) == 1
        # v2 doesn't see v1's cache entry → inner runs again.
        result = await decorator_v2.evaluate(_listing(), _entry())
        assert len(inner.calls) == 2
        assert result.cache_hit is False
    finally:
        await cache.close()


async def test_decorator_after_ttl_expiry_calls_inner_again(tmp_path: Path) -> None:
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    inner = _RecordingEvaluator(_evaluation(confidence="low"))
    decorator = CachingListingEvaluator(inner, cache, "v1", prompt_builder=lambda _l, _e: "p")
    try:
        await decorator.evaluate(_listing(), _entry())
        clock.advance(timedelta(hours=2))  # past low-conf TTL
        await decorator.evaluate(_listing(), _entry())
        # Both calls hit the inner because the first's value expired.
        assert len(inner.calls) == 2
    finally:
        await cache.close()


async def test_decorator_uses_real_prompt_builder_by_default(
    tmp_path: Path,
) -> None:
    """When no `prompt_builder` is injected, the decorator falls back to
    :func:`domain.prompts.build_evaluation_prompt` — i.e. the production
    path. Just verify it runs without exception and the stored
    prompt_text isn't empty."""
    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    inner = _RecordingEvaluator(_evaluation())
    decorator = CachingListingEvaluator(inner, cache, "v1")
    try:
        await decorator.evaluate(_listing(), _entry())
        cursor = cache._connection.execute(
            "SELECT prompt_text FROM llm_evaluation_cache WHERE listing_url = ?",
            (_listing().url,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert len(row["prompt_text"]) > 100  # the real prompt is substantial
    finally:
        await cache.close()


# ─────────────────────────────────────────────────────────────────────────
# PROMPT_VERSION constant lives where the rest of the project expects it
# ─────────────────────────────────────────────────────────────────────────


def test_prompt_version_is_exported_from_domain_prompts() -> None:
    from salvager.domain.prompts import PROMPT_VERSION

    assert isinstance(PROMPT_VERSION, str)
    assert PROMPT_VERSION  # non-empty


# ─────────────────────────────────────────────────────────────────────────
# Concurrency — `get` must serialize against itself and against `set`
# ─────────────────────────────────────────────────────────────────────────


async def test_concurrent_gets_do_not_race(tmp_path: Path) -> None:
    """Regression for the daemon's `llm_eval_failed error_class=InterfaceError`.

    The poll loop spawns up to 8 evaluations in parallel (NFR-P3). Each
    one starts with `cache.get`, and asyncio.to_thread dispatches every
    call to its own worker thread. With `check_same_thread=False`, the
    sqlite3 module permits cross-thread access to a single Connection
    BUT the caller must serialize execute() calls — otherwise Python
    raises `sqlite3.InterfaceError` ("Recursive use of cursors not
    allowed" / "Cannot operate on a closed database").

    Without the `_db_lock` around the read in `get`, this test fires the
    race deterministically once you push concurrency past one worker.
    """
    import asyncio as _asyncio

    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    try:
        evaluation = _evaluation()
        # Pre-populate so half the reads hit and half miss; both code
        # paths inside `get` execute the same way, so it doesn't change
        # the race shape, but it does exercise the post-read JSON parse.
        seeded_url = "https://wallapop.com/item/seeded"
        await cache.set(seeded_url, "v1", prompt_text="x", evaluation=evaluation)

        async def hit() -> object:
            return await cache.get(seeded_url, "v1")

        async def miss() -> object:
            return await cache.get("https://wallapop.com/item/missing", "v1")

        # 32 concurrent calls — well past the 8 the daemon spawns, so the
        # race window is wide enough to fire reliably if the lock is off.
        coros = [hit() if i % 2 == 0 else miss() for i in range(32)]
        results = await _asyncio.gather(*coros, return_exceptions=True)

        for r in results:
            assert not isinstance(r, BaseException), f"concurrent get raised: {r!r}"

        # Sanity: hits returned a real evaluation, misses returned None.
        hits = [r for i, r in enumerate(results) if i % 2 == 0]
        misses = [r for i, r in enumerate(results) if i % 2 == 1]
        assert all(r is not None for r in hits)
        assert all(r is None for r in misses)
    finally:
        await cache.close()


async def test_concurrent_get_and_set_do_not_race(tmp_path: Path) -> None:
    """Same race surface, but mixing reads and writes on the same Connection."""
    import asyncio as _asyncio

    clock = _FrozenClock()
    cache = _make_cache(tmp_path, clock=clock)
    try:
        evaluation = _evaluation()
        urls = [f"https://wallapop.com/item/{i}" for i in range(16)]

        async def writer(url: str) -> None:
            await cache.set(url, "v1", prompt_text="p", evaluation=evaluation)

        async def reader(url: str) -> object:
            return await cache.get(url, "v1")

        coros: list[Coroutine[Any, Any, Any]] = []
        for url in urls:
            coros.append(writer(url))
            coros.append(reader(url))

        results = await _asyncio.gather(*coros, return_exceptions=True)
        for r in results:
            assert not isinstance(r, BaseException), f"concurrent op raised: {r!r}"
    finally:
        await cache.close()
