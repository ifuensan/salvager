"""Tests for the Gemini Flash LLM evaluator — Story 3.9."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import SecretStr

from salvager.adapters.llm_gemini import (
    GeminiCallable,
    GeminiFlashEvaluator,
)
from salvager.domain.errors import LlmEvaluationError, LlmRateLimited
from salvager.domain.listing import Listing
from salvager.domain.prompts import (
    FORBIDDEN_PROMPT_TERMS,
    build_evaluation_prompt,
)
from salvager.domain.wishlist import WishlistEntry

# ─────────────────────────────────────────────────────────────────────────
# Fixtures
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


def _make_callable(response_text: str) -> GeminiCallable:
    """Return an ``async (str) -> str`` that yields ``response_text`` once."""

    async def _call(prompt: str) -> str:
        return response_text

    return _call


def _make_failing_callable(exc: Exception) -> GeminiCallable:
    async def _call(prompt: str) -> str:
        raise exc

    return _call


# ─────────────────────────────────────────────────────────────────────────
# Prompt builder
# ─────────────────────────────────────────────────────────────────────────


def test_prompt_includes_entry_context() -> None:
    prompt = build_evaluation_prompt(_listing(), _entry())
    assert "WD Red Plus 4TB" in prompt
    assert "WD40EFPX" in prompt
    assert "Western Digital" in prompt
    assert "container_keywords" in prompt
    assert '"NAS"' in prompt
    assert "EUR 60.00" in prompt  # solo ceiling
    assert "EUR 90.00" in prompt  # in_device ceiling


def test_prompt_includes_listing_context() -> None:
    prompt = build_evaluation_prompt(_listing(), _entry())
    assert "Como nuevo" in prompt
    assert "Madrid" in prompt
    assert "55.00" in prompt
    assert "https://cdn/photo.jpg" in prompt


def test_prompt_asks_single_matching_question() -> None:
    prompt = build_evaluation_prompt(_listing(), _entry())
    assert "Does this listing match this wishlist entry?" in prompt


def test_prompt_requires_output_schema() -> None:
    prompt = build_evaluation_prompt(_listing(), _entry())
    for required_field in (
        "confidence",
        "one_line_take",
        "is_container",
        "wrapper_text",
        "extracted_text",
    ):
        assert required_field in prompt


@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_PROMPT_TERMS))
def test_prompt_never_contains_arbitrage_term(forbidden: str) -> None:
    """FR17 structural enforcement: the prompt must not contain any term
    that would invite the LLM to produce arbitrage-flavored output."""
    prompt = build_evaluation_prompt(_listing(), _entry()).lower()
    assert forbidden.lower() not in prompt, f"prompt contains forbidden term {forbidden!r}"


def test_prompt_handles_missing_in_device_ceiling() -> None:
    """Container detection disabled (FR5) — prompt still renders cleanly."""
    entry_no_container = _entry(max_price_in_device=None)
    prompt = build_evaluation_prompt(_listing(), entry_no_container)
    assert "EUR 60.00" in prompt  # solo ceiling still shown
    assert "in_device" not in prompt or "in_device <= EUR" not in prompt


# ─────────────────────────────────────────────────────────────────────────
# Successful evaluation
# ─────────────────────────────────────────────────────────────────────────


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


@pytest.mark.asyncio
async def test_successful_evaluation_returns_typed_result() -> None:
    evaluator = GeminiFlashEvaluator(
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
    evaluator = GeminiFlashEvaluator(
        SecretStr("test-key"),
        call=_make_callable(container_response),
    )
    evaluation = await evaluator.evaluate(_listing(), _entry())
    assert evaluation.is_container is True
    assert "Synology" in (evaluation.wrapper_text or "")


@pytest.mark.asyncio
async def test_evaluator_extracts_json_from_markdown_fences() -> None:
    """LLMs sometimes ignore "no fences" instructions — the extractor
    is robust enough to keep working."""
    fenced = "Sure — here's the JSON:\n\n```json\n" + _valid_response() + "\n```\n"
    evaluator = GeminiFlashEvaluator(
        SecretStr("test-key"),
        call=_make_callable(fenced),
    )
    evaluation = await evaluator.evaluate(_listing(), _entry())
    assert evaluation.confidence == "high"


# ─────────────────────────────────────────────────────────────────────────
# Budget short-circuit
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_above_both_budgets_short_circuits_to_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listing price > BOTH ceilings → confidence=low, no LLM call."""
    calls: list[str] = []

    async def _record_call(prompt: str) -> str:
        calls.append(prompt)
        return _valid_response()

    evaluator = GeminiFlashEvaluator(
        SecretStr("test-key"),
        call=_record_call,
    )
    pricey = _listing(price_eur=Decimal("150.00"))  # above both 60 and 90
    evaluation = await evaluator.evaluate(pricey, _entry())

    assert calls == []  # LLM was not invoked
    assert evaluation.confidence == "low"
    assert "price exceeds wishlist max" in evaluation.one_line_take


@pytest.mark.asyncio
async def test_between_budgets_does_not_short_circuit() -> None:
    """Above max_solo but below max_in_device — still call the LLM
    (the wrapper-listing case is genuinely interesting)."""
    calls: list[str] = []

    async def _record_call(prompt: str) -> str:
        calls.append(prompt)
        return _valid_response()

    evaluator = GeminiFlashEvaluator(SecretStr("test-key"), call=_record_call)
    mid = _listing(price_eur=Decimal("75.00"))  # > 60 solo, < 90 in_device
    await evaluator.evaluate(mid, _entry())
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_short_circuit_skipped_when_no_ceiling_is_set() -> None:
    """If both ceilings are None, the budget guard cannot fire — the
    wishlist schema rejects entries with both None, but we keep the
    code defensive."""
    calls: list[str] = []

    async def _record_call(prompt: str) -> str:
        calls.append(prompt)
        return _valid_response()

    evaluator = GeminiFlashEvaluator(SecretStr("test-key"), call=_record_call)
    entry = _entry(max_price_in_device=None)
    listing = _listing(price_eur=Decimal("1000.00"))
    await evaluator.evaluate(listing, entry)
    # max_price_solo (60) is the only ceiling, listing is above it →
    # the all() ceiling check IS satisfied → LLM should NOT be called.
    assert calls == []


