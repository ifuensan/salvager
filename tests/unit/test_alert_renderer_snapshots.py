"""Phase 1 renderer snapshot tests — Story 3.15 (FR22 mechanical drift).

Six fixtures cover every Phase 1 rendering shape the daemon emits:

  - ``direct``                — Direction A baseline (a clean direct match)
  - ``container``             — Direction E indented wrapper / extracted rows
  - ``low_confidence``        — confidence: low (renders without alarm)
  - ``missing_photo``         — photo_url None when listing.photo_urls is empty
  - ``long_llm_take``         — multi-sentence LLM commentary doesn't wrap unsafely
  - ``special_chars_in_title``— every MarkdownV2 reserved char in user content
                                gets escaped so the markup can't break

Each render is checked against its tracked syrupy snapshot in
``__snapshots__/test_alert_renderer_snapshots.ambr``. A CI failure
shows a precise text diff of the drift — the renderer format is
locked at v1 per FR22 and these snapshots are the enforcement
mechanism.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from syrupy.assertion import SnapshotAssertion

from hardware_hunter.domain.alert import (
    AlertSnapshot,
    render_phase1_listing_alert,
)
from hardware_hunter.domain.evaluation import ListingEvaluation
from hardware_hunter.domain.listing import Listing

# Stable values so syrupy diffs reflect renderer drift, never clock drift.
_FIXED_ALERT_ID = UUID("12345678-1234-1234-1234-123456789abc")
_FIXED_TS = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


def _listing(**overrides: object) -> Listing:
    base: dict[str, object] = {
        "listing_id": "abc123",
        "marketplace": "wallapop",
        "url": "https://wallapop.com/item/abc123",
        "title": "WD Red Plus 4TB",
        "description": "Como nuevo, en caja.",
        "price_eur": Decimal("55.00"),
        "location": "Madrid",
        "photo_urls": ["https://cdn/photo.jpg"],
        "fetched_at": _FIXED_TS,
    }
    base.update(overrides)
    return Listing(**base)  # type: ignore[arg-type]


def _evaluation(**overrides: object) -> ListingEvaluation:
    base: dict[str, object] = {
        "listing_id": "abc123",
        "entry_key": ("Western Digital", "WD Red Plus 4TB", "WD40EFPX"),
        "confidence": "high",
        "one_line_take": "WD Red Plus 4TB at 55€ — strong match.",
        "is_container": False,
        "evaluated_at": _FIXED_TS,
    }
    base.update(overrides)
    return ListingEvaluation(**base)  # type: ignore[arg-type]


def _snapshot(**overrides: object) -> AlertSnapshot:
    base: dict[str, object] = {
        "alert_id": _FIXED_ALERT_ID,
        "entry_key": ("Western Digital", "WD Red Plus 4TB", "WD40EFPX"),
        "entry_display_name": "WD Red Plus 4TB (WD40EFPX)",
        "listing": _listing(),
        "evaluation": _evaluation(),
        "phase": "phase1",
        "rendered_at": _FIXED_TS,
    }
    base.update(overrides)
    return AlertSnapshot(**base)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────
# Six fixtures (Story 3.15 AC)
# ─────────────────────────────────────────────────────────────────────────


def test_snapshot_direct(snapshot: SnapshotAssertion) -> None:
    rendered = render_phase1_listing_alert(_snapshot())
    assert rendered.text == snapshot


def test_snapshot_container(snapshot: SnapshotAssertion) -> None:
    rendered = render_phase1_listing_alert(
        _snapshot(
            evaluation=_evaluation(
                is_container=True,
                wrapper_text="Synology DS220+ NAS",
                extracted_text="WD Red Plus 4TB drives",
            )
        )
    )
    assert rendered.text == snapshot


def test_snapshot_low_confidence(snapshot: SnapshotAssertion) -> None:
    rendered = render_phase1_listing_alert(
        _snapshot(
            evaluation=_evaluation(
                confidence="low",
                one_line_take="Title hints at WD Red, ref not visible — uncertain.",
            )
        )
    )
    assert rendered.text == snapshot


def test_snapshot_missing_photo(snapshot: SnapshotAssertion) -> None:
    rendered = render_phase1_listing_alert(_snapshot(listing=_listing(photo_urls=[])))
    assert rendered.text == snapshot
    assert rendered.photo_url is None


def test_snapshot_long_llm_take(snapshot: SnapshotAssertion) -> None:
    """A multi-sentence LLM take must survive escape + interpolation without
    breaking the row layout."""
    long_take = (
        "Coincide con el modelo del wishlist (WD Red Plus 4TB, ref WD40EFPX) "
        "y el precio está claramente por debajo de mercado; descripción "
        "menciona uso intensivo en NAS — riesgo de SMART degradado, pero el "
        "precio compensa para un homelab."
    )
    rendered = render_phase1_listing_alert(
        _snapshot(evaluation=_evaluation(one_line_take=long_take))
    )
    assert rendered.text == snapshot


def test_snapshot_special_chars_in_title(snapshot: SnapshotAssertion) -> None:
    """Every MarkdownV2-reserved char appearing in a real user-supplied
    title must be escaped — otherwise a stray ``*`` or ``[`` would either
    break the markup or open an injection path. The fixture jams several
    reserved chars into the title and location at once."""
    rendered = render_phase1_listing_alert(
        _snapshot(
            entry_display_name="WD_Red+Plus *4TB* (ref!)",
            listing=_listing(
                title="WD_Red+Plus *4TB* [WD40EFPX] (caja!)",
                location="L'Hospitalet de Llobregat",
            ),
            evaluation=_evaluation(
                one_line_take="100% match: title=ref. Price < 60€!",
            ),
        )
    )
    assert rendered.text == snapshot
