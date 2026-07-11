"""Gemini Flash :class:`ListingEvaluator` — Story 3.9 (FR13-FR17).

Pre-flight budget guard
-----------------------
Before calling the model, the evaluator checks ``listing.price_eur``
against both wishlist ceilings (``max_price_solo`` and
``max_price_in_device``). If the price strictly exceeds *both*, the
LLM is not consulted; the evaluator short-circuits to ``confidence=low``
with a "price exceeds wishlist max" take. This saves an API call per
out-of-budget listing and satisfies the Story 3.9 AC.

When max_price_in_device is None (container detection disabled per
FR5), the budget check uses only max_price_solo.

Response extraction
-------------------
LLMs sometimes wrap JSON in markdown code fences despite instructions
not to. :func:`_extract_json_object` finds the outermost ``{...}`` in
the response body — robust to leading/trailing prose and code fences.

Test seam
---------
The constructor accepts an injectable :data:`GeminiCallable`
(``async (str) -> str``). The production default wraps
``google.genai.Client.aio.models.generate_content`` and translates
provider-specific rate-limit errors into :class:`LlmRateLimited`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from pydantic import SecretStr, ValidationError

from salvager.adapters._llm_evaluator_shared import (
    budget_short_circuit_evaluation,
    clip_one_line_take,
    exceeds_all_ceilings,
    extract_json_object,
)
from salvager.adapters.llm_gemini.schema import GeminiEvalResponse
from salvager.domain.errors import LlmEvaluationError, LlmRateLimited
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.domain.prompts import build_evaluation_prompt
from salvager.domain.wishlist import WishlistEntry
from salvager.interfaces.listing_evaluator import ListingEvaluator
from salvager.observability.logging import get_logger

#: Take any prompt string, return the model's raw text reply.
GeminiCallable = Callable[[str], Awaitable[str]]

# gemini-2.0-flash was retired by Google (404 "no longer available",
# observed 2026-07-11); 2.5-flash is its successor. Thinking is disabled at
# the call site — this is a classification task and reasoning tokens only
# add cost + latency.
_DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiFlashEvaluator(ListingEvaluator):
    """LLM-backed match judge — Gemini Flash by default, swappable per NFR-I3."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        model: str = _DEFAULT_MODEL,
        call: GeminiCallable | None = None,
    ) -> None:
        self._model = model
        self._call: GeminiCallable = (
            call if call is not None else _build_default_call(api_key.get_secret_value(), model)
        )
        self._log = get_logger("adapter.llm_gemini")

    async def evaluate(
        self,
        listing: Listing,
        entry: WishlistEntry,
    ) -> ListingEvaluation:
        # Pre-flight budget guard — no LLM call when the listing's price
        # exceeds every configured ceiling.
        if exceeds_all_ceilings(listing, entry):
            return budget_short_circuit_evaluation(listing, entry)

        prompt = build_evaluation_prompt(listing, entry)
        raw = await self._call(prompt)

        try:
            json_blob = extract_json_object(raw)
            parsed = GeminiEvalResponse.model_validate_json(json_blob)
        except (ValidationError, ValueError) as exc:
            self._log.error(
                "llm_eval_failed",
                extra={
                    "error_class": "LlmEvaluationError",
                    "listing_id": listing.listing_id,
                    "marketplace": listing.marketplace,
                },
            )
            raise LlmEvaluationError(f"malformed Gemini response: {raw[:200]}") from exc

        return ListingEvaluation(
            listing_id=listing.listing_id,
            entry_key=entry.entry_key,
            confidence=parsed.confidence,
            one_line_take=clip_one_line_take(parsed.one_line_take),
            is_container=parsed.is_container,
            wrapper_text=parsed.wrapper_text,
            extracted_text=parsed.extracted_text,
            evaluated_at=datetime.now(UTC),
            cache_hit=False,
        )


# ─────────────────────────────────────────────────────────────────────────
# Default callable — wraps google.genai
# ─────────────────────────────────────────────────────────────────────────


def _build_default_call(api_key: str, model: str) -> GeminiCallable:
    """Construct the production ``GeminiCallable`` backed by google.genai.

    Imports happen lazily so tests that inject their own ``call`` don't
    pull the SDK at import time — and so the adapter-discipline lint
    sees google.genai used exclusively inside this adapter package.
    """
    # Imports kept inside the factory:
    # - keeps the SDK out of the module-level import graph for tests
    # - matches NFR-I3 (provider-swappable) by making the SDK a runtime
    #   dependency of this specific adapter only.
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types

    client = genai.Client(api_key=api_key)
    # Classification workload: disable 2.5-flash's thinking so each eval
    # costs/behaves like the retired 2.0-flash (0 reasoning tokens).
    config = genai_types.GenerateContentConfig(
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0)
    )

    async def _call(prompt: str) -> str:
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )
        except genai_errors.APIError as exc:
            if getattr(exc, "code", None) == 429 or "rate" in str(exc).lower():
                raise LlmRateLimited(str(exc)) from exc
            raise LlmEvaluationError(f"Gemini API error: {exc}") from exc
        return response.text or ""

    return _call