# ─────────────────────────────────────────────────────────────────────────
# Error mapping
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_malformed_json_raises_llm_evaluation_error() -> None:
    evaluator = GeminiFlashEvaluator(
        SecretStr("test-key"),
        call=_make_callable("not even JSON-shaped"),
    )
    with pytest.raises(LlmEvaluationError):
        await evaluator.evaluate(_listing(), _entry())


@pytest.mark.asyncio
async def test_empty_response_raises_llm_evaluation_error() -> None:
    evaluator = GeminiFlashEvaluator(
        SecretStr("test-key"),
        call=_make_callable(""),
    )
    with pytest.raises(LlmEvaluationError):
        await evaluator.evaluate(_listing(), _entry())


@pytest.mark.asyncio
async def test_invalid_confidence_value_raises_llm_evaluation_error() -> None:
    """Non-conforming response — confidence='very-high' isn't in the literal."""
    bad = json.dumps(
        {
            "confidence": "very-high",  # invalid
            "one_line_take": "x",
            "is_container": False,
        }
    )
    evaluator = GeminiFlashEvaluator(SecretStr("test-key"), call=_make_callable(bad))
    with pytest.raises(LlmEvaluationError):
        await evaluator.evaluate(_listing(), _entry())


@pytest.mark.asyncio
async def test_one_line_take_too_long_is_clipped_not_rejected() -> None:
    """An over-long take is a display constraint, not a bad verdict: models
    routinely need >120 chars for multi-variant "lote" listings, and raising
    here failed the same listings deterministically every cycle (observed in
    prod 2026-07). The take is clipped to 120 chars with an ellipsis."""
    long_response = json.dumps(
        {
            "confidence": "high",
            "one_line_take": "x" * 200,  # > 120 chars
            "is_container": False,
        }
    )
    evaluator = GeminiFlashEvaluator(
        SecretStr("test-key"),
        call=_make_callable(long_response),
    )
    evaluation = await evaluator.evaluate(_listing(), _entry())
    assert len(evaluation.one_line_take) == 120
    assert evaluation.one_line_take == "x" * 119 + "…"
    assert evaluation.confidence == "high"  # verdict preserved


@pytest.mark.asyncio
async def test_one_line_take_at_exactly_the_cap_is_untouched() -> None:
    response = json.dumps(
        {
            "confidence": "medium",
            "one_line_take": "y" * 120,
            "is_container": False,
        }
    )
    evaluator = GeminiFlashEvaluator(SecretStr("test-key"), call=_make_callable(response))
    evaluation = await evaluator.evaluate(_listing(), _entry())
    assert evaluation.one_line_take == "y" * 120


def test_default_model_is_not_the_retired_flavour() -> None:
    """gemini-2.0-flash was retired by Google on 2026-07-11 (API returns 404
    "no longer available") — pin the default so a revert fails loudly."""
    from salvager.adapters.llm_gemini.evaluator import _DEFAULT_MODEL

    assert _DEFAULT_MODEL == "gemini-2.5-flash"


def test_thinking_toggle_gated_to_the_25_flash_family() -> None:
    """thinking_budget=0 is only valid on 2.5 Flash/Flash-Lite: 2.5 Pro
    rejects it (400) and pre-2.5 models take no ThinkingConfig. The default
    model must be in the supported set."""
    from salvager.adapters.llm_gemini.evaluator import (
        _DEFAULT_MODEL,
        _supports_thinking_toggle,
    )

    assert _supports_thinking_toggle(_DEFAULT_MODEL)
    assert _supports_thinking_toggle("gemini-2.5-flash-lite")
    assert not _supports_thinking_toggle("gemini-2.5-pro")
    assert not _supports_thinking_toggle("gemini-1.5-flash")


@pytest.mark.asyncio
async def test_rate_limit_exception_propagates() -> None:
    """LlmRateLimited surfaces unchanged from the call — orchestration
    decides degradation, not the adapter."""
    evaluator = GeminiFlashEvaluator(
        SecretStr("test-key"),
        call=_make_failing_callable(LlmRateLimited("429 Too Many Requests")),
    )
    with pytest.raises(LlmRateLimited):
        await evaluator.evaluate(_listing(), _entry())


# ─────────────────────────────────────────────────────────────────────────
# Adapter-discipline: only this package imports google.genai
# ─────────────────────────────────────────────────────────────────────────


def test_no_other_package_imports_google_genai() -> None:
    """NFR-I3 / NFR-M1: google.genai imports allowed only in
    adapters/llm_gemini/. The existing adapter-discipline lint already
    blocks domain/interfaces/orchestration/cli from importing it; this
    test extends the coverage to other adapter packages."""
    import ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    adapters_dir = repo_root / "src" / "salvager" / "adapters"
    for path in adapters_dir.rglob("*.py"):
        if "llm_gemini" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("google.genai"), (
                        f"{path.relative_to(repo_root)}: forbidden google.genai import"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith("google.genai") and not module.startswith("google"), (
                    f"{path.relative_to(repo_root)}: forbidden google.genai import"
                )
