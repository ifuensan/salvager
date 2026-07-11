"""Tests for the Claude Haiku LLM evaluator — NFR-I3 alternate provider.

Mirrors ``tests/unit/test_llm_gemini_evaluator.py``. Same prompt is
shared, same response contract, same error mapping — the only delta
is the SDK behind the default callable. The injected ``call`` seam
keeps the ``anthropic`` SDK out of the unit-test import graph (and
out of the NFR-M1 adapter-discipline lint blast radius).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import SecretStr

from salvager.adapters.llm_claude import (
    ClaudeCallable,
    ClaudeHaikuEvaluator,
)
from salvager.domain.errors import LlmEvaluationError, LlmRateLimited
from salvager.domain.listing import Listing
from salvager.domain.wishlist import WishlistEntry

# ─────────────────────────────────────────────────────────────────────────
# Fixtures (copied from gemini test file to keep tests self-contained)
# ─────────────────────────────────────────────────────────────────────────


def _entry(**overrides: object) -> WishlistEntry:
    base: dict[str, object] = {
        "manufacturer": "Western Digital",
        "model": "WD Red Plus 4TB",
        "ref": "WD40EFPX",
        "type": "hdd",
        "max_price_solo": Decimal("60.00"),
        "max_price_in_device": Decimal("90.00"),
        "keywords": ["WD Red Plus 4TB", "WD40EFPX"],
        "container_keywords": ["NAS", "Synology"],
        "confidence_threshold": "high",
    }
    base.update(overrides)
    return WishlistEntry(**base)  # type: ignore[arg-type]


def _listing(price_eur: Decimal = Decimal("55.00"), **overrides: object) -> Listing:
    base: dict[str, object] = {
        "listing_id": "lst-001",
        "marketplace": "wallapop",
        "url": "https://wallapop.com/item/lst-001",
        "title": "WD Red Plus 4TB",
        "description": "Como nuevo, en caja.",
        "price_eur": price_eur,
        "location": "Madrid",
        "photo_urls": ["https://cdn/photo.jpg"],
        "fetched_at": datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return Listing(**base)  # type: ignore[arg-type]


def _make_callable(response_text: str) -> ClaudeCallable:
    async def _call(prompt: str) -> str:
        return response_text

    return _call


def _make_failing_callable(exc: Exception) -> ClaudeCallable:
    async def _call(prompt: str) -> str:
        raise exc

    return _call


def _valid_response() -> str:
    return json.dumps(
        {
            "confidence": "high",
            "one_line_take": "WD Red Plus 4TB at €55 in Madrid — strong match.",
            "is_container": False,
            "wrapper_text": None,
            "extracted_text": "WD40EFPX visible in title",
        }
    )


# ─────────────────────────────────────────────────────────────────────────
# Successful evaluation
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_evaluation_returns_typed_result() -> None:
    evaluator = ClaudeHaikuEvaluator(
        SecretStr("test-key"),
        call=_make_callable(_valid_response()),
    )
    evaluation = await evaluator.evaluate(_listing(), _entry())
    assert evaluation.confidence == "high"
    assert evaluation.is_container is False
    assert "WD Red Plus" in evaluation.one_line_take
    assert evaluation.listing_id == "lst-001"
    assert evaluation.entry_key == ("Western Digital", "WD Red Plus 4TB", "WD40EFPX")
    assert evaluation.cache_hit is False
    assert evaluation.evaluated_at is not None


@pytest.mark.asyncio
async def test_container_detection_result_round_trips() -> None:
    container_response = json.dumps(
        {
            "confidence": "medium",
            "one_line_take": "Synology DS220+ NAS *with* 2x WD Red Plus 4TB drives.",
            "is_container": True,
            "wrapper_text": "Synology DS220+ NAS",
            "extracted_text": "WD Red Plus 4TB drives",
        }
    )
    evaluator = ClaudeHaikuEvaluator(
        SecretStr("test-key"),
        call=_make_callable(container_response),
    )
    evaluation = await evaluator.evaluate(_listing(), _entry())
    assert evaluation.is_container is True
    assert "Synology" in (evaluation.wrapper_text or "")


@pytest.mark.asyncio
async def test_evaluator_extracts_json_from_markdown_fences() -> None:
    """Claude sometimes wraps JSON in ```json``` despite instructions
    not to. Extractor stays robust."""
    fenced = "Sure — here's the JSON:\n\n```json\n" + _valid_response() + "\n```\n"
    evaluator = ClaudeHaikuEvaluator(
        SecretStr("test-key"),
        call=_make_callable(fenced),
    )
    evaluation = await evaluator.evaluate(_listing(), _entry())
    assert evaluation.confidence == "high"


@pytest.mark.asyncio
async def test_evaluator_extracts_json_ignoring_trailing_brace_prose() -> None:
    """Regression: the old greedy ``\\{.*\\}`` regex would span from
    the first ``{`` to the last ``}`` and capture trailing aside text
    with literal braces, producing invalid JSON. The structural
    extractor (json.JSONDecoder.raw_decode) returns just the first
    parseable object."""
    trailing = _valid_response() + "\n\nPS: aside text mentioning {literal:braces} after the JSON."
    evaluator = ClaudeHaikuEvaluator(
        SecretStr("test-key"),
        call=_make_callable(trailing),
    )
    evaluation = await evaluator.evaluate(_listing(), _entry())
    assert evaluation.confidence == "high"


# ─────────────────────────────────────────────────────────────────────────
# Budget short-circuit
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_above_both_budgets_short_circuits_to_low() -> None:
    """Listing price > BOTH ceilings → confidence=low, no LLM call."""
    calls: list[str] = []

    async def _record_call(prompt: str) -> str:
        calls.append(prompt)
        return _valid_response()

    evaluator = ClaudeHaikuEvaluator(SecretStr("test-key"), call=_record_call)
    pricey = _listing(price_eur=Decimal("150.00"))  # above both 60 and 90
    evaluation = await evaluator.evaluate(pricey, _entry())

    assert calls == []
    assert evaluation.confidence == "low"
    assert "price exceeds wishlist max" in evaluation.one_line_take


@pytest.mark.asyncio
async def test_between_budgets_does_not_short_circuit() -> None:
    """Above max_solo but below max_in_device — still call the LLM
    (wrapper-listing case is interesting)."""
    calls: list[str] = []

    async def _record_call(prompt: str) -> str:
        calls.append(prompt)
        return _valid_response()

    evaluator = ClaudeHaikuEvaluator(SecretStr("test-key"), call=_record_call)
    mid = _listing(price_eur=Decimal("75.00"))  # > 60 solo, < 90 in_device
    await evaluator.evaluate(mid, _entry())
    assert len(calls) == 1


# ─────────────────────────────────────────────────────────────────────────
# Error mapping
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_malformed_json_raises_llm_evaluation_error() -> None:
    evaluator = ClaudeHaikuEvaluator(
        SecretStr("test-key"),
        call=_make_callable("not even JSON-shaped"),
    )
    with pytest.raises(LlmEvaluationError):
        await evaluator.evaluate(_listing(), _entry())


@pytest.mark.asyncio
async def test_empty_response_raises_llm_evaluation_error() -> None:
    evaluator = ClaudeHaikuEvaluator(
        SecretStr("test-key"),
        call=_make_callable(""),
    )
    with pytest.raises(LlmEvaluationError):
        await evaluator.evaluate(_listing(), _entry())


@pytest.mark.asyncio
async def test_invalid_confidence_value_raises_llm_evaluation_error() -> None:
    bad = json.dumps(
        {
            "confidence": "very-high",  # invalid
            "one_line_take": "x",
            "is_container": False,
        }
    )
    evaluator = ClaudeHaikuEvaluator(SecretStr("test-key"), call=_make_callable(bad))
    with pytest.raises(LlmEvaluationError):
        await evaluator.evaluate(_listing(), _entry())


@pytest.mark.asyncio
async def test_one_line_take_too_long_is_clipped_not_rejected() -> None:
    """Mirror of the Gemini adapter's clipping behaviour — see
    `_llm_evaluator_shared.clip_one_line_take`."""
    long_response = json.dumps(
        {
            "confidence": "high",
            "one_line_take": "x" * 200,  # > 120 chars
            "is_container": False,
        }
    )
    evaluator = ClaudeHaikuEvaluator(
        SecretStr("test-key"),
        call=_make_callable(long_response),
    )
    evaluation = await evaluator.evaluate(_listing(), _entry())
    assert len(evaluation.one_line_take) == 120
    assert evaluation.one_line_take == "x" * 119 + "…"
    assert evaluation.confidence == "high"  # verdict preserved


@pytest.mark.asyncio
async def test_rate_limit_exception_propagates() -> None:
    """LlmRateLimited surfaces unchanged — orchestration decides
    degradation, not the adapter."""
    evaluator = ClaudeHaikuEvaluator(
        SecretStr("test-key"),
        call=_make_failing_callable(LlmRateLimited("429 Too Many Requests")),
    )
    with pytest.raises(LlmRateLimited):
        await evaluator.evaluate(_listing(), _entry())


# ─────────────────────────────────────────────────────────────────────────
# Adapter-discipline: only this package imports anthropic
# ─────────────────────────────────────────────────────────────────────────


def test_no_other_package_imports_anthropic() -> None:
    """NFR-I3 / NFR-M1: the ``anthropic`` SDK import is allowed only in
    adapters/llm_claude/. The existing adapter-discipline lint already
    blocks domain/interfaces/orchestration/cli from importing
    marketplace SDKs; this test extends the coverage to other adapter
    packages so we catch leakage at unit-test time."""
    import ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    adapters_dir = repo_root / "src" / "salvager" / "adapters"
    for path in adapters_dir.rglob("*.py"):
        if "llm_claude" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("anthropic"), (
                        f"{path.relative_to(repo_root)}: forbidden anthropic import"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith("anthropic"), (
                    f"{path.relative_to(repo_root)}: forbidden anthropic import"
                )
