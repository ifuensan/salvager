"""Tests for ``hardware-hunter test-search`` — Story 4.6 (FR43).

The marketplace fetchers are mocked at the ``_build_fetcher`` boundary
so no network call (and no credential file) is needed. The contract
under test: a dry-run search renders results, never touches Telegram
or any SQLite audit table, and degrades gracefully.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from hardware_hunter.cli.commands import test_search_cmd
from hardware_hunter.cli.commands.test_search_cmd import run
from hardware_hunter.config.config_yaml import ConfigModel
from hardware_hunter.config.env import EnvSettings
from hardware_hunter.domain.errors import TinyFishRateLimited
from hardware_hunter.domain.evaluation import ListingEvaluation
from hardware_hunter.domain.listing import Listing, SearchQuery
from hardware_hunter.domain.wishlist import Wishlist, WishlistEntry
from hardware_hunter.interfaces.page_fetcher import PageFetcher

_T0 = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)


def _env() -> EnvSettings:
    return EnvSettings(
        TELEGRAM_BOT_TOKEN=SecretStr("bot"),
        TELEGRAM_CHAT_ID=1,
        GEMINI_API_KEY=SecretStr("gemini"),
        EBAY_APP_ID=SecretStr("app"),
        EBAY_CERT_ID=SecretStr("cert"),
        EBAY_DEV_ID=SecretStr("dev"),
        TINYFISH_API_KEY=SecretStr("tinyfish"),
    )


def _entry() -> WishlistEntry:
    return WishlistEntry.model_validate(
        {
            "manufacturer": "Western Digital",
            "model": "WD Red Plus 4TB",
            "ref": "WD40EFPX",
            "type": "hdd",
            "keywords": ["wd red plus 4tb"],
            "max_price_solo": Decimal("70.00"),
            "confidence_threshold": "medium",
        }
    )


def _wishlist() -> Wishlist:
    return Wishlist(entries=[_entry()])


def _listing(
    listing_id: str,
    *,
    marketplace: str = "wallapop",
    title: str = "WD Red Plus 4TB",
) -> Listing:
    return Listing(
        listing_id=listing_id,
        marketplace=marketplace,  # type: ignore[arg-type]
        url=f"https://example/{listing_id}",
        title=title,
        description="Como nuevo",
        price_eur=Decimal("55.00"),
        location="Madrid",
        fetched_at=_T0,
    )


class _FakeFetcher(PageFetcher):
    def __init__(self, listings: list[Listing] | BaseException) -> None:
        self._listings = listings
        self.closed = False

    async def search(self, query: SearchQuery) -> list[Listing]:
        if isinstance(self._listings, BaseException):
            raise self._listings
        return list(self._listings)

    async def fetch(self, listing_url: str) -> Listing:  # pragma: no cover
        raise AssertionError("test-search never calls fetch()")

    async def aclose(self) -> None:
        self.closed = True


def _patch_fetchers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wallapop: _FakeFetcher | None,
    ebay: _FakeFetcher | None,
) -> None:
    def fake_build(market: str, **_kw: Any) -> PageFetcher | None:
        return wallapop if market == "wallapop" else ebay

    monkeypatch.setattr(test_search_cmd, "_build_fetcher", fake_build)


def _run(tmp_path: Path, **overrides: Any) -> int:
    kwargs: dict[str, Any] = {
        "query_or_entry": "WD40EFPX",
        "env": _env(),
        "config": ConfigModel(),
        "wishlist": _wishlist(),
        "data_dir": tmp_path,
        "output_format": "json",
    }
    kwargs.update(overrides)
    return run(**kwargs)


# ─────────────────────────────────────────────────────────────────────────
# Happy path — both marketplaces, heuristic only
# ─────────────────────────────────────────────────────────────────────────


def test_search_renders_results_from_both_marketplaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_fetchers(
        monkeypatch,
        wallapop=_FakeFetcher([_listing("w1")]),
        ebay=_FakeFetcher([_listing("e1", marketplace="ebay")]),
    )
    code = _run(tmp_path)
    assert code == 0
    results = json.loads(capsys.readouterr().out)
    assert {r["marketplace"] for r in results} == {"wallapop", "ebay"}
    # Heuristic populated; no LLM fields without --evaluate.
    assert all("match_probability" in r for r in results)
    assert all("confidence" not in r for r in results)


def test_search_heuristic_scores_keyword_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Entry keywords: ["wd red plus 4tb"], ref WD40EFPX, model "WD Red Plus 4TB".
    hit = _listing("hit", title="WD Red Plus 4TB WD40EFPX")
    miss = _listing("miss", title="Unrelated GPU", marketplace="wallapop")
    _patch_fetchers(monkeypatch, wallapop=_FakeFetcher([hit, miss]), ebay=None)
    _run(tmp_path, marketplace="wallapop")
    results = {r["listing_id"]: r["match_probability"] for r in json.loads(capsys.readouterr().out)}
    assert results["hit"] > results["miss"]


# ─────────────────────────────────────────────────────────────────────────
# Marketplace selection + missing credentials
# ─────────────────────────────────────────────────────────────────────────


def test_marketplace_flag_limits_to_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_fetchers(
        monkeypatch,
        wallapop=_FakeFetcher([_listing("w1")]),
        ebay=_FakeFetcher([_listing("e1", marketplace="ebay")]),
    )
    _run(tmp_path, marketplace="ebay")
    results = json.loads(capsys.readouterr().out)
    assert {r["marketplace"] for r in results} == {"ebay"}


def test_missing_creds_for_explicit_marketplace_exits_4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_fetchers(monkeypatch, wallapop=None, ebay=None)
    code = _run(tmp_path, marketplace="wallapop")
    assert code == 4
    assert "credentials not found" in capsys.readouterr().err


def test_missing_creds_for_one_of_both_is_a_note_not_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_fetchers(monkeypatch, wallapop=_FakeFetcher([_listing("w1")]), ebay=None)
    code = _run(tmp_path)
    assert code == 0
    out = capsys.readouterr()
    results = json.loads(out.out)
    assert {r["marketplace"] for r in results} == {"wallapop"}
    assert "ebay: skipped" in out.err


def test_unknown_marketplace_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run(tmp_path, marketplace="amazon")
    assert code == 2
    assert "unknown --marketplace" in capsys.readouterr().err


# ─────────────────────────────────────────────────────────────────────────
# Rate-limit — partial results, exit 0
# ─────────────────────────────────────────────────────────────────────────


def test_rate_limit_yields_partial_results_and_exit_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_fetchers(
        monkeypatch,
        wallapop=_FakeFetcher([_listing("w1")]),
        ebay=_FakeFetcher(TinyFishRateLimited(retry_after_s=10)),
    )
    code = _run(tmp_path)
    assert code == 0  # partial results are still useful
    out = capsys.readouterr()
    results = json.loads(out.out)
    assert {r["marketplace"] for r in results} == {"wallapop"}
    assert "rate limit reached" in out.err


# ─────────────────────────────────────────────────────────────────────────
# --evaluate — LLM columns populated
# ─────────────────────────────────────────────────────────────────────────


def test_evaluate_flag_runs_the_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_fetchers(monkeypatch, wallapop=_FakeFetcher([_listing("w1")]), ebay=None)

    class _FakeEvaluator:
        async def evaluate(self, listing: Listing, entry: WishlistEntry) -> ListingEvaluation:
            return ListingEvaluation(
                listing_id=listing.listing_id,
                entry_key=entry.entry_key,
                confidence="high",
                one_line_take="Strong match.",
                is_container=False,
                evaluated_at=_T0,
            )

    # Swap the whole evaluator-construction path: a no-op cache + the fake.
    class _NoopCache:
        async def close(self) -> None:
            return None

    monkeypatch.setattr(test_search_cmd, "SqliteLlmEvalCache", lambda *a, **kw: _NoopCache())
    monkeypatch.setattr(
        test_search_cmd,
        "CachingListingEvaluator",
        lambda *a, **kw: _FakeEvaluator(),
    )
    monkeypatch.setattr(test_search_cmd, "GeminiFlashEvaluator", lambda *a, **kw: object())

    code = _run(tmp_path, marketplace="wallapop", evaluate=True)
    assert code == 0
    results = json.loads(capsys.readouterr().out)
    assert results[0]["confidence"] == "high"
    assert results[0]["one_line_take"] == "Strong match."


# ─────────────────────────────────────────────────────────────────────────
# Arbitrary query (not a wishlist ref)
# ─────────────────────────────────────────────────────────────────────────


def test_arbitrary_query_is_passed_verbatim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: list[SearchQuery] = []

    class _CapturingFetcher(_FakeFetcher):
        async def search(self, query: SearchQuery) -> list[Listing]:
            captured.append(query)
            return [_listing("w1")]

    monkeypatch.setattr(
        test_search_cmd,
        "_build_fetcher",
        lambda market, **_kw: _CapturingFetcher([]) if market == "wallapop" else None,
    )
    code = _run(tmp_path, query_or_entry="random gpu query", marketplace="wallapop")
    assert code == 0
    # The free-text string went straight into the query keywords.
    assert captured[0].keywords == ["random gpu query"]
