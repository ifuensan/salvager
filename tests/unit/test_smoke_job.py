"""Tests for the daemon-scheduled smoke-test job (hour-gate + runner guard)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from salvager.domain.phase2_audit import Phase2StateSnapshot
from salvager.orchestration import smoke_job as smoke_job_mod
from salvager.orchestration.smoke_job import (
    build_scheduled_smoke_task,
    build_smoke_runner,
)
from salvager.orchestration.smoke_test import DEFAULT_SMOKE_FIXTURES_DIR, SmokeTestSummary


class _FakeStateReader:
    def __init__(self, last_smoke_at: datetime | None) -> None:
        self._last = last_smoke_at

    async def read(self) -> Phase2StateSnapshot:
        return Phase2StateSnapshot(
            globally_disabled=False,
            consecutive_failures=0,
            last_smoke_result="pass" if self._last else None,
            last_smoke_at=self._last,
        )


def _at(hour: int, day: int = 14) -> datetime:
    return datetime(2026, 6, day, hour, 30, 0, tzinfo=UTC)


# ─────────────────────────────────────────────────────────────────────────
# Hour-gate: once per UTC day at the configured hour
# ─────────────────────────────────────────────────────────────────────────


async def _counting_runner(counter: list[int]) -> None:
    counter[0] += 1


def _gate(
    *, last: datetime | None, now: datetime, counter: list[int]
) -> smoke_job_mod.SmokeRunner:
    return build_scheduled_smoke_task(
        runner=lambda: _counting_runner(counter),
        state_reader=_FakeStateReader(last),
        hour_utc=6,
        clock=lambda: now,
    )


async def test_gate_runs_at_configured_hour_when_never_run() -> None:
    counter = [0]
    task = _gate(last=None, now=_at(6), counter=counter)
    await task()
    assert counter[0] == 1


async def test_gate_skips_off_hour() -> None:
    counter = [0]
    task = _gate(last=None, now=_at(7), counter=counter)
    await task()
    assert counter[0] == 0


async def test_gate_skips_when_already_run_today() -> None:
    counter = [0]
    task = _gate(last=_at(6, day=14), now=_at(6, day=14), counter=counter)
    await task()
    assert counter[0] == 0


async def test_gate_runs_next_day() -> None:
    counter = [0]
    task = _gate(last=_at(6, day=13), now=_at(6, day=14), counter=counter)
    await task()
    assert counter[0] == 1


# ─────────────────────────────────────────────────────────────────────────
# Runner: never raises; calls run_smoke_test with discovered fixtures
# ─────────────────────────────────────────────────────────────────────────


def _runner(fixtures_dir: Path) -> Any:
    return build_smoke_runner(
        fixtures_dir=fixtures_dir,
        parsers={},
        audit_writer=object(),  # type: ignore[arg-type]
        state_reader=object(),  # type: ignore[arg-type]
        reporter=object(),  # type: ignore[arg-type]
        tolerance_eur=Decimal("1.00"),
        tolerance_pct=Decimal("5"),
    )


async def test_runner_invokes_smoke_test_with_shipped_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def _fake_run_smoke_test(**_kwargs: Any) -> SmokeTestSummary:
        calls.append(len(_kwargs["fixtures"]))
        return SmokeTestSummary(
            outcomes=[], any_failed=False, fired_lockout=False, fired_recovery=False
        )

    monkeypatch.setattr(smoke_job_mod, "run_smoke_test", _fake_run_smoke_test)
    await _runner(DEFAULT_SMOKE_FIXTURES_DIR)()
    assert calls and calls[0] > 0  # real shipped fixtures were discovered + passed


async def test_runner_swallows_missing_fixtures_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = [0]

    async def _fake_run_smoke_test(**_kwargs: Any) -> SmokeTestSummary:
        ran[0] += 1
        return SmokeTestSummary(
            outcomes=[], any_failed=False, fired_lockout=False, fired_recovery=False
        )

    monkeypatch.setattr(smoke_job_mod, "run_smoke_test", _fake_run_smoke_test)
    # A non-existent dir makes discover_fixtures raise; the runner must log and
    # return without calling run_smoke_test and without raising.
    await _runner(Path("/nonexistent/price_parsers/active"))()
    assert ran[0] == 0
