"""``ListingEvaluator`` ABC — Story 3.2 (NFR-I3 / FR13-FR17).

The port through which the poll loop asks a model "does this listing
match this wishlist entry?". Concrete adapters at v1:

  - ``adapters/llm_gemini``   — default (gemini-flash)
  - ``adapters/llm_openai``   — gpt-4o-mini (optional)
  - ``adapters/llm_claude``   — claude-haiku (optional)

The wishlist-anchored prompt lives in ``domain/prompts.py`` (Story 3.9)
and is consumed by every concrete evaluator; the prompt is the single
source of truth for what evaluation means in this project.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from hardware_hunter.domain.evaluation import ListingEvaluation
from hardware_hunter.domain.listing import Listing
from hardware_hunter.domain.wishlist import WishlistEntry


class ListingEvaluator(ABC):
    """Port for the LLM-backed match question."""

    @abstractmethod
    async def evaluate(
        self,
        listing: Listing,
        entry: WishlistEntry,
    ) -> ListingEvaluation:
        """Ask the model whether ``listing`` matches ``entry``.

        Adapters must populate every required field on the returned
        :class:`ListingEvaluation` — even on a "no match" verdict
        (which is encoded as ``confidence="low"`` per the locked
        prompt contract, not as None).
        """


class ListingEvaluatorError(RuntimeError):
    """Adapter could not produce a verdict (rate-limited, malformed
    response, network failure, etc.). The cause lives in ``__cause__``.
    """
