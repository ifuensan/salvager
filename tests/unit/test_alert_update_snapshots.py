"""Edit-surface snapshot tests — release-audit re-capture for v0.4.x.

The live-updating alerts feature (v0.4.0, edit-alerts-on-state-change)
edits a dispatched alert in place: a replaceable status banner is
prepended to a freshly re-rendered body, and a big price drop
additionally sends a short NEW ping message. Five fixtures lock the
edit-surface shapes for the Story 5.17 release audit (ROADMAP
criterion 3), exactly like the listing/buy snapshot files lock theirs:

  - ``edited_reserved``        — 🔴 RESERVADO banner over a Phase 1 body
  - ``edited_available``       — 🟢 Disponible de nuevo (flip-back)
  - ``edited_price_drop``      — 📉 banner carrying new + previous price
  - ``edited_reserved_phase2`` — Phase 2 body + dead 🔴 Reservado keyboard
  - ``price_drop_ping``        — the standalone notification message

The bodies carry the ``💶`` buyer-total row because the production edit
path (``orchestration/alert_updater.py``) always re-renders with
``buyer_cost`` — and omit the comp row, which the updater drops by
design (an in-cycle signal, not current data).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from syrupy.assertion import SnapshotAssertion

from salvager.domain.alert import (
    AlertSnapshot,
    RenderedAlert,
    apply_update_banner,
    phase2_dead_reserved_row,
    render_phase1_listing_alert,
    render_phase2_listing_alert,
    render_price_drop_ping,
    update_banner_line,
)
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.domain.pricing import buyer_cost

_FIXED_ALERT_ID = UUID("12345678-1234-1234-1234-123456789abc")
_FIXED_TS = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
_PHASE2_MAX = Decimal("60.00")
_ASSUMED_SHIPPING = Decimal("3.50")


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


def _phase1_base(listing: Listing | None = None) -> RenderedAlert:
    listing = listing if listing is not None else _listing()
    cost = buyer_cost(listing, assumed_shipping_eur=_ASSUMED_SHIPPING)
    return render_phase1_listing_alert(_snapshot(listing=listing), buyer_cost=cost)


# ─────────────────────────────────────────────────────────────────────────
# Five fixtures
# ─────────────────────────────────────────────────────────────────────────


def test_snapshot_edited_reserved(snapshot: SnapshotAssertion) -> None:
    base = _phase1_base()
    edited = apply_update_banner(base, update_banner_line("reserved"), base.inline_keyboard)
    assert edited.text == snapshot
    assert edited.text.split("\n")[0] == "🔴 RESERVADO"


def test_snapshot_edited_available(snapshot: SnapshotAssertion) -> None:
    base = _phase1_base()
    edited = apply_update_banner(base, update_banner_line("available"), base.inline_keyboard)
    assert edited.text == snapshot
    assert edited.text.split("\n")[0] == "🟢 Disponible de nuevo"


def test_snapshot_edited_price_drop(snapshot: SnapshotAssertion) -> None:
    """The body reflects the NEW price everywhere (headline + 💶 row); the
    banner alone carries the price the operator last saw."""
    base = _phase1_base(_listing(price_eur=Decimal("48.00")))
    banner = update_banner_line(
        "price_drop",
        old_price_eur=Decimal("55.00"),
        new_price_eur=Decimal("48.00"),
    )
    edited = apply_update_banner(base, banner, base.inline_keyboard)
    assert edited.text == snapshot
    assert edited.text.split("\n")[0].startswith("📉 ")
    assert "antes" in edited.text


def test_snapshot_edited_reserved_phase2(snapshot: SnapshotAssertion) -> None:
    """A reserved Phase 2 listing swaps ✅ Comprar for the non-tappable
    🔴 Reservado badge (the ``noop`` verb) while keeping 👁 Ver."""
    listing = _listing()
    cost = buyer_cost(listing, assumed_shipping_eur=_ASSUMED_SHIPPING)
    base = render_phase2_listing_alert(
        _snapshot(phase="phase2", phase2_max_price_eur=_PHASE2_MAX, listing=listing),
        _PHASE2_MAX,
        buyer_cost=cost,
    )
    keyboard = [phase2_dead_reserved_row(str(_FIXED_ALERT_ID))]
    edited = apply_update_banner(base, update_banner_line("reserved"), keyboard)
    assert edited.text == snapshot
    assert edited.inline_keyboard is not None
    assert [b.text for b in edited.inline_keyboard[0]] == ["🔴 Reservado", "👁 Ver"]
    assert edited.inline_keyboard[0][0].callback_data == f"listing:noop:{_FIXED_ALERT_ID}"


def test_snapshot_price_drop_ping(snapshot: SnapshotAssertion) -> None:
    """The standalone ping is plain text — no photo, no keyboard."""
    rendered = render_price_drop_ping(
        "WD Red Plus 4TB (WD40EFPX)",
        old_price_eur=Decimal("55.00"),
        new_price_eur=Decimal("48.00"),
    )
    assert rendered.text == snapshot
    assert rendered.photo_url is None
    assert rendered.inline_keyboard is None
