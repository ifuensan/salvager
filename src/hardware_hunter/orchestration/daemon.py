"""Daemon lifecycle — Story 3.14 entry-point shell.

Owns the scheduler + per-marketplace job registration + shutdown
drain. The components (store, fetchers, evaluator, telegram surface)
are constructed by the caller and passed in fully wired; this module
does not touch credentials or config.yaml — that wiring lives in
:func:`compose_daemon_from_config` (TBD; tracked alongside the daemon
CLI entry point in a follow-up).

Lifecycle
---------
- :meth:`start` registers each enabled marketplace's poll job with the
  scheduler at the configured cadence and emits ``daemon_started``.
- :meth:`serve_until_shutdown_signal` blocks until :meth:`shutdown`
  is called from elsewhere (signal handler).
- :meth:`shutdown` drains in-flight scheduler jobs (FR50 budget) and
  emits ``daemon_stopped`` with ``ctx={"reason", "drain_seconds"}``.
  Idempotent.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Final

from hardware_hunter.interfaces.scheduler import Scheduler
from hardware_hunter.observability.logging import get_logger

#: Default cadence (minutes) per marketplace if config.yaml omits them.
DEFAULT_WALLAPOP_CADENCE_MINUTES: Final[int] = 15
DEFAULT_EBAY_CADENCE_MINUTES: Final[int] = 30


class Daemon:
    """Composition root for the running daemon.

    The caller injects one async-callable job per enabled marketplace.
    Each job is the closure ``() -> run_poll_cycle(...)`` with all
    components pre-bound; the daemon doesn't introspect them.
    """

    def __init__(
        self,
        *,
        scheduler: Scheduler,
        wallapop_job: Callable[[], Awaitable[None]] | None = None,
        wallapop_cadence_minutes: int = DEFAULT_WALLAPOP_CADENCE_MINUTES,
        ebay_job: Callable[[], Awaitable[None]] | None = None,
        ebay_cadence_minutes: int = DEFAULT_EBAY_CADENCE_MINUTES,
    ) -> None:
        if wallapop_job is None and ebay_job is None:
            raise ValueError("Daemon must be given at least one marketplace job to run")
        self._scheduler = scheduler
        self._wallapop_job = wallapop_job
        self._wallapop_cadence_minutes = wallapop_cadence_minutes
        self._ebay_job = ebay_job
        self._ebay_cadence_minutes = ebay_cadence_minutes
        self._shutdown_event = asyncio.Event()
        self._started = False
        self._log = get_logger("orchestration.daemon")

    async def start(self) -> None:
        """Register every configured job with the scheduler and log
        ``daemon_started``. Subsequent calls are a no-op."""
        if self._started:
            return
        registered: list[dict[str, object]] = []

        if self._wallapop_job is not None:
            await self._scheduler.register(
                "wallapop_poll",
                cadence_minutes=self._wallapop_cadence_minutes,
                task=self._wallapop_job,
            )
            registered.append(
                {
                    "name": "wallapop_poll",
                    "cadence_minutes": self._wallapop_cadence_minutes,
                }
            )

        if self._ebay_job is not None:
            await self._scheduler.register(
                "ebay_poll",
                cadence_minutes=self._ebay_cadence_minutes,
                task=self._ebay_job,
            )
            registered.append(
                {
                    "name": "ebay_poll",
                    "cadence_minutes": self._ebay_cadence_minutes,
                }
            )

        self._started = True
        self._log.info("daemon_started", extra={"jobs": registered})

    async def serve_until_shutdown_signal(self) -> None:
        """Block until :meth:`shutdown` flips the internal event.

        Typical wiring: an outer signal handler (``signal.SIGTERM`` →
        ``loop.create_task(daemon.shutdown())``) triggers shutdown
        from outside; this coroutine returns once that happens.
        """
        await self._shutdown_event.wait()

    async def shutdown(self, *, reason: str = "unknown") -> None:
        """Drain in-flight scheduler jobs and emit ``daemon_stopped``.

        ``reason`` is threaded from the signal handler (``"sigterm"`` /
        ``"sigint"``) and surfaced — alongside the measured drain
        duration — in the ``daemon_stopped`` event ctx (Story 4.8 / FR50).

        Idempotent: a second call is a no-op so a signal-handler race
        can't double-shutdown.
        """
        if self._shutdown_event.is_set():
            return
        self._shutdown_event.set()
        started = time.monotonic()
        await self._scheduler.shutdown()
        drain_seconds = round(time.monotonic() - started, 2)
        self._log.info(
            "daemon_stopped",
            extra={"reason": reason, "drain_seconds": drain_seconds},
        )


__all__ = [
    "DEFAULT_EBAY_CADENCE_MINUTES",
    "DEFAULT_WALLAPOP_CADENCE_MINUTES",
    "Daemon",
]
