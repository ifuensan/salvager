"""Phase 2 buy success + failure renderer tests — Stories 5.8 + 5.9.

Three layers:

  - snapshot tests of the success receipt anatomy (happy / large-receipt /
    special-char receipt-id) and one per failure variant;
  - structural invariants (no inline keyboard, locked severity prefix,
    receipt only emoji being ``✅``);
  - the mandatory-reassurance property test: every ``BuyFailureReason``
    variant's rendered text contains the verbatim reassurance line —
    in its MarkdownV2-escaped form — with the documented exception for
    ``screenshot_missing`` (anticipates the Story 5.16 enumeration).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from syrupy.assertion import SnapshotAssertion

from hardware_hunter.domain.alert import (
    REASSURANCE_LINE,
    SCREENSHOT_MISSING_REASSURANCE,
    escape_markdown_v2,
    render_phase2_buy_failure,
    render_phase2_buy_success,
)
from hardware_hunter.domain.errors import BuyFailureReason
from hardware_hunter.domain.phase2_audit import TransactionRecord

_FIXED_ALERT_ID = UUID("12345678-1234-1234-1234-123456789abc")
_FIXED_TS = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)


def _transaction(**overrides: object) -> TransactionRecord:
    base: dict[str, object] = {
        "alert_id": _FIXED_ALERT_ID,
        "price_paid_eur": Decimal("55.00"),
        "payment_method": "wallapop_pay",
        "receipt_id": "WP-2026-0001",
        "screenshot_path": "/app/data/screenshots/WP-2026-0001.png",
        "total_seconds": 42,
        "committed_at": _FIXED_TS,
    }
    base.update(overrides)
    return TransactionRecord(**base)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────
# render_phase2_buy_success — snapshots + structure (Story 5.8)
# ─────────────────────────────────────────────────────────────────────────


def test_success_renders_locked_anatomy(snapshot: SnapshotAssertion) -> None:
    rendered = render_phase2_buy_success(
        _transaction(),
        entry_display_name="WD Red Plus 4TB (WD40EFPX)",
        audit_id=42,
    )
    assert rendered.text == snapshot
    assert rendered.text.startswith("✅ ")
    assert rendered.inline_keyboard is None
    assert rendered.photo_url == "/app/data/screenshots/WP-2026-0001.png"


def test_success_handles_large_receipt(snapshot: SnapshotAssertion) -> None:
    rendered = render_phase2_buy_success(
        _transaction(
            price_paid_eur=Decimal("1234.56"),
            receipt_id="WP-2026-0001-VERY-LONG-RECEIPT-ID-X9",
            payment_method="ebay_checkout",
            total_seconds=180,
        ),
        entry_display_name="Crucial 16GB DDR4 3200 (CT16G4DFD832A)",
        audit_id=99,
    )
    assert rendered.text == snapshot


def test_success_escapes_entry_name_specials(snapshot: SnapshotAssertion) -> None:
    rendered = render_phase2_buy_success(
        _transaction(),
        entry_display_name="WD_Red+Plus *4TB* (ref!)",
        audit_id=7,
    )
    assert rendered.text == snapshot


def test_success_requires_screenshot_path() -> None:
    """The orchestrator must divert to render_phase2_buy_failure
    (reason=screenshot_missing) when the capture failed — calling the
    success renderer with an empty path is a programmer error."""
    with pytest.raises(ValueError, match="screenshot_path"):
        render_phase2_buy_success(
            _transaction(screenshot_path=""),
            entry_display_name="WD Red Plus 4TB",
            audit_id=1,
        )


def test_success_body_carries_only_the_lead_emoji() -> None:
    """UX-discipline: the receipt body uses the ✅ lead and no other
    celebratory emoji. The only other emoji in the body would be from
    a user-supplied entry name — escape can't strip those, so the
    contract is on the static template."""
    rendered = render_phase2_buy_success(
        _transaction(),
        entry_display_name="WD Red Plus 4TB",
        audit_id=1,
    )
    static_template = rendered.text.replace("WD Red Plus 4TB", "")
    # ✅ allowed (the lead). 🎉 / 🥳 / etc are not.
    forbidden = "🎉🥳🎊🚀✨"
    assert not any(char in static_template for char in forbidden)


# ─────────────────────────────────────────────────────────────────────────
# render_phase2_buy_failure — locked structure + reassurance (Story 5.9)
# ─────────────────────────────────────────────────────────────────────────


def test_failure_reconciliation_tripped_snapshot(snapshot: SnapshotAssertion) -> None:
    rendered = render_phase2_buy_failure(
        BuyFailureReason.reconciliation_tripped,
        entry_display_name="WD Red Plus 4TB (WD40EFPX)",
        ctx={
            "api_price": Decimal("53.00"),
            "html_price": Decimal("0.53"),
            "tolerance_eur": Decimal("1.00"),
        },
    )
    assert rendered.text == snapshot
    assert rendered.inline_keyboard is None
    assert rendered.photo_url is None
    # The bullet rows the AC names verbatim:
    assert "Wallapop API: 53,00 €" in rendered.text or "Wallapop API: 53,00" in rendered.text
    assert "Tolerancia:" in rendered.text


def test_failure_circuit_open_snapshot(snapshot: SnapshotAssertion) -> None:
    rendered = render_phase2_buy_failure(
        BuyFailureReason.circuit_open,
        entry_display_name="WD Red Plus 4TB (WD40EFPX)",
        ctx={"consecutive_failures": 3, "threshold": 3},
    )
    assert rendered.text == snapshot
    assert "3 fallos consecutivos" in rendered.text
    assert "phase2 enable" in rendered.text


def test_failure_screenshot_missing_uses_alternate_reassurance(
    snapshot: SnapshotAssertion,
) -> None:
    rendered = render_phase2_buy_failure(
        BuyFailureReason.screenshot_missing,
        entry_display_name="WD Red Plus 4TB",
        ctx={"transaction_id": 42, "receipt_id": "WP-2026-0001"},
    )
    assert rendered.text == snapshot
    # The alternate reassurance line is the one in the message, the
    # default one MUST NOT also be present.
    assert escape_markdown_v2(SCREENSHOT_MISSING_REASSURANCE) in rendered.text
    assert escape_markdown_v2(REASSURANCE_LINE) not in rendered.text
    # Receipt-aware next step: the reconcile command is added when
    # a receipt_id is in ctx.
    assert "phase2 reconcile WP-2026-0001" in rendered.text


def test_failure_screenshot_missing_without_receipt_id_skips_reconcile_step() -> None:
    rendered = render_phase2_buy_failure(
        BuyFailureReason.screenshot_missing,
        entry_display_name="WD Red Plus 4TB",
        ctx={"transaction_id": 42},
    )
    assert "phase2 reconcile" not in rendered.text


# ─────────────────────────────────────────────────────────────────────────
# Property: every variant carries the reassurance line (5.9 / 5.16)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reason",
    [r for r in BuyFailureReason if r is not BuyFailureReason.screenshot_missing],
    ids=lambda r: r.value,
)
def test_every_non_screenshot_variant_carries_reassurance(reason: BuyFailureReason) -> None:
    rendered = render_phase2_buy_failure(
        reason,
        entry_display_name="WD Red Plus 4TB",
        ctx={
            # Generic ctx — variant-specific fields are optional.
            "api_price": Decimal("10.00"),
            "html_price": Decimal("10.00"),
            "tolerance_eur": Decimal("1.00"),
            "consecutive_failures": 3,
            "threshold": 3,
            "missing": ["buy_button"],
            "error_class": "TinyFishUnavailable",
        },
    )
    # The reassurance text contains MarkdownV2-reserved characters (the
    # trailing period); we assert on the escaped form which is what
    # actually lives in rendered.text.
    assert escape_markdown_v2(REASSURANCE_LINE) in rendered.text


def test_screenshot_missing_uses_the_alternate_reassurance() -> None:
    rendered = render_phase2_buy_failure(
        BuyFailureReason.screenshot_missing,
        entry_display_name="WD Red Plus 4TB",
        ctx={"transaction_id": 42},
    )
    assert escape_markdown_v2(SCREENSHOT_MISSING_REASSURANCE) in rendered.text
    # The standard reassurance line must NOT also appear — UX-DR10 is
    # mutually exclusive.
    assert escape_markdown_v2(REASSURANCE_LINE) not in rendered.text


@pytest.mark.parametrize("reason", list(BuyFailureReason), ids=lambda r: r.value)
def test_every_variant_has_a_snapshot(
    reason: BuyFailureReason,
    snapshot: SnapshotAssertion,
) -> None:
    """Story 5.16 AC: one snapshot fixture per ``BuyFailureReason``."""
    rendered = render_phase2_buy_failure(
        reason,
        entry_display_name="WD Red Plus 4TB (WD40EFPX)",
        ctx={
            # Generic, variant-spanning ctx; unused fields are silently
            # ignored by the per-variant detail builders.
            "api_price": Decimal("53.00"),
            "html_price": Decimal("0.53"),
            "tolerance_eur": Decimal("1.00"),
            "consecutive_failures": 3,
            "threshold": 3,
            "missing": ["buy_button"],
            "error_class": "TinyFishUnavailable",
            "transaction_id": 42,
            "receipt_id": "WP-2026-0001",
        },
    )
    assert rendered.text == snapshot


@pytest.mark.parametrize("reason", list(BuyFailureReason), ids=lambda r: r.value)
def test_every_variant_starts_with_locked_severity(reason: BuyFailureReason) -> None:
    rendered = render_phase2_buy_failure(
        reason,
        entry_display_name="WD Red Plus 4TB",
        ctx={"transaction_id": 1},
    )
    assert rendered.text.startswith("🚫 ")
    assert rendered.inline_keyboard is None
    # The headline pattern "🚫 *Compra abortada* · <entry>" must hold.
    assert re.match(r"^🚫 \*Compra abortada\* · ", rendered.text)
