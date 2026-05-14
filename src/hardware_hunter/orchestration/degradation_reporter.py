"""Degradation reporter — Story 4.2 (NFR-R3 "no silent failure").

Every degraded condition anywhere in the daemon flows through ONE
method — :meth:`DegradationReporter.report` — which fans out to three
independent surfaces:

  1. the structured logger (NFR-O1 fields, always),
  2. the Telegram surface (an operational alert, unless deduped),
  3. the in-memory :class:`HealthState` cache (always).

Why three surfaces
------------------
NFR-R3 says no failure may be silent. Three independent sinks means a
single broken sink can't hide a degradation: if Telegram is down, the
log + health state still record it (and a *secondary* log line records
the Telegram outage itself).

Single entry point
------------------
This is the ONLY codepath that renders + sends operational Telegram
alerts. ``test_degradation_reporter.py`` carries a lint test that
fails the build if ``render_operational_alert`` is called anywhere
else in ``src/``.

Dedup
-----
Repeated ``(event, ctx-fingerprint)`` pairs inside
``dedup_window_seconds`` emit only one Telegram alert — the log + the
health state still update on every occurrence. This stops alert storms
when a cascading failure fires the same event dozens of times a
minute. A window of ``0`` disables dedup entirely.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from hardware_hunter.domain.alert import EventName, Severity, render_operational_alert
from hardware_hunter.domain.errors import TelegramError
from hardware_hunter.interfaces.telegram_surface import TelegramSurface
from hardware_hunter.observability.logging import get_logger
from hardware_hunter.orchestration.health_state import HealthState

#: Default dedup window — mirrors
#: ``config.observability.degradation_dedup_window_seconds``.
DEFAULT_DEDUP_WINDOW_SECONDS: int = 300


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _ctx_fingerprint(ctx: Mapping[str, Any]) -> str:
    """A stable string key for a ctx dict — used for dedup matching.

    ``sort_keys`` makes the fingerprint order-independent; ``default=str``
    keeps it total over datetimes, UUIDs, Decimals, etc.
    """
    return json.dumps(ctx, sort_keys=True, default=str)


class DegradationReporter:
    """Fan-out reporter for every degraded condition (NFR-R3).

    Constructed once by the daemon entry point with the live logger,
    Telegram surface, and health-state cache injected. Subsystems call
    :meth:`report`; they never touch the three sinks directly.
    """

    def __init__(
        self,
        *,
        telegram_surface: TelegramSurface,
        health_state: HealthState,
        logger: logging.Logger | None = None,
        dedup_window_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._telegram = telegram_surface
        self._health = health_state
        self._log = logger if logger is not None else get_logger("orchestration.degradation")
        self._dedup_window_seconds = dedup_window_seconds
        self._clock = clock
        #: (event, ctx-fingerprint) → last time we Telegram'd it.
        self._last_telegram_at: dict[tuple[EventName, str], datetime] = {}

    async def report(
        self,
        severity: Severity,
        event: EventName,
        ctx: Mapping[str, Any],
    ) -> None:
        """Fan one degradation out to log + health state + Telegram.

        Never raises: a broken Telegram surface is logged and swallowed
        so the log + health-state sinks still land. The method is the
        single chokepoint for operational Telegram alerts.
        """
        now = self._clock()

        # ── Sink 1: structured log (always) ──────────────────────────
        log_fn = self._log.warning if severity == "warn" else self._log.info
        log_fn(event.value, extra={"severity": severity, **dict(ctx)})

        # ── Sink 2: health-state cache (always) ──────────────────────
        self._health.record(severity, event, ctx, now)

        # ── Sink 3: Telegram (unless deduped) ────────────────────────
        if self._is_deduped(event, ctx, now):
            self._log.info(
                "degradation_telegram_deduped",
                extra={
                    # NOT "event" — that key is the log message itself;
                    # reusing it would clobber the JSON record's event field.
                    "degraded_event": event.value,
                    "dedup_window_seconds": self._dedup_window_seconds,
                },
            )
            return

        rendered = render_operational_alert(severity, event, ctx)
        try:
            await self._telegram.send(rendered)
        except TelegramError as exc:
            # The operator can't get the alert via Telegram right now —
            # but the log + health state already landed above, and this
            # secondary line records the Telegram outage itself.
            self._log.error(
                "degradation_telegram_dispatch_failed",
                extra={
                    "degraded_event": event.value,
                    "error_class": exc.__class__.__name__,
                },
            )
            return

        # Only a delivered alert arms the dedup window — a failed send
        # must not suppress the next attempt.
        self._last_telegram_at[(event, _ctx_fingerprint(ctx))] = now

    def _is_deduped(
        self,
        event: EventName,
        ctx: Mapping[str, Any],
        now: datetime,
    ) -> bool:
        """True iff this ``(event, ctx)`` was Telegram'd within the window."""
        if self._dedup_window_seconds <= 0:
            return False
        last = self._last_telegram_at.get((event, _ctx_fingerprint(ctx)))
        if last is None:
            return False
        return (now - last).total_seconds() < self._dedup_window_seconds


__all__ = [
    "DEFAULT_DEDUP_WINDOW_SECONDS",
    "DegradationReporter",
]
