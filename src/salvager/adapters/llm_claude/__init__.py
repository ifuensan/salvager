"""Claude Haiku :class:`ListingEvaluator` adapter — NFR-I3 alternate provider.

Public surface:

  - :class:`ClaudeHaikuEvaluator` — the concrete :class:`ListingEvaluator`
  - :class:`ClaudeEvalResponse` — pydantic shape for the model's reply
  - :data:`ClaudeCallable` — the protocol callers can inject for tests
"""

from salvager.adapters.llm_claude.evaluator import (
    ClaudeCallable,
    ClaudeHaikuEvaluator,
)
from salvager.adapters.llm_claude.schema import ClaudeEvalResponse

__all__ = [
    "ClaudeCallable",
    "ClaudeEvalResponse",
    "ClaudeHaikuEvaluator",
]
