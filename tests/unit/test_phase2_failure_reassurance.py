"""Reassurance-line property test — Story 5.16 (FR28 / UX-DR10).

UX-DR10 mandates that every Phase 2 buy-failure variant carries the
verbatim ``La compra NO se ha ejecutado.`` reassurance so the operator
can answer "did the agent buy it?" from the alert alone. The
``screenshot_missing`` variant is the documented exception (the buy
MAY have completed; the alternate reassurance acknowledges the
ambiguity).

The reassurance text contains MarkdownV2-reserved characters (the
trailing period), so the *rendered* form is the escaped string
``La compra NO se ha ejecutado\\.``. This test asserts on the escaped
form — that is what actually lives in ``RenderedAlert.text``; Telegram
un-escapes it back to the user-visible canonical form.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hardware_hunter.domain.alert import (
    REASSURANCE_LINE,
    SCREENSHOT_MISSING_REASSURANCE,
    escape_markdown_v2,
    render_phase2_buy_failure,
)
from hardware_hunter.domain.errors import BuyFailureReason

_GENERIC_CTX: dict[str, object] = {
    "api_price": Decimal("53.00"),
    "html_price": Decimal("0.53"),
    "tolerance_eur": Decimal("1.00"),
    "consecutive_failures": 3,
    "threshold": 3,
    "missing": ["buy_button"],
    "error_class": "TinyFishUnavailable",
    "transaction_id": 42,
    "receipt_id": "WP-2026-0001",
}


@pytest.mark.parametrize(
    "reason",
    [r for r in BuyFailureReason if r is not BuyFailureReason.screenshot_missing],
    ids=lambda r: r.value,
)
def test_every_non_screenshot_variant_carries_reassurance(
    reason: BuyFailureReason,
) -> None:
    rendered = render_phase2_buy_failure(
        reason, entry_display_name="WD Red Plus 4TB", ctx=_GENERIC_CTX
    )
    assert escape_markdown_v2(REASSURANCE_LINE) in rendered.text
    # The screenshot-missing alternate must NOT appear on these variants.
    assert escape_markdown_v2(SCREENSHOT_MISSING_REASSURANCE) not in rendered.text


def test_screenshot_missing_uses_the_alternate_reassurance() -> None:
    rendered = render_phase2_buy_failure(
        BuyFailureReason.screenshot_missing,
        entry_display_name="WD Red Plus 4TB",
        ctx={"transaction_id": 42},
    )
    assert escape_markdown_v2(SCREENSHOT_MISSING_REASSURANCE) in rendered.text
    # And the standard line is suppressed — they are mutually exclusive.
    assert escape_markdown_v2(REASSURANCE_LINE) not in rendered.text


def test_reassurance_text_is_the_locked_constant() -> None:
    """A regression guard against silent re-wording of the canonical line."""
    assert REASSURANCE_LINE == "La compra NO se ha ejecutado."
    assert SCREENSHOT_MISSING_REASSURANCE == (
        "La compra puede haberse completado, pero no se capturó el recibo."
    )
