"""Pydantic shape for the Gemini Flash response — Story 3.9.

The model is instructed to reply with strict JSON matching this schema.
A non-conforming response surfaces as ``ValidationError`` and gets
wrapped in :class:`LlmEvaluationError` by the evaluator.

There are NO arbitrage-flavored fields here. The schema's surface area
is the structural enforcement of FR17 at the evaluator boundary —
even if the model went off-script and included a "margin" field, this
schema would silently drop it via ``extra="ignore"`` (we don't need
those values), and downstream code can't read them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ConfidenceLevel = Literal["low", "medium", "high"]


class GeminiEvalResponse(BaseModel):
    """Strict shape of a Gemini Flash evaluation reply."""

    model_config = ConfigDict(extra="ignore")

    is_match: bool
    confidence: ConfidenceLevel
    one_line_take: str = Field(min_length=1)
    is_container: bool
    wrapper_text: str | None = None
    extracted_text: str | None = None
