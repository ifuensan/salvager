"""Tests for the daemon lifecycle wrapper — Story 3.14.

The scheduler is mocked; we only assert the daemon composes jobs +
emits the lifecycle events. The actual job registration semantics
are exercised by :mod:`test_asyncio_scheduler`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from salvager.interfaces.scheduler import Scheduler, SchedulerTask
from salvager.orchestration.daemon import (
    DEFAULT_EBAY_CADENCE_MINUTES,
    DEFAULT_WALLAPOP_CADENCE_MINUTES,
    Daemon,
)


class _FakeScheduler(Scheduler):
    def __init__(self) -> None:
        self.registered: list[tuple[str, int, SchedulerTask]] = []
        self.shutdown_calls = 0

    async def register(
        self,
        job_name: str,
        cadence_minutes: int,
        task: SchedulerTask,
    ) -> None:
        self.registered.append((job_name, cadence_minutes, task))

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def _records(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


async def _noop() -> None:
    return None


# ─────────────────────────────────────────────────────────────────────────
# Construction
# ─────────────────────────────────────────────────────────────────────────


def test_daemon_requires_at_least_one_job() -> None:
    with pytest.raises(ValueError, match="at least one marketplace job"):
        Daemon(scheduler=_FakeScheduler())


# ─────────────────────────────────────────────────────────────────────────
# start — registers each job exactly once + emits daemon_started
# ─────────────────────────────────────────────────────────────────────────


async def test_start_registers_both_jobs_at_configured_cadence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    scheduler = _FakeScheduler()
    daemon = Daemon(
        scheduler=scheduler,
        wallapop_job=_noop,
        wallapop_cadence_minutes=10,
        ebay_job=_noop,
        ebay_cadence_minutes=20,
    )

    await daemon.start()

    names = [name for name, _, _ in scheduler.registered]
    cadences = {name: cad for name, cad, _ in scheduler.registered}
    assert names == ["wallapop_poll", "ebay_poll"]
    assert cadences["wallapop_poll"] == 10
    assert cadences["ebay_poll"] == 20

    records = _records(capsys.readouterr().out)
    started = [r for r in records if r["event"] == "daemon_started"]
    assert started
    job_names = [job["name"] for job in started[0]["jobs"]]
    assert "wallapop_poll" in job_names
    assert "ebay_poll" in job_names


async def test_start_with_only_wallapop_skips_ebay() -> None:
    scheduler = _FakeScheduler()
    daemon = Daemon(scheduler=scheduler, wallapop_job=_noop)
    await daemon.start()
    assert [name for name, _, _ in scheduler.registered] == ["wallapop_poll"]


async def test_start_uses_default_cadences_when_unspecified() -> None:
    scheduler = _FakeScheduler()
    daemon = Daemon(scheduler=scheduler, wallapop_job=_noop, ebay_job=_noop)
    await daemon.start()
    cadences = {name: cad for name, cad, _ in scheduler.registered}
    assert cadences["wallapop_poll"] == DEFAULT_WALLAPOP_CADENCE_MINUTES
    assert cadences["ebay_poll"] == DEFAULT_EBAY_CADENCE_MINUTES


async def test_start_is_idempotent() -> None:
    """A second start() call does not re-register jobs."""
    scheduler = _FakeScheduler()
    daemon = Daemon(scheduler=scheduler, wallapop_job=_noop)
    await daemon.start()
    await daemon.start()
    assert len(scheduler.registered) == 1


async def test_start_registers_smoke_job_and_runs_startup_smoke() -> None:
    """When a smoke job is wired it registers as ``phase2_smoke_test`` and the
    one-shot startup runner fires exactly once on start."""
    scheduler = _FakeScheduler()
    startup_runs = 0

    async def _startup() -> None:
        nonlocal startup_runs
        startup_runs += 1

    daemon = Daemon(
        scheduler=scheduler,
        wallapop_job=_noop,
        smoke_job=_noop,
        smoke_cadence_minutes=30,
        smoke_startup=_startup,
    )
    await daemon.start()

    cadences = {name: cad for name, cad, _ in scheduler.registered}
    assert cadences["phase2_smoke_test"] == 30
    assert startup_runs == 1


async def test_start_without_smoke_job_registers_no_smoke() -> None:
    scheduler = _FakeScheduler()
    daemon = Daemon(scheduler=scheduler, wallapop_job=_noop)
    await daemon.start()
    assert "phase2_smoke_test" not in [name for name, _, _ in scheduler.registered]


# ─────────────────────────────────────────────────────────────────────────
# Shutdown — drains scheduler, emits daemon_stopped, idempotent
# ─────────────────────────────────────────────────────────────────────────


async def test_shutdown_drains_scheduler_and_logs_stopped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    scheduler = _FakeScheduler()
    daemon = Daemon(scheduler=scheduler, wallapop_job=_noop)
    await daemon.start()
    capsys.readouterr()
    await daemon.shutdown()
    out = capsys.readouterr().out

    assert scheduler.shutdown_calls == 1
    records = _records(out)
    stopped = [r for r in records if r["event"] == "daemon_stopped"]
    assert stopped


async def test_shutdown_is_idempotent() -> None:
    scheduler = _FakeScheduler()
    daemon = Daemon(scheduler=scheduler, wallapop_job=_noop)
    await daemon.start()
    await daemon.shutdown()
    await daemon.shutdown()  # second call must not re-drain
    assert scheduler.shutdown_calls == 1


async def test_shutdown_threads_reason_and_drain_seconds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Story 4.8: daemon_stopped carries ctx={reason, drain_seconds}."""
    scheduler = _FakeScheduler()
    daemon = Daemon(scheduler=scheduler, wallapop_job=_noop)
    await daemon.start()
    capsys.readouterr()
    await daemon.shutdown(reason="sigterm")
    out = capsys.readouterr().out

    stopped = [r for r in _records(out) if r["event"] == "daemon_stopped"]
    assert stopped
    assert stopped[0]["reason"] == "sigterm"
    assert isinstance(stopped[0]["drain_seconds"], int | float)
    assert stopped[0]["drain_seconds"] >= 0


# ─────────────────────────────────────────────────────────────────────────
# serve_until_shutdown_signal — blocks until shutdown flips the event
# ─────────────────────────────────────────────────────────────────────────


async def test_serve_returns_when_shutdown_is_called() -> None:
    scheduler = _FakeScheduler()
    daemon = Daemon(scheduler=scheduler, wallapop_job=_noop)
    await daemon.start()

    serve_task = asyncio.create_task(daemon.serve_until_shutdown_signal())
    await asyncio.sleep(0.01)  # give the task a chance to enter wait()
    assert not serve_task.done()

    await daemon.shutdown()
    await asyncio.wait_for(serve_task, timeout=1.0)
    assert serve_task.done()
