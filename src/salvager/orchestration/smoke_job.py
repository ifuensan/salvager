"""Daemon-scheduled price-parser smoke-test job.

The Phase 2 buy path is gated by :class:`Phase2Preflight`, which needs a
*fresh passing* smoke result in ``phase2_state`` — otherwise every alert
downgrades to Phase 1 (no Comprar button). This module keeps that signal
green without operator intervention.

The :class:`Scheduler` port is interval-only (no cron / at-hour primitive),
so the daily smoke is an **hour-gated coarse-cadence task**: it runs the real
smoke only when the current UTC hour equals ``smoke_test_hour_utc`` and no
smoke has run yet this UTC day (decided from ``phase2_state.last_smoke_at``).
The daemon also runs the runner **once on startup** so the signal is fresh
immediately after a (re)deploy instead of waiting for the configured hour.

Both the runner and the gate swallow exceptions and log them — a smoke
hiccup must never crash the daemon's job loop. The smoke *outcome* (pass /
fail, lockout, recovery) is owned by :func:`run_smoke_test`, which persists
it and fires the operational alerts; this module only schedules it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from salvager.adapters.sqlite_store.audit_writer import Phase2AuditWriter
from salvager.interfaces.phase2_state_reader import Phase2StateReader
from salvager.observability.logging import get_logger
from salvager.orchestration.degradation_reporter import Reporter
from salvager.orchestration.smoke_test import (
    PriceParser,
    discover_fixtures,
    run_smoke_test,
)

#: How often the scheduled smoke task wakes to check the hour-gate. The gate
#: decides whether to actually run; 30 min honours ``smoke_test_hour_utc`` to
#: within half an hour and re-checks comfortably inside the 24h freshness
#: window (DEFAULT_SMOKE_FRESHNESS_HOURS).
DEFAULT_SMOKE_CADENCE_MINUTES: Final[int] = 30

SmokeRunner = Callable[[], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_smoke_runner(
    *,
    fixtures_dir: Path,
    parsers: Mapping[str, PriceParser],
    audit_writer: Phase2AuditWriter,
    state_reader: Phase2StateReader,
    reporter: Reporter,
    tolerance_eur: Decimal,
    tolerance_pct: Decimal,
    clock: Callable[[], datetime] = _utc_now,
) -> SmokeRunner:
    """Return a no-arg coroutine that runs ONE smoke-test, never raising.

    Fixture-discovery or run failures are logged and swallowed so a bad run
    can't kill the daemon (the startup invocation and the scheduled job both
    rely on this). The pass/fail signal lands in ``phase2_state`` via
    :func:`run_smoke_test`.
    """
    log = get_logger("orchestration.smoke_job")

    async def _run() -> None:
        try:
            fixtures = discover_fixtures(fixtures_dir)
        except Exception as exc:
            log.error(
                "smoke_test_fixtures_unavailable",
                extra={"error_class": exc.__class__.__name__, "fixtures_dir": str(fixtures_dir)},
            )
            return
        try:
            summary = await run_smoke_test(
                fixtures=fixtures,
                parsers=parsers,
                audit_writer=audit_writer,
                state_reader=state_reader,
                reporter=reporter,
                tolerance_eur=tolerance_eur,
                tolerance_pct=tolerance_pct,
                clock=clock,
            )
        except Exception as exc:
            log.exception("smoke_test_run_failed", extra={"error_class": exc.__class__.__name__})
            return
        log.info(
            "smoke_test_completed",
            extra={
                "any_failed": summary.any_failed,
                "fired_lockout": summary.fired_lockout,
                "fired_recovery": summary.fired_recovery,
            },
        )

    return _run


def build_scheduled_smoke_task(
    *,
    runner: SmokeRunner,
    state_reader: Phase2StateReader,
    hour_utc: int,
    clock: Callable[[], datetime] = _utc_now,
) -> SmokeRunner:
    """Wrap ``runner`` in the hour-gate: run once per UTC day at ``hour_utc``.

    Registered on the scheduler at a coarse cadence; this task no-ops on every
    tick except the first one of the configured hour each UTC day. "Already
    ran today" is decided from ``phase2_state.last_smoke_at`` so a daemon
    restart mid-day does not re-fire it.
    """
    log = get_logger("orchestration.smoke_job")

    async def _task() -> None:
        now = clock()
        if now.hour != hour_utc:
            return
        # Guard the gate the same way the runner guards itself: a transient
        # store failure reading phase2_state must not propagate into the
        # scheduler's job loop (matches this module's swallow-and-log contract).
        try:
            state = await state_reader.read()
        except Exception as exc:
            log.error(
                "scheduled_smoke_test_state_read_failed",
                extra={"error_class": exc.__class__.__name__},
            )
            return
        last = state.last_smoke_at
        if last is not None and last.astimezone(UTC).date() == now.date():
            return  # already ran this UTC day
        log.info("scheduled_smoke_test_firing", extra={"hour_utc": hour_utc})
        await runner()  # runner swallows its own errors

    return _task


__all__ = [
    "DEFAULT_SMOKE_CADENCE_MINUTES",
    "SmokeRunner",
    "build_scheduled_smoke_task",
    "build_smoke_runner",
]
