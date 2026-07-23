"""Golden snapshots for every offer-flow rendering surface.

Built FROM the variant registry (the PR #50 pattern): the fixture each
test renders is exactly what ``salvager dev emit-alert <variant>``
dispatches, so the golden text here ≡ the on-device capture reference.
Covers the 4 offer-eligible listing shapes, ``offer_sent``, and the 12
``offer_failure_*`` variants — 17 snapshots, locked formats (FR22 +
FR50-FR57).
"""

from __future__ import annotations

import pytest
from syrupy.assertion import SnapshotAssertion

from salvager.cli.dev_alert_fixtures import VARIANT_REGISTRY, build_rendered_variant

_OFFER_VARIANTS = sorted(
    name
    for name in VARIANT_REGISTRY
    if name.startswith(("negotiable_listing", "offer_failure_"))
    or name in {"offer_sent", "phase1_listing_with_offer", "phase2_listing_with_offer"}
)


def test_offer_variant_selection_is_complete() -> None:
    # 2 negotiable shapes + 2 with-offer shapes + offer_sent + 12 failures.
    assert len(_OFFER_VARIANTS) == 17


@pytest.mark.parametrize("variant", _OFFER_VARIANTS)
def test_offer_variant_matches_snapshot(variant: str, snapshot: SnapshotAssertion) -> None:
    rendered = build_rendered_variant(variant)
    assert rendered.text == snapshot(name=f"{variant}.text")


@pytest.mark.parametrize("variant", _OFFER_VARIANTS)
def test_offer_variant_keyboard_matches_snapshot(variant: str, snapshot: SnapshotAssertion) -> None:
    rendered = build_rendered_variant(variant)
    keyboard = (
        [[button.model_dump() for button in row] for row in rendered.inline_keyboard]
        if rendered.inline_keyboard is not None
        else None
    )
    assert keyboard == snapshot(name=f"{variant}.keyboard")
