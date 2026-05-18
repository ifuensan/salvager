"""Pydantic shape for the Claude Haiku response — mirrors GeminiEvalResponse.

The same wishlist-anchored prompt (``domain/prompts.py``) is used for
every provider, so every provider's response shape must match. Keeping
a per-provider schema (rather than a shared one) lets us evolve them
independently if a provider develops quirks; today both are identical.

There are NO arbitrage-flavored fields here (FR17 structural guard).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ConfidenceLevel = Literal["low", "medium", "high"]


class ClaudeEvalResponse(BaseModel):
    """Strict shape of a Claude Haiku evaluation reply."""

    model_config = ConfigDict(extra="ignore")

    confidence: ConfidenceLevel
    one_line_take: str = Field(min_length=1)
    is_container: bool
    wrapper_text: str | None = None
    extracted_text: str | None = None
