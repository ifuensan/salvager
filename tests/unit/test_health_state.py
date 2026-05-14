"""Tests for the in-memory health-state cache — Story 4.2."""

from __future__ import annotations

from datetime import UTC, datetime

from hardware_hunter.domain.alert import EventName
from hardware_hunter.orchestration.health_state import HealthState

_T0 = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 5, 14, 12, 5, 0, tzinfo=UTC)


def test_record_stores_last_event() -> None:
    state = HealthState()
    state.record("warn", EventName.poll_cycle_error, {"marketplace": "wallapop"}, _T0)

    record = state.last_event(EventName.poll_cycle_error)
    assert record is not None
    assert record.event is EventName.poll_cycle_error
    assert record.severity == "warn"
    assert record.ctx == {"marketplace": "wallapop"}
    assert record.at == _T0


def test_record_overwrites_same_event_with_latest() -> None:
    state = HealthState()
    state.record("warn", EventName.poll_cycle_error, {"n": 1}, _T0)
    state.record("warn", EventName.poll_cycle_error, {"n": 2}, _T1)

    record = state.last_event(EventName.poll_cycle_error)
    assert record is not None
    assert record.ctx == {"n": 2}
    assert record.at == _T1


def test_ctx_adapter_flags_adapter_degraded() -> None:
    state = HealthState()
    state.record(
        "info",
        EventName.wallapop_session_expired,
        {"adapter": "wallapop_api"},
        _T0,
    )
    assert state.is_degraded()
    assert "wallapop_api" in state.degraded_adapters()


def test_recovery_event_clears_degraded_flag() -> None:
    state = HealthState()
    state.record(
        "info",
        EventName.wallapop_session_expired,
        {"adapter": "wallapop_api"},
        _T0,
    )
    assert state.is_degraded()

    # A *_renewed event for the same adapter clears the flag.
    state.record(
        "info",
        EventName.wallapop_session_renewed,
        {"adapter": "wallapop_api"},
        _T1,
    )
    assert not state.is_degraded()
    assert "wallapop_api" not in state.degraded_adapters()
    # The recovery event itself is still recorded in the event history.
    assert state.last_event(EventName.wallapop_session_renewed) is not None


def test_recovered_suffix_also_clears_flag() -> None:
    state = HealthState()
    state.record("info", EventName.tinyfish_fallback_active, {"adapter": "wallapop"}, _T0)
    assert state.is_degraded()
    state.record("info", EventName.tinyfish_fallback_recovered, {"adapter": "wallapop"}, _T1)
    assert not state.is_degraded()


def test_event_without_adapter_does_not_touch_degraded_adapters() -> None:
    state = HealthState()
    state.record("info", EventName.daemon_started, {"version": "0.1.0"}, _T0)
    assert not state.is_degraded()
    assert state.degraded_adapters() == {}
    # ...but it IS in the event history.
    assert state.last_event(EventName.daemon_started) is not None


def test_snapshots_are_copies_not_live_views() -> None:
    state = HealthState()
    state.record("info", EventName.wallapop_session_expired, {"adapter": "wallapop_api"}, _T0)

    adapters = state.degraded_adapters()
    events = state.last_events()
    adapters.clear()
    events.clear()

    # Mutating the returned snapshots must not affect the cache.
    assert state.is_degraded()
    assert state.last_event(EventName.wallapop_session_expired) is not None
