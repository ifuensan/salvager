"""Claude Haiku :class:`ListingEvaluator` — NFR-I3 alternate provider.

Mirrors the Gemini Flash adapter line-for-line:

- Same pre-flight budget guard (no LLM call when price > every ceiling).
- Same prompt (``domain.prompts.build_evaluation_prompt`` is shared).
- Same response schema (:class:`ClaudeEvalResponse` is identical to
  :class:`GeminiEvalResponse` — separate types keep providers
  independently evolvable).
- Same JSON extraction (robust to markdown fences / surrounding prose).
- Same error mapping: 429 / rate-limit → :class:`LlmRateLimited`,
  everything else → :class:`LlmEvaluationError`.

The default model is Claude Haiku 4.5 (``claude-haiku-4-5-20251001``).
Tests inject a :data:`ClaudeCallable` so the SDK never loads in unit
tests, and so the adapter-discipline lint sees ``anthropic`` used
exclusively inside this adapter package (NFR-M1).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from pydantic import SecretStr, ValidationError

from salvager.adapters._llm_evaluator_shared import (
    MAX_ONE_LINE_TAKE,
    budget_short_circuit_evaluation,
    exceeds_all_ceilings,
    extract_json_object,
)
from salvager.adapters.llm_claude.schema import ClaudeEvalResponse
from salvager.domain.errors import LlmEvaluationError, LlmRateLimited
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.domain.prompts import build_evaluation_prompt
from salvager.domain.wishlist import WishlistEntry
from salvager.interfaces.listing_evaluator import ListingEvaluator
from salvager.observability.logging import get_logger

#: Take any prompt string, return the model's raw text reply.
ClaudeCallable = Callable[[str], Awaitable[str]]

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_MAX_TOKENS = 512


class ClaudeHaikuEvaluator(ListingEvaluator):
    """LLM-backed match judge — Claude Haiku, alternate to Gemini Flash."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        model: str = _DEFAULT_MODEL,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        call: ClaudeCallable | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._call: ClaudeCallable = (
            call
            if call is not None
            else _build_default_call(api_key.get_secret_value(), model, max_tokens)
        )
        self._log = get_logger("adapter.llm_claude")

    async def evaluate(
        self,
        listing: Listing,
        entry: WishlistEntry,
    ) -> ListingEvaluation:
        # Pre-flight budget guard — no LLM call when the listing's
        # price exceeds every configured ceiling.
        if exceeds_all_ceilings(listing, entry):
            return budget_short_circuit_evaluation(listing, entry)

        prompt = build_evaluation_prompt(listing, entry)
        raw = await self._call(prompt)

        try:
            json_blob = extract_json_object(raw)
            parsed = ClaudeEvalResponse.model_validate_json(json_blob)
        except (ValidationError, ValueError) as exc:
            self._log.error(
                "llm_eval_failed",
                extra={
                    "error_class": "LlmEvaluationError",
                    "listing_id": listing.listing_id,
                    "marketplace": listing.marketplace,
                },
            )
            raise LlmEvaluationError(f"malformed Claude response: {raw[:200]}") from exc

        if len(parsed.one_line_take) > MAX_ONE_LINE_TAKE:
            raise LlmEvaluationError(
                f"one_line_take too long ({len(parsed.one_line_take)} > {MAX_ONE_LINE_TAKE} chars)"
            )

        return ListingEvaluation(
            listing_id=listing.listing_id,
            entry_key=entry.entry_key,
            confidence=parsed.confidence,
            one_line_take=parsed.one_line_take,
            is_container=parsed.is_container,
            wrapper_text=parsed.wrapper_text,
            extracted_text=parsed.extracted_text,
            evaluated_at=datetime.now(UTC),
            cache_hit=False,
        )


# ─────────────────────────────────────────────────────────────────────────
# Default callable — wraps anthropic.AsyncAnthropic
# ─────────────────────────────────────────────────────────────────────────


def _build_default_call(api_key: str, model: str, max_tokens: int) -> ClaudeCallable:
    """Construct the production ``ClaudeCallable`` backed by anthropic.

    Imports happen lazily so tests that inject their own ``call`` don't
    pull the SDK at import time — and so the adapter-discipline lint
    sees ``anthropic`` used exclusively inside this adapter package
    (NFR-M1).
    """
    # Imports kept inside the factory:
    # - keeps the SDK out of the module-level import graph for tests
    # - matches NFR-I3 (provider-swappable) by making the SDK a runtime
    #   dependency of this specific adapter only.
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key)

    async def _call(prompt: str) -> str:
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.RateLimitError as exc:
            raise LlmRateLimited(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            # Some 429s arrive as generic APIStatusError depending on
            # the SDK version; treat any status_code == 429 as rate
            # limit for parity with the Gemini adapter.
            if getattr(exc, "status_code", None) == 429:
                raise LlmRateLimited(str(exc)) from exc
            raise LlmEvaluationError(f"Claude API error: {exc}") from exc
        except anthropic.APIError as exc:
            raise LlmEvaluationError(f"Claude API error: {exc}") from exc

        # Anthropic response: .content is a list of ContentBlock; for
        # plain text replies the first block has .text. Concatenate
        # all text blocks defensively.
        text_parts: list[str] = []
        for block in response.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                text_parts.append(text)
        return "".join(text_parts)

    return _call
