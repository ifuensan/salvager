"""Tests for the degradation reporter — Story 4.2 (NFR-R3).

Covers the three-surface fan-out, Telegram-failure resilience, the
dedup window, and the single-entry-point invariant (a lint test that
fails if ``render_operational_alert`` is called anywhere outside the
reporter).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from salvager.domain.alert import EventName, InlineButton, RenderedAlert
from salvager.domain.errors import TelegramDeliveryFailed
from salvager.interfaces.telegram_surface import CallbackHandler, TelegramSurface
from salvager.orchestration.degradation_reporter import DegradationReporter
from salvager.orchestration.health_state import HealthState

_T0 = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)


class _RecordingTelegram(TelegramSurface):
    """Records every send; optionally raises to simulate an outage."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sends: list[RenderedAlert] = []
        self._fail = fail

    async def send(self, rendered: RenderedAlert, *, reply_to_message_id: int | None = None) -> int:
        if self._fail:
            raise TelegramDeliveryFailed("send failed after 3 attempts")
        self.sends.append(rendered)
        return 1000 + len(self.sends)

    async def edit_alert(
        self,
        message_id: int,
        rendered: RenderedAlert,
        *,
        has_photo: bool,
    ) -> None:
        return None

    async def edit_keyboard(
        self,
        message_id: int,
        keyboard: list[list[InlineButton]] | None,
    ) -> None:  # pragma: no cover — reporter never edits
        return None

    async def listen_callbacks(self, handler: CallbackHandler) -> None:  # pragma: no cover
        _ = handler


