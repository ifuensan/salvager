"""Tests for the Phase 1 alert renderer — Story 3.11 (FR22 locked format)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from salvager.domain.alert import (
    BUTTON_LABELS,
    CALLBACK_DATA_FORMAT,
    SEVERITY_TOKENS,
    AlertSnapshot,
    escape_markdown_v2,
    render_phase1_listing_alert,
)
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.domain.pricing import buyer_cost

# A stable UUID + datetime so snapshot tests are deterministic.
FIXED_ALERT_ID = UUID("12345678-1234-1234-1234-123456789abc")
FIXED_RENDERED_AT = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
FIXED_FETCHED_AT = datetime(2026, 5, 12, 11, 59, 0, tzinfo=UTC)
FIXED_EVALUATED_AT = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


# ─────────────────────────────────────────────────────────────────────────
# Locked UX-DR constants
# ─────────────────────────────────────────────────────────────────────────


def test_severity_tokens_have_locked_nine_entries() -> None:
    # Grown by the wallapop-offer-flow PRD amendment (FR58-FR65): the three
    # offer surfaces join the original six.
    assert set(SEVERITY_TOKENS.keys()) == {
        "operational_warn",
        "operational_info",
        "phase1_listing",
        "phase2_listing",
        "phase2_buy_success",
        "phase2_buy_failure",
        "negotiable_listing",
        "offer_sent",
        "offer_failure",
    }
    assert SEVERITY_TOKENS["phase1_listing"] == "📦"
    assert SEVERITY_TOKENS["operational_warn"] == "⚠️ "
    assert SEVERITY_TOKENS["negotiable_listing"] == "💰"


def test_button_labels_have_locked_six_entries() -> None:
    assert set(BUTTON_LABELS.keys()) == {
        "view",
        "skip_phase1",
        "snooze",
        "buy",
        "skip_phase2",
        "offer",
    }
    assert BUTTON_LABELS["view"] == "👁 Ver"
    assert BUTTON_LABELS["skip_phase1"] == "🙅 Saltar"
    assert BUTTON_LABELS["snooze"] == "😴 Posponer 24h"
    assert BUTTON_LABELS["offer"] == "💰 Ofertar"


def test_callback_data_format_is_literal_template() -> None:
    assert CALLBACK_DATA_FORMAT == "<surface>:<verb>:<id>"


# ─────────────────────────────────────────────────────────────────────────
# escape_markdown_v2
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Each reserved character is backslash-escaped.
        ("WD_Red_Plus", r"WD\_Red\_Plus"),
        ("price: 55€!", r"price: 55€\!"),
        ("a*b", r"a\*b"),
        ("[link](url)", r"\[link\]\(url\)"),
        ("hello.world", r"hello\.world"),
        # Plain text passes through untouched.
        ("Como nuevo en caja", "Como nuevo en caja"),
        # Backslash itself gets escaped.
        ("path\\to\\file", r"path\\to\\file"),
    ],
)
def test_escape_markdown_v2_escapes_every_reserved(raw: str, expected: str) -> None:
    assert escape_markdown_v2(raw) == expected


def test_escape_markdown_v2_handles_full_reserved_set() -> None:
    """Smoke test against every reserved char individually."""
    for char in "_*[]()~`>#+-=|{}.!":
        rendered = escape_markdown_v2(f"x{char}y")
        assert rendered == f"x\\{char}y", f"escape failed for {char!r}"


# ─────────────────────────────────────────────────────────────────────────
# Fixture builders
# ─────────────────────────────────────────────────────────────────────────


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
        "fetched_at": FIXED_FETCHED_AT,
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
        "evaluated_at": FIXED_EVALUATED_AT,
    }
    base.update(overrides)
    return ListingEvaluation(**base)  # type: ignore[arg-type]


def _snapshot(**overrides: object) -> AlertSnapshot:
    base: dict[str, object] = {
        "alert_id": FIXED_ALERT_ID,
        "entry_key": ("Western Digital", "WD Red Plus 4TB", "WD40EFPX"),
        "entry_display_name": "WD Red Plus 4TB (WD40EFPX)",
        "listing": _listing(),
        "evaluation": _evaluation(),
        "phase": "phase1",
        "rendered_at": FIXED_RENDERED_AT,
    }
    base.update(overrides)
    return AlertSnapshot(**base)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────
# Direction A anatomy — direct listing
# ─────────────────────────────────────────────────────────────────────────


def test_direct_listing_alert_has_locked_row_anatomy() -> None:
    rendered = render_phase1_listing_alert(_snapshot())
    lines = rendered.text.split("\n")
    assert len(lines) == 5  # rows 1-5: + deep-link row (FR18)
    assert lines[0].startswith("📦 ")
    assert "*" in lines[0]  # bold name + price
    assert lines[1].startswith("📍 ")
    assert lines[2].startswith("🔗 ")  # clickable deep link to the listing
    assert lines[3].startswith("_") and lines[3].endswith("_")
    assert lines[4].startswith("🔍 Confidence: ")


def test_cost_line_shows_known_shipping_breakdown_on_wallapop() -> None:
    """The breakdown row shows item + shipping + Protección = total when a
    ``buyer_cost`` is supplied (shipping-aware-pricing). Substrings only —
    MarkdownV2 escapes '.', '(', ')', '+', '=' but not the comma decimals."""
    listing = _listing(marketplace="wallapop", price_eur=Decimal("55.00"))
    cost = buyer_cost(listing, assumed_shipping_eur=Decimal("3.50"))  # shipping unknown
    rendered = render_phase1_listing_alert(_snapshot(listing=listing), buyer_cost=cost)

    cost_line = next(line for line in rendered.text.split("\n") if line.startswith("💶"))
    assert "55,00" in cost_line  # item
    assert "envío" in cost_line
    assert "Protección" in cost_line  # Wallapop fee present
    assert "63,32" in cost_line  # delivered total (55 + 3,50 buffer + 4,82 fee)


def test_cost_line_flags_estimated_shipping_and_omits_fee_on_ebay() -> None:
    """An eBay listing with unknown shipping marks the buffer as estimated and
    carries no Protección term."""
    listing = _listing(marketplace="ebay", price_eur=Decimal("70.00"), shipping_eur=None)
    cost = buyer_cost(listing, assumed_shipping_eur=Decimal("3.50"))
    rendered = render_phase1_listing_alert(_snapshot(listing=listing), buyer_cost=cost)

    cost_line = next(line for line in rendered.text.split("\n") if line.startswith("💶"))
    assert "envío" in cost_line
    assert "est" in cost_line  # (est.) flag — buffer applied
    assert "Protección" not in cost_line  # eBay has no Protección fee
    assert "73,50" in cost_line  # 70 + 3,50 buffer
    assert "importación" not in cost_line  # country unknown → no import term


def test_cost_line_shows_estimated_import_charges_for_non_eu_listing() -> None:
    """A non-EU-located eBay listing renders the estimated import term and a
    total that includes it (ebay-import-charges-pricing)."""
    listing = _listing(
        marketplace="ebay", price_eur=Decimal("91.80"), shipping_eur=Decimal("0")
    ).model_copy(update={"country": "CN"})
    cost = buyer_cost(
        listing,
        assumed_shipping_eur=Decimal("3.50"),
        assumed_import_charges_eur=Decimal("3.63"),
    )
    rendered = render_phase1_listing_alert(_snapshot(listing=listing), buyer_cost=cost)

    cost_line = next(line for line in rendered.text.split("\n") if line.startswith("💶"))
    assert "importación" in cost_line
    assert "3,63" in cost_line
    assert "est" in cost_line  # the import value is always an estimate
    assert "95,43" in cost_line  # 91,80 + 0 envío + 3,63 importación


def test_cost_line_eu_listing_renders_without_import_term() -> None:
    """An EU-located listing's breakdown is identical to the pre-change
    render — no import component leaks in."""
    listing = _listing(
        marketplace="ebay", price_eur=Decimal("63.66"), shipping_eur=Decimal("16.82")
    ).model_copy(update={"country": "DE"})
    cost = buyer_cost(
        listing,
        assumed_shipping_eur=Decimal("3.50"),
        assumed_import_charges_eur=Decimal("3.63"),
    )
    rendered = render_phase1_listing_alert(_snapshot(listing=listing), buyer_cost=cost)

    cost_line = next(line for line in rendered.text.split("\n") if line.startswith("💶"))
    assert "importación" not in cost_line
    assert "80,48" in cost_line  # 63,66 + 16,82 — unchanged v0.3.3 total


def test_cost_line_absent_when_no_buyer_cost_supplied() -> None:
    """Backwards-compatible: no ``buyer_cost`` → no breakdown row."""
    rendered = render_phase1_listing_alert(_snapshot())
    assert not any(line.startswith("💶") for line in rendered.text.split("\n"))


def test_rendered_alert_parse_mode_is_markdown_v2() -> None:
    rendered = render_phase1_listing_alert(_snapshot())
    assert rendered.parse_mode == "MarkdownV2"


def test_rendered_alert_photo_url_is_first_listing_photo() -> None:
    rendered = render_phase1_listing_alert(_snapshot())
    assert rendered.photo_url == "https://cdn/photo.jpg"


def test_rendered_alert_missing_photo_yields_none() -> None:
    rendered = render_phase1_listing_alert(_snapshot(listing=_listing(photo_urls=[])))
    assert rendered.photo_url is None


def test_phase1_inline_keyboard_carries_three_buttons() -> None:
    rendered = render_phase1_listing_alert(_snapshot())
    assert rendered.inline_keyboard is not None
    row = rendered.inline_keyboard[0]
    assert len(row) == 3
    assert row[0].text == BUTTON_LABELS["view"]
    assert row[1].text == BUTTON_LABELS["skip_phase1"]
    assert row[2].text == BUTTON_LABELS["snooze"]
    # callback_data carries the alert_id (not the raw listing_id, which
    # may contain `|` for eBay).
    assert row[0].callback_data == f"listing:view:{FIXED_ALERT_ID}"
    assert row[1].callback_data == f"listing:skip:{FIXED_ALERT_ID}"
    assert row[2].callback_data == f"listing:snooze:{FIXED_ALERT_ID}"


# ─────────────────────────────────────────────────────────────────────────
# Direction E — container split
# ─────────────────────────────────────────────────────────────────────────


def test_container_alert_inserts_two_indented_rows() -> None:
    container_eval = _evaluation(
        is_container=True,
        wrapper_text="Synology DS220+ NAS",
        extracted_text="WD Red Plus 4TB drives",
    )
    rendered = render_phase1_listing_alert(_snapshot(evaluation=container_eval))
    lines = rendered.text.split("\n")
    assert len(lines) == 7  # 5 base (incl. deep link) + 2 inserted
    # Indented rows go between the deep-link row and the take row.
    assert lines[2].startswith("🔗 ")
    assert lines[3].startswith("  ↪︎ Wrapper: ")
    assert lines[4].startswith("  ↪︎ Extracted: ")


def test_container_alert_handles_missing_wrapper_text() -> None:
    container_eval = _evaluation(
        is_container=True,
        wrapper_text=None,
        extracted_text=None,
    )
    rendered = render_phase1_listing_alert(_snapshot(evaluation=container_eval))
    assert "Wrapper: —" in rendered.text
    assert "Extracted: —" in rendered.text


# ─────────────────────────────────────────────────────────────────────────
# MarkdownV2 escape — no injection possible
# ─────────────────────────────────────────────────────────────────────────


def test_listing_with_asterisks_does_not_break_markup() -> None:
    """A listing title with an asterisk must NOT escape into the markup."""
    rendered = render_phase1_listing_alert(
        _snapshot(
            entry_display_name="WD Red *Plus* 4TB",
            listing=_listing(price_eur=Decimal("55.50")),
        )
    )
    # The asterisks inside the name should be escaped.
    assert "WD Red \\*Plus\\* 4TB" in rendered.text


def test_location_with_dot_is_escaped() -> None:
    rendered = render_phase1_listing_alert(_snapshot(listing=_listing(location="St. Cugat")))
    assert "St\\. Cugat" in rendered.text


def test_llm_take_with_dot_is_escaped() -> None:
    rendered = render_phase1_listing_alert(
        _snapshot(evaluation=_evaluation(one_line_take="Looks good. Strong match."))
    )
    assert "Looks good\\. Strong match\\." in rendered.text


# Snapshot drift detection lives in test_alert_renderer_snapshots.py
# (Story 3.15) — it covers six fixtures including long-LLM-take and
# special-chars-in-title that this file's behavioral tests don't reach.

# ─────────────────────────────────────────────────────────────────────────
# Price formatting (es-ES style)
# ─────────────────────────────────────────────────────────────────────────


def test_price_formatted_in_es_style_with_decimal_comma() -> None:
    rendered = render_phase1_listing_alert(
        _snapshot(listing=_listing(price_eur=Decimal("1234.56")))
    )
    # Expect "1.234,56 €" with the dot escaped + comma + escaped €... wait
    # € is not in the reserved set; only the dot is escaped.
    assert "1\\.234,56 €" in rendered.text
