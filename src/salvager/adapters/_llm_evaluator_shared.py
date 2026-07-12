"""Helpers shared by every LLM-backed :class:`ListingEvaluator` adapter.

Lives at adapter level (private — leading underscore) rather than in
``domain/`` because the budget guard semantics are domain-shaped but
the JSON extraction is a wire-format concern; keeping both in one
spot avoids file proliferation and the cross-package import noise
that two separate homes would create.

Adapter-discipline (NFR-M1) is unaffected: nothing here imports a
provider SDK; concrete adapters keep their SDK imports lazy and
local.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.domain.wishlist import WishlistEntry

#: Hard cap on the model's ``one_line_take`` field — operators expect a
#: Telegram-renderable single line, not a paragraph. Over-long takes are
#: clipped (:func:`clip_one_line_take`), never discarded: the length is a
#: display constraint, and models routinely need >120 chars to describe
#: multi-variant "lote" listings, which made a raise-on-overflow fail the
#: same listings deterministically every cycle.
MAX_ONE_LINE_TAKE = 120


def clip_one_line_take(take: str) -> str:
    """Clip ``take`` to :data:`MAX_ONE_LINE_TAKE` chars (ellipsis-terminated)."""
    if len(take) <= MAX_ONE_LINE_TAKE:
        return take
    return take[: MAX_ONE_LINE_TAKE - 1] + "…"


# ─────────────────────────────────────────────────────────────────────────
# Budget short-circuit
# ─────────────────────────────────────────────────────────────────────────


def exceeds_all_ceilings(listing: Listing, entry: WishlistEntry) -> bool:
    """True iff the listing price is strictly above every configured ceiling.

    A None ceiling means "container detection disabled for that variant"
    (FR5) — treat it as not-a-bound, so a None ceiling alone never
    triggers the short-circuit.
    """
    price = listing.price_eur
    ceilings = [c for c in (entry.max_price_solo, entry.max_price_in_device) if c is not None]
    if not ceilings:
        return False
    return all(price > ceiling for ceiling in ceilings)


def budget_short_circuit_evaluation(
    listing: Listing,
    entry: WishlistEntry,
) -> ListingEvaluation:
    """Return the ``confidence=low`` verdict adapters use when the
    pre-flight budget guard fires — same shape for every provider so
    the cache + downstream rendering treat both paths identically.
    """
    return ListingEvaluation(
        listing_id=listing.listing_id,
        entry_key=entry.entry_key,
        confidence="low",
        one_line_take=(f"EUR {listing.price_eur} — price exceeds wishlist max."),
        is_container=False,
        wrapper_text=None,
        extracted_text=None,
        evaluated_at=datetime.now(UTC),
        cache_hit=False,
    )


# ─────────────────────────────────────────────────────────────────────────
# JSON extraction
# ─────────────────────────────────────────────────────────────────────────


def extract_json_object(raw: str) -> str:
    """Return the first complete JSON object substring of ``raw``.

    Robust to LLMs that wrap JSON in markdown code fences or pad it
    with explanatory prose, and — unlike a greedy ``\\{.*\\}`` regex —
    also robust to extra brace-containing text *after* the JSON object
    (e.g. the model appending a "PS:" with literal braces). Uses
    :class:`json.JSONDecoder.raw_decode` to find the first ``{`` that
    starts a parseable object and returns exactly those bytes.

    Raises ``ValueError`` when no parseable object is present —
    adapters wrap that as :class:`LlmEvaluationError`.
    """
    if not raw or not raw.strip():
        raise ValueError("empty LLM response")

    decoder = json.JSONDecoder()
    idx = raw.find("{")
    while idx != -1:
        try:
            obj, consumed = decoder.raw_decode(raw[idx:])
        except json.JSONDecodeError:
            idx = raw.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            return raw[idx : idx + consumed]
        idx = raw.find("{", idx + 1)

    raise ValueError("no JSON object found in response")
