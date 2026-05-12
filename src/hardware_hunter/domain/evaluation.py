"""LLM-evaluation result schema — Story 3.1.

A ``ListingEvaluation`` is what the :class:`ListingEvaluator` adapter
returns after asking the model "does this listing match this wishlist
entry?". It is the bridge between the model's free-form answer and the
typed pipeline downstream of evaluation (cache, dedup, alert renderer).

``ConfidenceLevel`` is re-exported as the same Literal the wishlist
``confidence_threshold`` field uses — the alert renderer cross-compares
the two to decide whether a listing crosses the operator's bar.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ConfidenceLevel = Literal["low", "medium", "high"]


class ListingEvaluation(BaseModel):
    """One LLM evaluation against one wishlist entry.

    ``is_container`` is True when the LLM identified the listing as a
    wrapper for the wishlisted part (e.g. "NAS *including* WD Red Plus
    4TB drives"); ``wrapper_text`` carries the operator-facing quote
    that justifies that classification, and the Direction-E renderer
    uses it to split the alert body.

    ``cache_hit`` is set by the cache layer (Hermes SQLite + FTS5,
    Story 3.10) so the alert renderer can decorate cached evaluations.
    """

    model_config = ConfigDict(extra="forbid")

    listing_id: str = Field(min_length=1)
    entry_key: tuple[str, str, str]
    confidence: ConfidenceLevel
    one_line_take: str = Field(min_length=1)
    is_container: bool
    wrapper_text: str | None = None
    extracted_text: str | None = None
    evaluated_at: datetime
    cache_hit: bool = False
