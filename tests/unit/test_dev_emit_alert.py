"""Tests for ``salvager dev emit-alert`` — Story 5.17.

Three layers:

  - **catalog completeness** — the registry contains exactly 66
    variants (3+3 listing, 3 cost, 3 edited, 1 price-drop ping,
    2 negotiable, 2 with-offer, 1 receipt, 1 offer-sent, 9 buy
    failures, 12 offer failures, 26 operational) and the set drifts
    only when a PRD amendment adds an EventName / BuyFailureReason /
    OfferFailureReason variant or a release-audit delta adds a
    rendering shape;
  - **renderability** — every registered builder produces a non-empty
    MarkdownV2 ``RenderedAlert.text`` without raising;
  - **CLI seams** — ``--dry-run`` prints the rendered text to stdout
    without touching the Telegram surface, and an unknown variant
    exits with code 2.
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from salvager.cli.app import app
from salvager.cli.dev_alert_fixtures import (
    VARIANT_REGISTRY,
    build_rendered_variant,
)
from salvager.domain.alert import EventName
from salvager.domain.errors import BuyFailureReason, OfferFailureReason

_RUNNER = CliRunner()


# ─────────────────────────────────────────────────────────────────────────
# Registry catalog completeness
# ─────────────────────────────────────────────────────────────────────────


def test_registry_is_a_closed_set_of_66_variants() -> None:
    expected_listing = {
        "phase1_listing_direct",
        "phase1_listing_container",
        "phase1_listing_missing_photo",
        "phase2_listing_direct",
        "phase2_listing_container",
        "phase2_listing_missing_photo",
        "phase1_listing_with_cost",
        "phase1_listing_with_import",
        "phase2_listing_with_cost",
        "phase1_listing_edited_reserved",
        "phase1_listing_edited_price_drop",
        "phase2_listing_edited_reserved",
        "price_drop_ping",
        "negotiable_listing_direct",
        "negotiable_listing_missing_photo",
        "phase1_listing_with_offer",
        "phase2_listing_with_offer",
    }
    expected_buy = {"buy_success"} | {f"buy_failure_{r.value}" for r in BuyFailureReason}
    expected_offer = {"offer_sent"} | {f"offer_failure_{r.value}" for r in OfferFailureReason}
    expected_operational = {e.value for e in EventName}
    expected = expected_listing | expected_buy | expected_offer | expected_operational
    assert set(VARIANT_REGISTRY) == expected
    # 17 listing shapes + 10 buy + 13 offer + 26 operational (= len(EventName)).
    assert len(VARIANT_REGISTRY) == 66


def test_registry_covers_every_buy_failure_reason() -> None:
    for reason in BuyFailureReason:
        assert f"buy_failure_{reason.value}" in VARIANT_REGISTRY


def test_registry_covers_every_offer_failure_reason() -> None:
    for reason in OfferFailureReason:
        assert f"offer_failure_{reason.value}" in VARIANT_REGISTRY


def test_registry_covers_every_event_name() -> None:
    for event in EventName:
        assert event.value in VARIANT_REGISTRY


# ─────────────────────────────────────────────────────────────────────────
# Renderability — every variant builds without raising
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("variant", sorted(VARIANT_REGISTRY))
def test_every_variant_renders_non_empty_markdownv2(variant: str) -> None:
    rendered = build_rendered_variant(variant)
    assert rendered.text.strip()
    assert rendered.parse_mode == "MarkdownV2"


def test_listing_variants_carry_inline_keyboard() -> None:
    for name in ("phase1_listing_direct", "phase2_listing_direct"):
        rendered = build_rendered_variant(name)
        assert rendered.inline_keyboard is not None


def test_operational_variants_never_carry_inline_keyboard() -> None:
    for event in EventName:
        rendered = build_rendered_variant(event.value)
        assert rendered.inline_keyboard is None


# ─────────────────────────────────────────────────────────────────────────
# CLI — list-variants + emit-alert --dry-run
# ─────────────────────────────────────────────────────────────────────────


def test_list_variants_prints_every_variant_name() -> None:
    result = _RUNNER.invoke(app, ["dev", "list-variants"])
    assert result.exit_code == 0
    for name in VARIANT_REGISTRY:
        assert name in result.output
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "66 variants total" in plain


def test_emit_alert_dry_run_prints_rendered_text_without_sending() -> None:
    result = _RUNNER.invoke(app, ["dev", "emit-alert", "daemon_started", "--dry-run"])
    assert result.exit_code == 0
    assert "Daemon iniciado" in result.output
    assert "# variant: daemon_started" in result.output
    assert "# parse_mode: MarkdownV2" in result.output


def test_emit_alert_dry_run_surfaces_photo_url_and_keyboard_for_listings() -> None:
    result = _RUNNER.invoke(app, ["dev", "emit-alert", "phase2_listing_direct", "--dry-run"])
    assert result.exit_code == 0
    assert "# photo_url:" in result.output
    assert "# keyboard:" in result.output
    assert "✅ Comprar" in result.output


def test_emit_alert_unknown_variant_exits_with_code_2() -> None:
    result = _RUNNER.invoke(app, ["dev", "emit-alert", "not_a_real_variant", "--dry-run"])
    assert result.exit_code == 2
    assert "Unknown variant" in result.output


def test_dev_subcommand_appears_in_root_help() -> None:
    result = _RUNNER.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "dev" in result.output
