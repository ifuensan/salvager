"""Phase 1 inline-keyboard contract — Story 3.15 (UX-DR4 / UX-DR5).

The Phase 1 button row is locked at v1: every alert renders exactly
``[[👁 Ver, 🙅 Saltar, 😴 Posponer 24h]]`` with ``callback_data`` in
the ``<surface>:<verb>:<id>`` shape and ≤ 64 bytes. These tests
mechanically enforce both invariants — if a future story tries to
add a fourth button, change a label, or swap the verb vocabulary,
CI breaks here, not in a screenshot review months later.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from hardware_hunter.domain.alert import (
    BUTTON_LABELS,
    AlertSnapshot,
    render_phase1_listing_alert,
)
from hardware_hunter.domain.evaluation import ListingEvaluation
from hardware_hunter.domain.listing import Listing

# UX-DR5 — callback_data format.
_CALLBACK_DATA_RE = re.compile(r"^[a-z0-9_]+:[a-z0-9_]+:[A-Za-z0-9_\-]+$")
_CALLBACK_DATA_MAX_BYTES = 64

# Locked Phase 1 row vocabulary (UX-DR4).
_PHASE1_LABELS = (
    BUTTON_LABELS["view"],  # 👁 Ver
    BUTTON_LABELS["skip_phase1"],  # 🙅 Saltar
    BUTTON_LABELS["snooze"],  # 😴 Posponer 24h
)
_PHASE1_VERBS = ("view", "skip", "snooze")


def _snapshot(**overrides: object) -> AlertSnapshot:
    base: dict[str, object] = {
        "alert_id": UUID("12345678-1234-1234-1234-123456789abc"),
        "entry_key": ("Western Digital", "WD Red Plus 4TB", "WD40EFPX"),
        "entry_display_name": "WD Red Plus 4TB (WD40EFPX)",
        "listing": Listing(
            listing_id="abc123",
            marketplace="wallapop",
            url="https://wallapop.com/item/abc123",
            title="WD Red Plus 4TB",
            description="Como nuevo.",
            price_eur=Decimal("55.00"),
            location="Madrid",
            photo_urls=["https://cdn/photo.jpg"],
            fetched_at=datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC),
        ),
        "evaluation": ListingEvaluation(
            listing_id="abc123",
            entry_key=("Western Digital", "WD Red Plus 4TB", "WD40EFPX"),
            confidence="high",
            one_line_take="strong match.",
            is_container=False,
            evaluated_at=datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC),
        ),
        "phase": "phase1",
        "rendered_at": datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return AlertSnapshot(**base)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────
# Layout: always exactly 1 row x 3 buttons (UX-DR4).
# ─────────────────────────────────────────────────────────────────────────


def test_inline_keyboard_is_exactly_one_row_three_buttons() -> None:
    rendered = render_phase1_listing_alert(_snapshot())
    assert rendered.inline_keyboard is not None
    assert len(rendered.inline_keyboard) == 1
    assert len(rendered.inline_keyboard[0]) == 3


def test_inline_keyboard_labels_match_locked_phase1_vocabulary() -> None:
    rendered = render_phase1_listing_alert(_snapshot())
    assert rendered.inline_keyboard is not None
    labels = tuple(btn.text for btn in rendered.inline_keyboard[0])
    assert labels == _PHASE1_LABELS


def test_inline_keyboard_label_order_is_view_skip_snooze() -> None:
    """Ordering is part of the contract — `Ver` first (primary action),
    `Saltar` second (dismiss), `Posponer 24h` third (defer)."""
    rendered = render_phase1_listing_alert(_snapshot())
    assert rendered.inline_keyboard is not None
    assert rendered.inline_keyboard[0][0].text.endswith("Ver")
    assert rendered.inline_keyboard[0][1].text.endswith("Saltar")
    assert rendered.inline_keyboard[0][2].text.endswith("Posponer 24h")


# ─────────────────────────────────────────────────────────────────────────
# callback_data shape + byte cap (UX-DR5).
# ─────────────────────────────────────────────────────────────────────────


def test_callback_data_matches_surface_verb_id_format() -> None:
    rendered = render_phase1_listing_alert(_snapshot())
    assert rendered.inline_keyboard is not None
    for btn in rendered.inline_keyboard[0]:
        assert _CALLBACK_DATA_RE.fullmatch(btn.callback_data), (
            f"callback_data {btn.callback_data!r} does not match <surface>:<verb>:<id>"
        )


def test_callback_data_is_within_telegram_64_byte_cap() -> None:
    rendered = render_phase1_listing_alert(_snapshot())
    assert rendered.inline_keyboard is not None
    for btn in rendered.inline_keyboard[0]:
        size = len(btn.callback_data.encode("utf-8"))
        assert size <= _CALLBACK_DATA_MAX_BYTES, (
            f"callback_data {btn.callback_data!r} is {size} bytes"
        )


@pytest.mark.parametrize("verb", _PHASE1_VERBS)
def test_callback_data_carries_one_of_three_phase1_verbs(verb: str) -> None:
    rendered = render_phase1_listing_alert(_snapshot())
    assert rendered.inline_keyboard is not None
    verbs = {btn.callback_data.split(":")[1] for btn in rendered.inline_keyboard[0]}
    assert verb in verbs


def test_callback_data_surface_segment_is_listing() -> None:
    """``<surface>`` for listing alerts is always ``listing`` (UX-DR5)."""
    rendered = render_phase1_listing_alert(_snapshot())
    assert rendered.inline_keyboard is not None
    surfaces = {btn.callback_data.split(":")[0] for btn in rendered.inline_keyboard[0]}
    assert surfaces == {"listing"}


def test_callback_data_id_segment_equals_alert_id() -> None:
    """The id segment carries the AlertSnapshot's UUID — not the listing_id
    (eBay listing IDs contain ``|`` which the validator rejects)."""
    snap = _snapshot()
    rendered = render_phase1_listing_alert(snap)
    assert rendered.inline_keyboard is not None
    ids = {btn.callback_data.split(":")[2] for btn in rendered.inline_keyboard[0]}
    assert ids == {str(snap.alert_id)}
