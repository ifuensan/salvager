"""Operational alert renderer tests — Story 4.1 (FR21 / UX-DR13-15).

Three layers of coverage:

  - **Snapshot** — one fixture per :class:`EventName` variant, each
    checked against its tracked syrupy snapshot. The fixture map is
    asserted complete, so a new enum variant without a fixture fails
    the build.
  - **Property** — every variant obeys the locked severity anatomy:
    the text starts with the exact severity prefix; ``warn`` always
    carries ≥ 1 numbered CLI next-step; ``info`` carries 0 or 1 CLI
    hint, never more.
  - **Anatomy** — the ``wallapop_both_paths_down`` warn alert is
    pinned to its AC-specified 3-step recovery list.
"""

from __future__ import annotations

from typing import Any

import pytest
from syrupy.assertion import SnapshotAssertion

from salvager.domain.alert import (
    EventName,
    RenderedAlert,
    Severity,
    render_operational_alert,
)

# One representative ctx per variant, paired with its canonical severity.
# Adding an EventName member without extending this map trips
# test_every_event_has_a_fixture.
_FIXTURES: dict[EventName, tuple[Severity, dict[str, Any]]] = {
    EventName.daemon_started: ("info", {"version": "0.1.0", "jobs": "wallapop_poll, ebay_poll"}),
    EventName.daemon_stopped: ("info", {"reason": "SIGTERM"}),
    EventName.wallapop_session_expired: ("info", {}),
    EventName.wallapop_session_renewed: ("info", {}),
    EventName.wallapop_api_degraded: ("info", {"error_class": "WallapopApiError"}),
    EventName.wallapop_both_paths_down: (
        "warn",
        {"consecutive_failures": 3, "last_error_class": "TinyFishUnavailable"},
    ),
    EventName.tinyfish_fallback_active: ("info", {}),
    EventName.tinyfish_fallback_recovered: ("info", {}),
    EventName.ebay_token_refresh_failed: ("warn", {}),
    EventName.ebay_quota_breach: ("info", {"used": 5000, "budget": 5000}),
    EventName.llm_provider_rate_limited: ("info", {"provider": "gemini-flash"}),
    EventName.entry_snoozed: (
        "info",
        {"entry_display_name": "WD Red Plus 4TB", "snooze_until": "2026-05-15T12:00:00Z"},
    ),
    EventName.poll_cycle_error: (
        "warn",
        {"error_class": "RuntimeError", "marketplace": "wallapop"},
    ),
    EventName.circuit_open: (
        "warn",
        {
            "consecutive_failures": 3,
            "threshold": 3,
            "last_affected_entry": "WD Red Plus 4TB / WD40EFPX",
        },
    ),
    EventName.smoke_test_failed: (
        "warn",
        {
            "fixture_name": "wallapop_html_comma_vs_dot",
            "parsed_price": "0.53",
            "expected_price": "53.00",
            "delta_eur": "52.47",
            "parser_error_class": "—",
        },
    ),
    EventName.smoke_test_recovered: ("info", {}),
    EventName.phase2_disabled: (
        "warn",
        {
            "reason": "receipt_mismatch",
            "last_affected_entry": "WD Red Plus 4TB / WD40EFPX",
        },
    ),
    EventName.phase2_re_enabled: (
        "info",
        {"entry": "WD Red Plus 4TB / WD40EFPX"},
    ),
    EventName.phase2_buy_callback_received: (
        "info",
        {
            "entry": "WD Red Plus 4TB / WD40EFPX",
            "alert_id": "12345678-1234-1234-1234-123456789abc",
        },
    ),
    EventName.phase2_screenshot_missing: (
        "warn",
        {"receipt_id": "WP-2026-0001", "listing_id": "abc123"},
    ),
    EventName.phase2_buy_completion_slow: (
        "info",
        {
            "entry": "WD Red Plus 4TB / WD40EFPX",
            "elapsed_seconds": 87,
            "budget_seconds": 60,
        },
    ),
    EventName.buy_orchestrator_error: (
        "warn",
        {
            "error_class": "TinyFishSessionLost",
            "alert_id": "12345678-1234-1234-1234-123456789abc",
        },
    ),
    EventName.offer_lockout_engaged: (
        "warn",
        {
            "consecutive_failures": 3,
            "threshold": 3,
            "last_affected_entry": "Corsair Vengeance LPX 16GB / CMK16GX4M2D3000C16",
        },
    ),
    EventName.offer_disabled: (
        "warn",
        {"reason": "kill_switch_global"},
    ),
    EventName.offer_re_enabled: (
        "info",
        {"entry": "Corsair Vengeance LPX 16GB / CMK16GX4M2D3000C16"},
    ),
    EventName.offer_orchestrator_error: (
        "warn",
        {
            "error_class": "TinyFishSessionLost",
            "alert_id": "12345678-1234-1234-1234-123456789abc",
        },
    ),
}


