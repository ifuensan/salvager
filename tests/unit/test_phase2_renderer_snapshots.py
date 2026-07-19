"""Phase 2 renderer snapshot tests — Story 5.2 (FR23 / UX-DR7).

Three fixtures cover the locked Phase 2 listing-alert anatomy:

  - ``direct``          — clean direct match (Direction A + Phase 2 row)
  - ``container``       — Direction E wrapper / extracted rows survive
  - ``missing_photo``   — photo_url None when listing.photo_urls is empty

Each render is asserted against a syrupy snapshot; the buttons are
asserted structurally so the locked ``Comprar · Saltar · Ver`` order
and the ``listing:<verb>:<id>`` callback shape can't drift unnoticed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from syrupy.assertion import SnapshotAssertion

from salvager.domain.alert import (
    AlertSnapshot,
    render_phase2_listing_alert,
)
from salvager.domain.comps import summarize_comps
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.domain.pricing import buyer_cost

_FIXED_ALERT_ID = UUID("12345678-1234-1234-1234-123456789abc")
_FIXED_TS = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
_PHASE2_MAX = Decimal("60.00")


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
        "phase": "phase2",
        "phase2_max_price_eur": _PHASE2_MAX,
        "rendered_at": _FIXED_TS,
    }
    base.update(overrides)
    return AlertSnapshot(**base)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────
# Three fixtures
# ─────────────────────────────────────────────────────────────────────────


def test_snapshot_direct(snapshot: SnapshotAssertion) -> None:
    rendered = render_phase2_listing_alert(_snapshot(), _PHASE2_MAX)
    assert rendered.text == snapshot
    # The locked confidence row carries the Phase 2 max in Spanish format.
    assert "Phase 2 max: 60,00 €" in rendered.text
    # Severity prefix flipped to the Phase 2 token.
    assert rendered.text.startswith("🟢 ")


def test_snapshot_container(snapshot: SnapshotAssertion) -> None:
    rendered = render_phase2_listing_alert(
        _snapshot(
            evaluation=_evaluation(
                is_container=True,
                wrapper_text="Synology DS220+ NAS",
                extracted_text="WD Red Plus 4TB drives",
            )
        ),
        _PHASE2_MAX,
    )
    assert rendered.text == snapshot
    # The Direction E wrapper/extracted rows still appear in Phase 2.
    assert "Wrapper:" in rendered.text
    assert "Extracted:" in rendered.text


def test_snapshot_missing_photo(snapshot: SnapshotAssertion) -> None:
    rendered = render_phase2_listing_alert(_snapshot(listing=_listing(photo_urls=[])), _PHASE2_MAX)
    assert rendered.text == snapshot
    assert rendered.photo_url is None


def test_snapshot_with_cost(snapshot: SnapshotAssertion) -> None:
    """Production Phase 2 alerts carry the ``💶`` buyer-total row on every
    dispatch since shipping-aware-pricing (v0.3.3), with the Comprar
    keyboard untouched."""
    cost = buyer_cost(_listing(), assumed_shipping_eur=Decimal("3.50"))
    rendered = render_phase2_listing_alert(_snapshot(), _PHASE2_MAX, buyer_cost=cost)
    assert rendered.text == snapshot
    assert "💶" in rendered.text
    assert rendered.inline_keyboard is not None
    assert [b.text for b in rendered.inline_keyboard[0]] == ["✅ Comprar", "❌ Saltar", "👁 Ver"]


def test_deeplink_row_present_on_phase2() -> None:
    """FR18: the Phase 2 alert carries the same deep-link row after the
    location row, with the Comprar keyboard untouched."""
    rendered = render_phase2_listing_alert(_snapshot(), _PHASE2_MAX)
    lines = rendered.text.split("\n")
    assert lines[1].startswith("📍 ")
    assert lines[2] == "🔗 [Ver anuncio en Wallapop](https://wallapop.com/item/abc123)"
    assert rendered.inline_keyboard is not None
    assert [b.text for b in rendered.inline_keyboard[0]] == ["✅ Comprar", "❌ Saltar", "👁 Ver"]


def test_snapshot_with_comps(snapshot: SnapshotAssertion) -> None:
    """The comp row renders after the Phase 2 ``Confidence … Phase 2 max``
    row and leaves the Comprar keyboard untouched (PR #7 Layer 2)."""
    comp_summary = summarize_comps([Decimal("180.00"), Decimal("200.00"), Decimal("240.00")])
    rendered = render_phase2_listing_alert(_snapshot(), _PHASE2_MAX, comp_summary=comp_summary)
    assert rendered.text == snapshot
    assert "💬 Comps" in rendered.text
    # The comp row sits below the Phase 2 max confidence row.
    assert rendered.text.index("Phase 2 max:") < rendered.text.index("💬 Comps")
    # Keyboard unchanged.
    assert rendered.inline_keyboard is not None
    assert [b.text for b in rendered.inline_keyboard[0]] == ["✅ Comprar", "❌ Saltar", "👁 Ver"]


# ─────────────────────────────────────────────────────────────────────────
# Locked keyboard — Comprar · Saltar · Ver, in that order
# ─────────────────────────────────────────────────────────────────────────


def test_phase2_keyboard_is_buy_skip_view_in_order() -> None:
    rendered = render_phase2_listing_alert(_snapshot(), _PHASE2_MAX)
    assert rendered.inline_keyboard is not None
    assert len(rendered.inline_keyboard) == 1
    row = rendered.inline_keyboard[0]
    assert [button.text for button in row] == ["✅ Comprar", "❌ Saltar", "👁 Ver"]
    alert_id = str(_FIXED_ALERT_ID)
    assert [button.callback_data for button in row] == [
        f"listing:buy:{alert_id}",
        f"listing:skip:{alert_id}",
        f"listing:view:{alert_id}",
    ]
