"""LLM evaluation prompt — Story 3.9 / FR13 / FR14 / FR15 / FR17.

The single source of truth for how the LLM is asked the matching
question. Every concrete :class:`ListingEvaluator` (Gemini Flash,
GPT-4o-mini, Claude Haiku) uses :func:`build_evaluation_prompt` so the
wording, the output schema, and the (c3) scope constraint are
identical across providers.

FR17 structural enforcement
---------------------------
The prompt asks ONE question — "does this listing match this wishlist
entry?". It does NOT ask for resale value, margin, market price,
arbitrage signal, or "good deal" framing. The
:data:`FORBIDDEN_PROMPT_TERMS` constant declares what cannot appear in
the prompt body; a unit test parametrizes over those terms and asserts
their absence on representative payloads. Adding any of them is a PRD
amendment.
"""

from __future__ import annotations

import json
from typing import Final

from salvager.domain.listing import Listing
from salvager.domain.wishlist import WishlistEntry

# Words that, if present in the prompt body, would invite the LLM to
# produce arbitrage-flavored output. The unit test asserts none of them
# appear in any rendered prompt; if a future prompt-improvement PR
# accidentally introduces one, CI fails loud.
#: Cache invalidation sentinel — bump this every time the prompt body
#: changes meaningfully. The LLM evaluation cache (Story 3.10) keys
#: stored evaluations by ``(listing_url, prompt_version)`` so a bump
#: causes cache misses on every entry the next time we evaluate,
#: forcing a fresh LLM call with the new prompt. The constant lives
#: here (next to the prompt body) so a PR that changes the prompt and
#: forgets to bump this is caught in code review.
PROMPT_VERSION: Final[str] = "v2"


FORBIDDEN_PROMPT_TERMS: Final[frozenset[str]] = frozenset(
    {
        "resale",
        "resale value",
        "margin",
        "market price",
        "arbitrage",
        "profit",
        "flip",
        "good deal",
        "great deal",
        "underpriced",
        "fair price",
    }
)

_OUTPUT_SCHEMA = {
    "is_match": "boolean — true only if the listing IS the wishlisted part (or a container of it)",
    "confidence": "low | medium | high — how certain you are of is_match",
    "one_line_take": "string, <= 120 chars, specific to this listing",
    "is_container": "boolean — true if the listing is a wrapper containing the wishlisted part",
    "wrapper_text": "string or null — the quoted phrase identifying the wrapper",
    "extracted_text": "string or null — quoted phrase identifying the part inside",
}


def build_evaluation_prompt(listing: Listing, entry: WishlistEntry) -> str:
    """Render the wishlist-anchored prompt for one ``(listing, entry)`` pair.

    The output is a plain string the adapter sends to the model — no
    template engine, no escape gymnastics. The model is instructed to
    reply with JSON only.
    """
    entry_block = _entry_context(entry)
    listing_block = _listing_context(listing)
    schema_block = json.dumps(_OUTPUT_SCHEMA, indent=2)

    return (
        "You are a matching judge for a second-hand hardware monitoring agent.\n"
        "Decide whether ONE listing matches ONE wishlist entry. Answer in JSON only.\n"
        "\n"
        "## Wishlist entry\n"
        f"{entry_block}\n"
        "\n"
        "## Listing under review\n"
        f"{listing_block}\n"
        "\n"
        "## Your single question\n"
        "Does this listing match this wishlist entry?\n"
        "\n"
        "Rules:\n"
        "- is_match is your verdict. Set it false whenever the listing is a different\n"
        "  part, however similar (other capacity, speed, form factor, model).\n"
        "- Confidence reflects how certain you are of that verdict: a clear mismatch\n"
        "  is is_match=false with confidence high.\n"
        "- If the listing wraps the part (e.g. a NAS that includes the wishlisted drive),\n"
        "  set is_container=true and quote the wrapper phrase in wrapper_text.\n"
        "- extracted_text quotes the listing language that identifies the part itself.\n"
        "- one_line_take MUST be specific to THIS listing (not a generic 'looks like a match').\n"
        "- Reply with strict JSON only — no markdown fences, no prose before or after.\n"
        "\n"
        "## Required output schema\n"
        f"{schema_block}\n"
    )


def _entry_context(entry: WishlistEntry) -> str:
    """Render a wishlist entry as a block of plain key/value lines."""
    ceilings: list[str] = []
    if entry.max_price_solo is not None:
        ceilings.append(f"solo <= EUR {entry.max_price_solo}")
    if entry.max_price_in_device is not None:
        ceilings.append(f"in_device <= EUR {entry.max_price_in_device}")
    ceiling_line = "; ".join(ceilings) if ceilings else "none configured"

    lines = [
        f"- display_name: {entry.display_name}",
        f"- manufacturer: {entry.manufacturer}",
        f"- model: {entry.model}",
        f"- ref: {entry.ref}",
        f"- type: {entry.type}",
        f"- keywords: {json.dumps(entry.keywords, ensure_ascii=False)}",
        f"- container_keywords: {json.dumps(entry.container_keywords, ensure_ascii=False)}",
        f"- price_ceilings: {ceiling_line}",
        f"- confidence_threshold: {entry.confidence_threshold}",
    ]
    return "\n".join(lines)


def _listing_context(listing: Listing) -> str:
    """Render a listing as a block of plain key/value lines."""
    photos = json.dumps(listing.photo_urls, ensure_ascii=False)
    lines = [
        f"- marketplace: {listing.marketplace}",
        f"- title: {listing.title}",
        f"- description: {listing.description}",
        f"- price_eur: {listing.price_eur}",
        f"- location: {listing.location or 'unknown'}",
        f"- photo_urls: {photos}",
        f"- url: {listing.url}",
    ]
    return "\n".join(lines)
