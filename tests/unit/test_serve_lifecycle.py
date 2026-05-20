"""Tests for ``_serve()`` lifecycle — listener supervision + shutdown ordering.

These exercise the async lifecycle orchestrated by the CLI's ``_serve``
coroutine, specifically:

- **Listener supervision**: when the Telegram callback listener dies
  with a non-cancellation exception, the daemon shuts down instead of
  running half-alive.
- **Shutdown-task drain**: signal-handler-created shutdown tasks are
  awaited before ``composed.aclose()`` so the scheduler drain
  completes before SQLite connections close.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from salvager.domain.errors import TelegramConfigError

# ─────────────────────────────────────────────────────────────────────────
# Minimal fakes that satisfy _serve() without the real adapter stack
# ─────────────────────────────────────────────────────────────────────────


class _FakeScheduler:
    """No-op scheduler that records register/shutdown calls."""

    def __init__(self) -> None:
        self.registered: list[str] = []
        self._shutdown = False

    async def register(self, name: str, *, cadence_minutes: int, task: Any) -> None:
        self.registered.append(name)

    async def shutdown(self) -> None:
        self._shutdown = True


class _FakeDaemon:
    """Minimal stand-in for :class:`Daemon`.

    ``serve_until_shutdown_signal`` blocks until ``shutdown`` sets the event.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.shutdown_reasons: list[str] = []

    async def start(self) -> None:
        pass

    async def serve_until_shutdown_signal(self) -> None:
        await self._event.wait()

    async def shutdown(self, *, reason: str = "unknown") -> None:
        if self._event.is_set():
            return
        self._event.set()
        self.shutdown_reasons.append(reason)


class _FakeStore:
    """Records set_meta + close calls."""

    def __init__(self) -> None:
        self.meta: dict[str, str] = {}
        self.closed = False

    async def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value

    async def close(self) -> None:
        self.closed = True


class _FakeTelegramSurface:
    """Configurable listen_callbacks: can succeed, raise, or block."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error

    async def listen_callbacks(self, handler: Any) -> None:
        if self._error is not None:
            raise self._error
        # Block forever until cancelled (normal production behavior).
        await asyncio.Event().wait()


class _FakeDispatcher:
    async def handle(self, event: Any) -> None:
        pass


class _FakeCache:
    async def close(self) -> None:
        pass


@dataclass
class _FakeComposedDaemon:
    daemon: _FakeDaemon
    store: _FakeStore
    cache: _FakeCache
    telegram: _FakeTelegramSurface
    dispatcher: _FakeDispatcher
    aclose_called: bool = field(default=False, init=False)

    async def aclose(self) -> None:
        self.aclose_called = True
        await self.cache.close()
        await self.store.close()


# ─────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_listener_failure_triggers_daemon_shutdown() -> None:
    """When the Telegram listener dies (e.g. TelegramConfigError from an
    invalid bot token), the daemon must shut down — not continue running
    with view/skip/snooze taps silently broken.
    """
    from salvager.cli.app import _serve

    daemon = _FakeDaemon()
    composed = _FakeComposedDaemon(
        daemon=daemon,
        store=_FakeStore(),
        cache=_FakeCache(),
        telegram=_FakeTelegramSurface(error=TelegramConfigError("bad token")),
        dispatcher=_FakeDispatcher(),
    )

    await _serve(composed)  # type: ignore[arg-type]

    assert "listener-failed" in daemon.shutdown_reasons
    assert composed.aclose_called


@pytest.mark.asyncio
async def test_aclose_waits_for_shutdown_tasks() -> None:
    """``composed.aclose()`` must not run until all signal-handler-initiated
    shutdown tasks have completed.  This prevents the SQLite connections
    from closing while the scheduler drain is still in progress.
    """
    from salvager.cli.app import _serve

    # Track ordering of operations.
    ordering: list[str] = []

    class _SlowShutdownDaemon(_FakeDaemon):
        async def shutdown(self, *, reason: str = "unknown") -> None:
            if self._event.is_set():
                return
            self._event.set()
            self.shutdown_reasons.append(reason)
            # Simulate slow scheduler drain.
            await asyncio.sleep(0.05)
            ordering.append("shutdown_complete")

    class _TrackingComposed(_FakeComposedDaemon):
        async def aclose(self) -> None:
            ordering.append("aclose")
            await super().aclose()

    daemon = _SlowShutdownDaemon()
    composed = _TrackingComposed(
        daemon=daemon,
        store=_FakeStore(),
        cache=_FakeCache(),
        # Listener dies immediately → triggers _on_listener_done → daemon.shutdown
        telegram=_FakeTelegramSurface(error=TelegramConfigError("bad token")),
        dispatcher=_FakeDispatcher(),
    )

    await _serve(composed)  # type: ignore[arg-type]

    # The shutdown task must complete before aclose runs.
    assert "shutdown_complete" in ordering
    assert "aclose" in ordering
    assert ordering.index("shutdown_complete") < ordering.index("aclose")


@pytest.mark.asyncio
async def test_normal_signal_shutdown_still_works() -> None:
    """Programmatic shutdown (simulating SIGTERM) still cleanly shuts
    down both the daemon and the listener."""
    from salvager.cli.app import _serve

    daemon = _FakeDaemon()
    composed = _FakeComposedDaemon(
        daemon=daemon,
        store=_FakeStore(),
        cache=_FakeCache(),
        telegram=_FakeTelegramSurface(),  # blocks until cancelled
        dispatcher=_FakeDispatcher(),
    )

    async def _trigger_shutdown() -> None:
        await asyncio.sleep(0.02)
        await daemon.shutdown(reason="test-signal")

    serve_task = asyncio.create_task(_serve(composed))  # type: ignore[arg-type]
    shutdown_trigger = asyncio.create_task(_trigger_shutdown())
    _ = shutdown_trigger  # prevent GC; RUF006

    await asyncio.wait_for(serve_task, timeout=2.0)

    assert composed.aclose_called
    assert composed.store.closed