def _render(event: EventName) -> RenderedAlert:
    severity, ctx = _FIXTURES[event]
    return render_operational_alert(severity, event, ctx)


# ─────────────────────────────────────────────────────────────────────────
# Fixture-map completeness
# ─────────────────────────────────────────────────────────────────────────


def test_every_event_has_a_fixture() -> None:
    """A new EventName variant must come with a fixture — otherwise the
    snapshot + property coverage silently skips it."""
    missing = set(EventName) - set(_FIXTURES)
    assert not missing, f"EventName variants without a test fixture: {missing}"


# ─────────────────────────────────────────────────────────────────────────
# Snapshot — locked anatomy per variant
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("event", list(EventName), ids=lambda e: e.value)
def test_operational_alert_matches_snapshot(
    event: EventName,
    snapshot: SnapshotAssertion,
) -> None:
    assert _render(event).text == snapshot


# ─────────────────────────────────────────────────────────────────────────
# Property — severity anatomy holds for every variant
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("event", list(EventName), ids=lambda e: e.value)
def test_severity_prefix_is_exact(event: EventName) -> None:
    severity, _ = _FIXTURES[event]
    text = _render(event).text
    if severity == "warn":
        assert text.startswith("⚠️ ")
    else:
        assert text.startswith("ℹ️ ")  # noqa: RUF001 — operator-facing info glyph


@pytest.mark.parametrize("event", list(EventName), ids=lambda e: e.value)
def test_warn_variants_carry_a_numbered_next_step(event: EventName) -> None:
    severity, _ = _FIXTURES[event]
    if severity != "warn":
        pytest.skip("info variants are covered by the CLI-hint property test")
    text = _render(event).text
    # The numbered list renders MarkdownV2-escaped ("1\. ") followed by a
    # backtick-wrapped command — copy-paste-ready per UX-DR15.
    assert "1\\. `" in text


@pytest.mark.parametrize("event", list(EventName), ids=lambda e: e.value)
def test_info_variants_carry_at_most_one_cli_hint(event: EventName) -> None:
    severity, _ = _FIXTURES[event]
    if severity != "info":
        pytest.skip("warn variants are covered by the numbered-next-step property test")
    text = _render(event).text
    # A CLI hint is a backtick code span; info alerts carry zero or one.
    code_spans = text.count("`") // 2
    assert code_spans <= 1


@pytest.mark.parametrize("event", list(EventName), ids=lambda e: e.value)
def test_operational_alerts_never_carry_buttons_or_photos(event: EventName) -> None:
    rendered = _render(event)
    assert rendered.inline_keyboard is None
    assert rendered.photo_url is None
    assert rendered.parse_mode == "MarkdownV2"


# ─────────────────────────────────────────────────────────────────────────
# Anatomy — the wallapop_both_paths_down warn alert (AC-pinned)
# ─────────────────────────────────────────────────────────────────────────


def test_wallapop_both_paths_down_carries_ctx_values_and_three_next_steps() -> None:
    text = render_operational_alert(
        "warn",
        EventName.wallapop_both_paths_down,
        {"consecutive_failures": 7, "last_error_class": "TinyFishUnavailable"},
    ).text
    # Specific ctx values surface in the body.
    assert "7 fallos consecutivos" in text
    assert "TinyFishUnavailable" in text
    # The three AC-named next-steps, in order.
    assert "1\\. `salvager audit show --last 5`" in text
    assert "2\\. revisa la conexión o parchea el adaptador si persiste" in text
    assert "3\\. `docker-compose restart salvager`" in text


def test_warn_headline_is_bold_info_headline_is_plain() -> None:
    warn = render_operational_alert("warn", EventName.poll_cycle_error, {}).text
    info = render_operational_alert("info", EventName.daemon_started, {}).text
    # warn: bold headline wrapped in asterisks.
    assert warn.splitlines()[0] == "⚠️ *Error en el ciclo de sondeo*"
    # info: plain headline, no asterisks.
    assert info.splitlines()[0] == "ℹ️ Daemon iniciado"  # noqa: RUF001


# ─────────────────────────────────────────────────────────────────────────
# Severity guard — a mismatched severity is a caller bug
# ─────────────────────────────────────────────────────────────────────────


def test_mismatched_severity_raises_value_error() -> None:
    # daemon_started is canonically info — asking for warn is a bug.
    with pytest.raises(ValueError, match="is a 'info' alert"):
        render_operational_alert("warn", EventName.daemon_started, {})
    # wallapop_both_paths_down is canonically warn — asking for info is a bug.
    with pytest.raises(ValueError, match="is a 'warn' alert"):
        render_operational_alert("info", EventName.wallapop_both_paths_down, {})


def test_missing_ctx_values_fall_back_to_em_dash() -> None:
    """A renderer must never crash on a sparse ctx — missing keys render
    as an em-dash placeholder, not a KeyError."""
    text = render_operational_alert("info", EventName.daemon_started, {}).text
    assert "Versión: — · jobs: —" in text
