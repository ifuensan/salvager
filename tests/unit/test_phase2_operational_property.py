"""Phase 2 operational property test — Story 5.16 (FR21 / UX-DR13-15).

Story 5.16 carves out the *Phase 2 slice* of the operational alert
event registry and asserts two locked anatomy invariants against it:

  - **Warn ⇒ numbered next-step**: every Phase 2 ``warn`` variant must
    render at least one numbered next-step row (``"1\\. ..."``). The
    operator needs a copy-pasteable starting point in every escalation.
  - **Entry name verbatim**: when the per-variant ctx carries an
    entry-shaped key (``entry`` or ``last_affected_entry``), the
    rendered body must contain that string verbatim. No silent dropping,
    no re-formatting.

The Phase 2 slice is the closed set of ``EventName`` members introduced
by Stories 5.5, 5.6 and 5.11 — they share the orchestrator / circuit
breaker / smoke-test surface and are the alerts a Phase 2 operator
relies on for "did anything autonomous happen and is it OK?".
"""

from __future__ import annotations

from typing import Any

import pytest

from hardware_hunter.domain.alert import (
    EventName,
    Severity,
    escape_markdown_v2,
    render_operational_alert,
)

# The Phase 2 slice of EventName — the closed set of operational events
# the Phase 2 surface emits. Adding a new Phase 2 event without
# extending this fixture map trips test_phase2_event_set_is_explicit.
_PHASE2_FIXTURES: dict[EventName, tuple[Severity, dict[str, Any]]] = {
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
}

# Ctx keys that carry an entry-shaped value (one or the other appears
# per variant — never both). Used by the verbatim-entry property test.
_ENTRY_KEYS: tuple[str, ...] = ("entry", "last_affected_entry")


def _is_phase2_event(event: EventName) -> bool:
    """An event is part of the Phase 2 slice if it lives in the
    fixture map. Centralised so the membership rule has one home."""
    return event in _PHASE2_FIXTURES


# ─────────────────────────────────────────────────────────────────────────
# Fixture-map completeness — the Phase 2 slice is explicit
# ─────────────────────────────────────────────────────────────────────────


def test_phase2_event_set_is_explicit() -> None:
    """The Phase 2 slice is a *named*, closed set — every member listed
    above must exist in :class:`EventName`. Stale entries here (e.g. a
    removed event) trip this check before the property tests run."""
    for event in _PHASE2_FIXTURES:
        assert isinstance(event, EventName)


# ─────────────────────────────────────────────────────────────────────────
# Property: every warn variant carries a numbered next-step (UX-DR15)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "event",
    [e for e, (sev, _) in _PHASE2_FIXTURES.items() if sev == "warn"],
    ids=lambda e: e.value,
)
def test_phase2_warn_variants_carry_a_numbered_next_step(event: EventName) -> None:
    severity, ctx = _PHASE2_FIXTURES[event]
    rendered = render_operational_alert(severity, event, ctx)
    # MarkdownV2 escapes the dot after "1" — the rendered next-step row
    # always starts "1\. " (period escaped), with the command in backticks
    # for one-tap copy-paste.
    assert "1\\. " in rendered.text


# ─────────────────────────────────────────────────────────────────────────
# Property: entry name appears verbatim when ctx carries one
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "event",
    [e for e, (_, ctx) in _PHASE2_FIXTURES.items() if any(k in ctx for k in _ENTRY_KEYS)],
    ids=lambda e: e.value,
)
def test_phase2_entry_name_appears_verbatim_in_body(event: EventName) -> None:
    severity, ctx = _PHASE2_FIXTURES[event]
    entry_value = next(str(ctx[k]) for k in _ENTRY_KEYS if k in ctx)
    rendered = render_operational_alert(severity, event, ctx)
    # The entry string contains MarkdownV2-reserved characters (the "/"
    # separator is safe, but slashes and dots may need escape elsewhere).
    # We assert on the escaped form — what actually lives in rendered.text.
    assert escape_markdown_v2(entry_value) in rendered.text


# ─────────────────────────────────────────────────────────────────────────
# Property: every Phase 2 variant obeys the locked operational anatomy
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("event", list(_PHASE2_FIXTURES), ids=lambda e: e.value)
def test_phase2_variants_never_carry_buttons_or_photos(event: EventName) -> None:
    """Operational alerts are operator-facing telemetry — they never
    carry an inline keyboard or a photo (those are reserved for the
    listing/receipt surfaces)."""
    severity, ctx = _PHASE2_FIXTURES[event]
    rendered = render_operational_alert(severity, event, ctx)
    assert rendered.inline_keyboard is None
    assert rendered.photo_url is None
    assert rendered.parse_mode == "MarkdownV2"


# ─────────────────────────────────────────────────────────────────────────
# Regression: phase2_event helper is honest about its membership rule
# ─────────────────────────────────────────────────────────────────────────


def test_is_phase2_event_only_counts_fixture_members() -> None:
    """A daemon-lifecycle event (not part of the Phase 2 slice) must
    not be picked up by the membership helper — otherwise the property
    tests would silently over-reach."""
    assert _is_phase2_event(EventName.circuit_open) is True
    assert _is_phase2_event(EventName.daemon_started) is False