def _records(out: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _clock_from(*moments: datetime) -> Callable[[], datetime]:
    """A clock callable that returns each moment in turn, then the last forever."""
    seq = list(moments)

    def _clock() -> datetime:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return _clock


# ─────────────────────────────────────────────────────────────────────────
# Three-surface fan-out
# ─────────────────────────────────────────────────────────────────────────


async def test_report_fans_out_to_log_health_and_telegram(
    capsys: pytest.CaptureFixture[str],
) -> None:
    telegram = _RecordingTelegram()
    health = HealthState()
    reporter = DegradationReporter(
        telegram_surface=telegram,
        health_state=health,
        dedup_window_seconds=300,
        clock=lambda: _T0,
    )

    await reporter.report(
        "info",
        EventName.wallapop_session_expired,
        {"adapter": "wallapop_api", "fallback_path_status": "active"},
    )

    # Sink 1: structured log carries the event + full ctx + severity.
    records = _records(capsys.readouterr().out)
    expired = next(r for r in records if r["event"] == "wallapop_session_expired")
    assert expired["severity"] == "info"
    assert expired["adapter"] == "wallapop_api"
    assert expired["fallback_path_status"] == "active"

    # Sink 2: Telegram got exactly one rendered operational alert.
    assert len(telegram.sends) == 1
    assert telegram.sends[0].text.startswith("ℹ️ ")  # noqa: RUF001 — info glyph

    # Sink 3: health state flagged the adapter degraded.
    assert "wallapop_api" in health.degraded_adapters()


async def test_warn_event_logs_at_warning_level(
    capsys: pytest.CaptureFixture[str],
) -> None:
    telegram = _RecordingTelegram()
    reporter = DegradationReporter(
        telegram_surface=telegram,
        health_state=HealthState(),
        clock=lambda: _T0,
    )
    await reporter.report(
        "warn",
        EventName.wallapop_both_paths_down,
        {"consecutive_failures": 3, "last_error_class": "TinyFishUnavailable"},
    )
    records = _records(capsys.readouterr().out)
    both_down = next(r for r in records if r["event"] == "wallapop_both_paths_down")
    assert both_down["level"] == "warn"


# ─────────────────────────────────────────────────────────────────────────
# Telegram-failure resilience
# ─────────────────────────────────────────────────────────────────────────


async def test_telegram_failure_does_not_block_log_or_health(
    capsys: pytest.CaptureFixture[str],
) -> None:
    telegram = _RecordingTelegram(fail=True)
    health = HealthState()
    reporter = DegradationReporter(
        telegram_surface=telegram,
        health_state=health,
        clock=lambda: _T0,
    )

    # Must not raise even though the Telegram surface is down.
    await reporter.report(
        "info",
        EventName.wallapop_session_expired,
        {"adapter": "wallapop_api"},
    )

    records = _records(capsys.readouterr().out)
    events = {r["event"] for r in records}
    # The primary event still logged...
    assert "wallapop_session_expired" in events
    # ...plus the secondary "Telegram is down" line so the operator sees
    # the outage even though they can't receive it via Telegram.
    assert "degradation_telegram_dispatch_failed" in events
    dispatch_failed = next(
        r for r in records if r["event"] == "degradation_telegram_dispatch_failed"
    )
    assert dispatch_failed["error_class"] == "TelegramDeliveryFailed"
    # Health state still updated despite the Telegram outage.
    assert "wallapop_api" in health.degraded_adapters()


async def test_failed_telegram_send_does_not_arm_the_dedup_window(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A send that failed must NOT suppress the next attempt — otherwise a
    transient Telegram blip would silently swallow the retry."""
    telegram = _RecordingTelegram(fail=True)
    reporter = DegradationReporter(
        telegram_surface=telegram,
        health_state=HealthState(),
        dedup_window_seconds=300,
        clock=lambda: _T0,
    )
    await reporter.report("info", EventName.wallapop_session_expired, {})
    # Recover the surface and report the same event again immediately.
    telegram._fail = False
    await reporter.report("info", EventName.wallapop_session_expired, {})
    # The second attempt went through — it was not deduped.
    assert len(telegram.sends) == 1


# ─────────────────────────────────────────────────────────────────────────
# Dedup window
# ─────────────────────────────────────────────────────────────────────────


async def test_duplicate_within_window_skips_telegram_but_still_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    telegram = _RecordingTelegram()
    health = HealthState()
    reporter = DegradationReporter(
        telegram_surface=telegram,
        health_state=health,
        dedup_window_seconds=300,
        clock=_clock_from(_T0, _T0 + timedelta(seconds=120)),
    )
    ctx = {"adapter": "wallapop_api"}

    await reporter.report("info", EventName.wallapop_session_expired, ctx)
    await reporter.report("info", EventName.wallapop_session_expired, ctx)

    # Only the first emission Telegram'd...
    assert len(telegram.sends) == 1
    # ...but BOTH occurrences logged (the second as a dedup notice + the event).
    records = _records(capsys.readouterr().out)
    event_lines = [r for r in records if r["event"] == "wallapop_session_expired"]
    assert len(event_lines) == 2
    assert any(r["event"] == "degradation_telegram_deduped" for r in records)


async def test_duplicate_after_window_telegrams_again() -> None:
    telegram = _RecordingTelegram()
    reporter = DegradationReporter(
        telegram_surface=telegram,
        health_state=HealthState(),
        dedup_window_seconds=300,
        clock=_clock_from(_T0, _T0 + timedelta(seconds=301)),
    )
    ctx = {"adapter": "wallapop_api"}
    await reporter.report("info", EventName.wallapop_session_expired, ctx)
    await reporter.report("info", EventName.wallapop_session_expired, ctx)
    # 301s elapsed > 300s window → the second alert is sent.
    assert len(telegram.sends) == 2


async def test_different_ctx_is_not_deduped() -> None:
    telegram = _RecordingTelegram()
    reporter = DegradationReporter(
        telegram_surface=telegram,
        health_state=HealthState(),
        dedup_window_seconds=300,
        clock=lambda: _T0,
    )
    await reporter.report("info", EventName.wallapop_api_degraded, {"error_class": "A"})
    await reporter.report("info", EventName.wallapop_api_degraded, {"error_class": "B"})
    # Same event, different ctx fingerprint → both Telegram'd.
    assert len(telegram.sends) == 2


async def test_zero_window_disables_dedup() -> None:
    telegram = _RecordingTelegram()
    reporter = DegradationReporter(
        telegram_surface=telegram,
        health_state=HealthState(),
        dedup_window_seconds=0,
        clock=lambda: _T0,
    )
    ctx = {"adapter": "wallapop_api"}
    await reporter.report("info", EventName.wallapop_session_expired, ctx)
    await reporter.report("info", EventName.wallapop_session_expired, ctx)
    assert len(telegram.sends) == 2


# ─────────────────────────────────────────────────────────────────────────
# Single-entry-point invariant (NFR-R3 structural enforcement)
# ─────────────────────────────────────────────────────────────────────────


def test_render_operational_alert_has_a_single_caller() -> None:
    """``render_operational_alert`` must be CALLED only from the degradation
    reporter — it is the one chokepoint for operational Telegram alerts.

    The function may be *imported* elsewhere for typing, but a call
    (``render_operational_alert(``) outside the reporter would mean a
    second codepath can dispatch operational alerts, breaking NFR-R3.
    """
    src_root = Path(__file__).resolve().parents[2] / "src" / "salvager"
    allowed = {
        src_root / "domain" / "alert.py",  # the definition
        src_root / "orchestration" / "degradation_reporter.py",  # the one caller
        # Story 5.17 — the release-audit ``dev emit-alert`` command
        # builds rendered operational variants for one-shot Telegram
        # capture. It is NOT the runtime degradation path (no dedup,
        # no health-state), so NFR-R3 is not engaged here; it is an
        # operator-driven, audited bypass for the v1.0 client-variance
        # audit only.
        src_root / "cli" / "dev_alert_fixtures.py",
    }
    call_re = re.compile(r"\brender_operational_alert\s*\(")

    offenders: list[str] = []
    for py_file in src_root.rglob("*.py"):
        if py_file in allowed:
            continue
        if call_re.search(py_file.read_text(encoding="utf-8")):
            offenders.append(str(py_file.relative_to(src_root)))

    assert not offenders, (
        f"render_operational_alert called outside DegradationReporter: {offenders}. "
        "All operational Telegram alerts must fan out through DegradationReporter.report()."
    )
