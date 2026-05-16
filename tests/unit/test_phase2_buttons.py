"""Phase 2 button-row property test — Story 5.16 (UX-DR4 / UX-DR5).

Two locked invariants on every rendered Phase 2 listing alert:

  - **UX-DR4 — vocabulary + order**: the inline keyboard is exactly
    one row of three buttons, in the order
    ``[✅ Comprar, ❌ Saltar, 👁 Ver]``. ``Comprar`` first parks the
    affirmative action in the dominant left slot; ``Ver`` last is the
    information escape hatch.
  - **UX-DR5 — callback contract**: every ``callback_data`` matches
    ``<surface>:<verb>:<id>`` and stays under Telegram's 64-byte cap.

The properties are exercised over a small matrix of ``AlertSnapshot``
shapes (direct match, container, missing photo, escape-prone display
name) so a renderer change that smuggles in a fourth button or shuffles
the order is caught immediately.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from hardware_hunter.domain.alert import (
    BUTTON_LABELS,
    AlertSnapshot,
    InlineButton,
    render_phase2_listing_alert,
)
from hardware_hunter.domain.evaluation import ListingEvaluation
from hardware_hunter.domain.listing import Listing

_FIXED_ALERT_ID = UUID("12345678-1234-1234-1234-123456789abc")
_FIXED_TS = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
_PHASE2_MAX = Decimal("60.00")

# The locked label order — re-asserted here so any reshuffle of
# BUTTON_LABELS (a UX regression) is caught even if the row builder
# is rewritten.
_LOCKED_ORDER: tuple[str, str, str] = (
    BUTTON_LABELS["buy"],
    BUTTON_LABELS["skip_phase2"],
    BUTTON_LABELS["view"],
)


def _listing(**overrides: object) -> Listing:
    base: dict[str, Any] = {
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
    return Listing(**base)


def _evaluation(**overrides: object) -> ListingEvaluation:
    base: dict[str, Any] = {
        "listing_id": "abc123",
        "entry_key": ("Western Digital", "WD Red Plus 4TB", "WD40EFPX"),
        "confidence": "high",
        "one_line_take": "WD Red Plus 4TB at 55€ — strong match.",
        "is_container": False,
        "evaluated_at": _FIXED_TS,
    }
    base.update(overrides)
    return ListingEvaluation(**base)


def _snapshot(
    *,
    listing_overrides: dict[str, Any] | None = None,
    evaluation_overrides: dict[str, Any] | None = None,
    **overrides: Any,
) -> AlertSnapshot:
    base: dict[str, Any] = {
        "alert_id": _FIXED_ALERT_ID,
        "entry_key": ("Western Digital", "WD Red Plus 4TB", "WD40EFPX"),
        "entry_display_name": "WD Red Plus 4TB (WD40EFPX)",
        "listing": _listing(**(listing_overrides or {})),
        "evaluation": _evaluation(**(evaluation_overrides or {})),
        "phase": "phase2",
        "phase2_max_price_eur": _PHASE2_MAX,
        "rendered_at": _FIXED_TS,
    }
    base.update(overrides)
    return AlertSnapshot(**base)


# The matrix the property tests sweep over — keep small but diverse.
_MATRIX: dict[str, AlertSnapshot] = {
    "direct": _snapshot(),
    "container": _snapshot(
        evaluation_overrides={
            "is_container": True,
            "wrapper_text": "Pack 4x HDD",
            "extracted_text": "WD Red Plus 4TB inside",
        },
    ),
    "missing_photo": _snapshot(listing_overrides={"photo_urls": []}),
    "escape_prone_name": _snapshot(entry_display_name="WD_Red+Plus *4TB* (ref!)"),
}


def _row(snapshot: AlertSnapshot) -> list[InlineButton]:
    rendered = render_phase2_listing_alert(snapshot, _PHASE2_MAX)
    assert rendered.inline_keyboard is not None, "Phase 2 alerts always carry a keyboard"
    assert len(rendered.inline_keyboard) == 1, "Phase 2 keyboard is exactly one row"
    return rendered.inline_keyboard[0]


# ─────────────────────────────────────────────────────────────────────────
# UX-DR4 — locked vocabulary + order
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", list(_MATRIX), ids=lambda n: n)
def test_button_row_is_exactly_three_buttons(name: str) -> None:
    row = _row(_MATRIX[name])
    assert len(row) == 3


@pytest.mark.parametrize("name", list(_MATRIX), ids=lambda n: n)
def test_button_labels_are_the_locked_vocabulary_in_order(name: str) -> None:
    row = _row(_MATRIX[name])
    assert tuple(b.text for b in row) == _LOCKED_ORDER


def test_locked_order_pins_comprar_left_ver_right() -> None:
    """A regression guard: even if a future change re-orders
    BUTTON_LABELS, the Phase 2 row must keep Comprar in slot 0 and
    Ver in slot 2 — the affirmative action sits in the visually
    dominant left slot, the read-only escape on the right."""
    row = _row(_MATRIX["direct"])
    assert row[0].text == "✅ Comprar"
    assert row[1].text == "❌ Saltar"
    assert row[2].text == "👁 Ver"


# ─────────────────────────────────────────────────────────────────────────
# UX-DR5 — callback_data contract
# ─────────────────────────────────────────────────────────────────────────


_EXPECTED_VERBS: tuple[str, str, str] = ("buy", "skip", "view")


@pytest.mark.parametrize("name", list(_MATRIX), ids=lambda n: n)
def test_callback_data_matches_locked_format(name: str) -> None:
    row = _row(_MATRIX[name])
    alert_id = str(_MATRIX[name].alert_id)
    for button, verb in zip(row, _EXPECTED_VERBS, strict=True):
        assert button.callback_data == f"listing:{verb}:{alert_id}"


@pytest.mark.parametrize("name", list(_MATRIX), ids=lambda n: n)
def test_callback_data_stays_within_telegram_64_byte_cap(name: str) -> None:
    row = _row(_MATRIX[name])
    for button in row:
        assert len(button.callback_data.encode("utf-8")) <= 64


@pytest.mark.parametrize("name", list(_MATRIX), ids=lambda n: n)
def test_every_button_shares_the_same_alert_id(name: str) -> None:
    """The three buttons dispatch to the same AlertSnapshot — the
    callback handler looks up the snapshot once and branches on the
    verb. Diverging IDs would break that contract silently."""
    row = _row(_MATRIX[name])
    ids = {button.callback_data.rsplit(":", 1)[1] for button in row}
    assert len(ids) == 1
    assert ids.pop() == str(_MATRIX[name].alert_id)
