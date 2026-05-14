"""SIGTERM graceful drain — Story 4.8 (FR50).

Drives the REAL :class:`AsyncioScheduler` + :class:`Daemon` with a poll
job that evaluates five mock listings sequentially. A simulated SIGTERM
(``daemon.shutdown(reason="sigterm")``) arrives mid-evaluation; the test
asserts:

  - the shutdown blocks while the in-flight cycle is still draining,
  - once the cycle finishes, all five listings landed (no half-evaluated
    cycle — the drain awaits the in-flight cycle to completion),
  - the next cycle never starts (listings not yet started are skipped),
  - the exit happens well within the FR50 30-second budget,
  - the final ``daemon_stopped`` event carries ``reason="sigterm"`` and a
    numeric ``drain_seconds``.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from hardware_hunter.adapters.asyncio_scheduler.scheduler import AsyncioScheduler
from hardware_hunter.orchestration.daemon import Daemon

_FR50_BUDGET_S = 30.0


def _records(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


async def test_sigterm_drains_in_flight_cycle(capsys: Any) -> None:
    batch_one = [f"L{i}" for i in range(5)]
    batch_two = [f"L{i}" for i in range(5, 10)]
    seen: list[str] = []

    cycle_count = 0
    mid_evaluation = asyncio.Event()
    release = asyncio.Event()

    async def poll_cycle() -> None:
        nonlocal cycle_count
        cycle_count += 1
        batch = batch_one if cycle_count == 1 else batch_two
        for index, listing_id in enumerate(batch):
            if cycle_count == 1 and index == 2:
                # SIGTERM lands here — partway through the first cycle.
                mid_evaluation.set()
                await release.wait()
            seen.append(listing_id)

    # Long inter-cycle unit so the cadence sleep never fires during the
    # test — the only cycle that runs is the first, kicked off on register.
    scheduler = AsyncioScheduler(seconds_per_cadence_unit=1000.0)
    daemon = Daemon(
        scheduler=scheduler,
        wallapop_job=poll_cycle,
        wallapop_cadence_minutes=1,
    )

    await daemon.start()
    await asyncio.wait_for(mid_evaluation.wait(), timeout=2.0)

    # SIGTERM arrives mid-evaluation: shutdown must block on the drain.
    shutdown_task = asyncio.create_task(daemon.shutdown(reason="sigterm"))
    await asyncio.sleep(0.02)
    assert not shutdown_task.done(), "shutdown must wait for the in-flight cycle"

    # Let the in-flight cycle run to completion.
    release.set()
    started = time.monotonic()
    await asyncio.wait_for(shutdown_task, timeout=_FR50_BUDGET_S)
    drain_elapsed = time.monotonic() - started
    assert drain_elapsed <= _FR50_BUDGET_S

    # The in-flight cycle drained fully — all 5 landed, none half-evaluated.
    assert seen == batch_one
    # The next cycle never started (its listings were skipped).
    assert cycle_count == 1
    assert not any(listing_id in seen for listing_id in batch_two)

    records = _records(capsys.readouterr().out)
    stopped = [r for r in records if r["event"] == "daemon_stopped"]
    assert stopped, "daemon_stopped event must be emitted"
    assert stopped[0]["reason"] == "sigterm"
    assert isinstance(stopped[0]["drain_seconds"], int | float)
