"""LLM-evaluation result schema — Story 3.1.

A ``ListingEvaluation`` is what the :class:`ListingEvaluator` adapter
returns after asking the model "does this listing match this wishlist
entry?". It is the bridge between the model's free-form answer and the
typed pipeline downstream of evaluation (cache, dedup, alert renderer).

``ConfidenceLevel`` is re-exported as the same Literal the wishlist
``confidence_threshold`` field uses; :meth:`ListingEvaluation.clears`
is the one place that compares the two.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

ConfidenceLevel = Literal["low", "medium", "high"]

#: Confidence ordering: low < medium < high.
CONFIDENCE_RANK: Final[dict[str, int]] = {"low": 0, "medium": 1, "high": 2}


class ListingEvaluation(BaseModel):
    """One LLM evaluation against one wishlist entry.

    ``is_container`` is True when the LLM identified the listing as a
    wrapper for the wishlisted part (e.g. "NAS *including* WD Red Plus
    4TB drives"); ``wrapper_text`` carries the operator-facing quote
    that justifies that classification, and the Direction-E renderer
    uses it to split the alert body.

    ``is_match`` is the model's verdict; ``confidence`` is how sure it
    is of that verdict, so a clear mismatch arrives as
    ``is_match=False, confidence="high"``. It defaults to True because
    every evaluation persisted before the field existed (alert
    snapshots) was an alerted match.

    ``cache_hit`` is set by the cache layer (Story 3.10) so the alert
    renderer can decorate cached evaluations. The cache is backed by a
    local SQLite file separate from ``salvager.db``.
    """

    model_config = ConfigDict(extra="forbid")

    listing_id: str = Field(min_length=1)
    entry_key: tuple[str, str, str]
    is_match: bool = True
    confidence: ConfidenceLevel
    one_line_take: str = Field(min_length=1)
    is_container: bool
    wrapper_text: str | None = None
    extracted_text: str | None = None
    evaluated_at: datetime
    cache_hit: bool = False

    def clears(self, threshold: str) -> bool:
        """True iff this evaluation should alert under ``threshold``.

        A confident "this is not the part" never clears, whatever the
        threshold: confidence describes the verdict, not the match.
        """
        return self.is_match and CONFIDENCE_RANK[self.confidence] >= CONFIDENCE_RANK[threshold]
