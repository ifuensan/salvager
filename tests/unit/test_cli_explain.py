"""Tests for ``salvager explain <url>`` — Story 4.7 (FR44).

The marketplace fetcher and the LLM evaluator are mocked at the
module-construction boundary so no network call is made. The contract
under test: fetch one listing, evaluate it against the plausible
wishlist entries, and surface the prompt + verdict + would-be-alert.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from salvager.cli.commands import explain_cmd
from salvager.cli.commands.explain_cmd import run
from salvager.config.config_yaml import ConfigModel
from salvager.config.env import EnvSettings
from salvager.domain.errors import WallapopApiError
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing, SearchQuery
from salvager.domain.wishlist import Wishlist, WishlistEntry
from salvager.interfaces.page_fetcher import PageFetcher

_T0 = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
_WALLAPOP_URL = "https://es.wallapop.com/item/wd-red-plus-4tb-abc123"


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


def _entry(
    ref: str = "WD40EFPX",
    *,
    keywords: list[str] | None = None,
    model: str = "WD Red Plus 4TB",
) -> WishlistEntry:
    return WishlistEntry.model_validate(
        {
            "manufacturer": "Western Digital",
            "model": model,
            "ref": ref,
            "type": "hdd",
            "keywords": keywords if keywords is not None else ["wd red plus 4tb"],
            "max_price_solo": Decimal("70.00"),
            "confidence_threshold": "medium",
        }
    )


def _listing() -> Listing:
    return Listing(
        listing_id="abc123",
        marketplace="wallapop",
        url=_WALLAPOP_URL,
        title="WD Red Plus 4TB NAS disk",
        description="Como nuevo, en caja",
        price_eur=Decimal("55.00"),
        location="Madrid",
        photo_urls=["https://cdn/p.jpg"],
        fetched_at=_T0,
    )


class _FakeFetcher(PageFetcher):
    def __init__(self, result: Listing | BaseException) -> None:
        self._result = result
        self.closed = False

    async def search(self, query: SearchQuery) -> list[Listing]:  # pragma: no cover
        raise AssertionError("explain never calls search()")

    async def fetch(self, listing_url: str) -> Listing:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result

    async def aclose(self) -> None:
        self.closed = True


def _patch_fetcher(monkeypatch: pytest.MonkeyPatch, fetcher: _FakeFetcher | None) -> None:
    monkeypatch.setattr(explain_cmd, "_build_fetcher", lambda *a, **kw: fetcher)


def _patch_evaluator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    confidence: str = "high",
    cache_hit: bool = False,
) -> list[str]:
    """Swap the evaluator construction for a fake; return a call-log list."""
    calls: list[str] = []

    class _FakeEvaluator:
        async def evaluate(self, listing: Listing, entry: WishlistEntry) -> ListingEvaluation:
            calls.append(entry.ref)
            return ListingEvaluation(
                listing_id=listing.listing_id,
                entry_key=entry.entry_key,
                confidence=confidence,  # type: ignore[arg-type]
                one_line_take="Strong match.",
                is_container=False,
                evaluated_at=_T0,
                cache_hit=cache_hit,
            )

    class _NoopCache:
        async def close(self) -> None:
            return None

    monkeypatch.setattr(explain_cmd, "SqliteLlmEvalCache", lambda *a, **kw: _NoopCache())
    monkeypatch.setattr(explain_cmd, "build_inner_evaluator", lambda *a, **kw: object())
    monkeypatch.setattr(explain_cmd, "CachingListingEvaluator", lambda *a, **kw: _FakeEvaluator())
    return calls


def _run(tmp_path: Path, **overrides: Any) -> int:
    kwargs: dict[str, Any] = {
        "url": _WALLAPOP_URL,
        "env": _env(),
        "config": ConfigModel(),
        "wishlist": Wishlist(entries=[_entry()]),
        "data_dir": tmp_path,
        "output_format": "json",
    }
    kwargs.update(overrides)
    return run(**kwargs)


# ─────────────────────────────────────────────────────────────────────────
# URL handling
# ─────────────────────────────────────────────────────────────────────────


def test_unknown_marketplace_url_exits_3(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run(tmp_path, url="https://amazon.es/dp/B0XYZ")
    assert code == 3
    err = capsys.readouterr().err
    assert "failed to fetch listing" in err
    assert "check the URL" in err


def test_fetch_error_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_fetcher(monkeypatch, _FakeFetcher(WallapopApiError(404, "not found")))
    code = _run(tmp_path)
    assert code == 3
    assert "failed to fetch listing" in capsys.readouterr().err


# ─────────────────────────────────────────────────────────────────────────
# Happy path — evaluation surfaced
# ─────────────────────────────────────────────────────────────────────────


def test_explain_evaluates_plausible_entry_and_emits_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_fetcher(monkeypatch, _FakeFetcher(_listing()))
    _patch_evaluator(monkeypatch, confidence="high")

    code = _run(tmp_path)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["listing"]["listing_id"] == "abc123"
    assert len(payload["evaluations"]) == 1
    evaluation = payload["evaluations"][0]
    assert evaluation["entry_key"] == ["Western Digital", "WD Red Plus 4TB", "WD40EFPX"]
    assert evaluation["prompt"]  # the built prompt is included
    assert evaluation["response"]["confidence"] == "high"
    # high >= medium threshold → would alert, with a rendered alert body.
    assert evaluation["would_alert"] is True
    assert evaluation["would_be_alert_text"] is not None
    assert evaluation["reason_for_skip"] is None


def test_low_confidence_records_a_skip_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_fetcher(monkeypatch, _FakeFetcher(_listing()))
    _patch_evaluator(monkeypatch, confidence="low")

    _run(tmp_path)
    evaluation = json.loads(capsys.readouterr().out)["evaluations"][0]
    assert evaluation["would_alert"] is False
    assert evaluation["would_be_alert_text"] is None
    assert "below the entry threshold" in evaluation["reason_for_skip"]


def test_cache_hit_is_noted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_fetcher(monkeypatch, _FakeFetcher(_listing()))
    _patch_evaluator(monkeypatch, cache_hit=True)

    _run(tmp_path)
    evaluation = json.loads(capsys.readouterr().out)["evaluations"][0]
    assert evaluation["from_cache"] is True


# ─────────────────────────────────────────────────────────────────────────
# Entry selection
# ─────────────────────────────────────────────────────────────────────────


def test_entry_flag_pins_a_single_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_fetcher(monkeypatch, _FakeFetcher(_listing()))
    calls = _patch_evaluator(monkeypatch)
    # Two entries; the listing text only mentions one — but --entry forces
    # exactly the requested ref regardless of the heuristic.
    wishlist = Wishlist(
        entries=[
            _entry("WD40EFPX"),
            _entry("CT16G", keywords=["crucial 16gb ddr4"]),
        ]
    )
    code = _run(tmp_path, wishlist=wishlist, entry_ref="CT16G")
    assert code == 0
    assert calls == ["CT16G"]


def test_no_plausible_entries_exits_0_with_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_fetcher(monkeypatch, _FakeFetcher(_listing()))
    _patch_evaluator(monkeypatch)
    # The single entry's keywords don't appear in the listing text.
    wishlist = Wishlist(
        entries=[
            _entry(
                "CT16G",
                keywords=["crucial 16gb ddr4"],
                model="Crucial 16GB DDR4",
            )
        ]
    )
    code = _run(tmp_path, wishlist=wishlist)
    assert code == 0
    out = capsys.readouterr().out
    assert "no wishlist entries plausibly match" in out


def test_explain_human_output_renders_panels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_fetcher(monkeypatch, _FakeFetcher(_listing()))
    _patch_evaluator(monkeypatch, confidence="high")
    code = _run(tmp_path, output_format="human")
    assert code == 0
    out = capsys.readouterr().out
    assert "Listing:" in out
    assert "Prompt" in out
    assert "WOULD ALERT" in out
